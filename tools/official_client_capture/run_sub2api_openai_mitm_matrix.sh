#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

capture_container=${CAPTURE_CONTAINER:-capture-cli}
service_container=${SERVICE_CONTAINER:-sub2apiplus}
keeper_container=${KEEPER_CONTAINER:-sub2apiplus-keeper}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
codex_account_id=${CODEX_ACCOUNT_ID:?必须由 Campaign 显式提供 CODEX_ACCOUNT_ID}
api_key_id=${API_KEY_ID:?必须由 Campaign 显式提供 API_KEY_ID}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_mount=${CAPTURE_MOUNT:-/capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$capture_root/tools/official_client_capture}
capture_runtime_root=${CAPTURE_RUNTIME_ROOT:-$capture_tool_root/runtime_scripts}
subjects=${SUBJECTS:-"codex-http codex-ws"}
# 与 direct 矩阵保持相同场景覆盖；MITM 只保存应用层 JSONL，不生成 pcap。
scenarios=${SCENARIOS:-"s1 s2 s3 s4"}
codex_model=${CODEX_MODEL:-gpt-5.6-luna}
codex_version=${CODEX_VERSION:?必须由 Campaign 提供 CODEX_VERSION}
scenario_timeout_seconds=${SCENARIO_TIMEOUT_SECONDS:-120}
scenario_attempt_limit=2
fingerprint_tcp_max_segment=${CAPTURE_FINGERPRINT_TCP_MAXSEG:-1368}
fingerprint_proxy_host_path=${FINGERPRINT_PROXY_HOST_PATH:-$capture_root/runtime/codex-fingerprint-capture-proxy}
fingerprint_proxy_container_path=${FINGERPRINT_PROXY_CONTAINER_PATH:-$capture_mount/runtime/codex-fingerprint-capture-proxy}
codex_profile_host_path=${CODEX_PROFILE_HOST_PATH:-$capture_root/runtime/codex-profile-$codex_version.json}
codex_profile_container_path=${CODEX_PROFILE_CONTAINER_PATH:-$capture_mount/runtime/codex-profile-$codex_version.json}
prewarm_tool="$capture_tool_root/prewarm_codex_home.py"
if [[ ! $codex_account_id =~ ^[1-9][0-9]*$ || ! $api_key_id =~ ^[1-9][0-9]*$ ]]; then
  echo "CODEX_ACCOUNT_ID 与 API_KEY_ID 必须是正整数。" >&2
  exit 2
fi
if [[ ! $codex_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "CODEX_VERSION 必须是完整的 x.y.z 版本。" >&2
  exit 2
fi
if [[ ! $scenario_timeout_seconds =~ ^[0-9]+$ ]] || (( scenario_timeout_seconds < 120 || scenario_timeout_seconds > 600 )); then
  echo "SCENARIO_TIMEOUT_SECONDS 必须是 120～600 的整数秒。" >&2
  exit 2
fi
if [[ ! $fingerprint_tcp_max_segment =~ ^[0-9]+$ ]] || (( fingerprint_tcp_max_segment < 536 || fingerprint_tcp_max_segment > 65495 )); then
  echo "CAPTURE_FINGERPRINT_TCP_MAXSEG 必须为 536～65495 的整数。" >&2
  exit 2
fi
codex_bin=${CODEX_BIN:-/opt/codex-$codex_version/bin/codex}
if [[ $codex_bin != /* ]]; then
  echo "CODEX_BIN 必须是绝对路径。" >&2
  exit 2
fi
run_id_prefix=${RUN_ID_PREFIX:-p0-p2-review-fix-mitm-openai-0.1.165-3}
window_id=${WINDOW_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
if [[ ! $window_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "WINDOW_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi
checkpoint_tool="$capture_tool_root/mitm_scenario_checkpoint.py"
for path in "$checkpoint_tool" "$prewarm_tool"; do
  if [[ -L $path || ! -f $path ]]; then
    echo "MITM 运行工具不存在或不可信：$path" >&2
    exit 1
  fi
done

# 在任何 Docker 探针、代理变更或 live 请求前先读取单场景 checkpoint。
# 若闭集为空立即退出；已通过坐标只登记复用，不会重新启动 MITM。
pending_coordinates=()
run_ids=()
reused_count=0
for subject in $subjects; do
  case "$subject" in
    codex-http|codex-ws) ;;
    codex-compact) ;;
    *) echo "不支持的 OpenAI 主体：$subject" >&2; exit 2 ;;
  esac
  for scenario in $scenarios; do
    if [[ ! $scenario =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
      echo "MITM 场景坐标格式非法：$scenario" >&2
      exit 2
    fi
    if [[ $subject == codex-compact && $scenario != compact ]]; then
      echo "codex-compact 只接受 compact 场景。" >&2
      exit 2
    fi
    if [[ $subject != codex-compact && $scenario == compact ]]; then
      echo "compact 场景只能由 codex-compact 执行。" >&2
      exit 2
    fi
    inspection=$(
      python3 "$checkpoint_tool" inspect \
        --runs-root "$capture_root/runs" \
        --run-id-prefix "$run_id_prefix" \
        --subject "$subject" \
        --scenario "$scenario" \
        --window-id "$window_id" \
        --model "$codex_model" \
        --attempt-limit "$scenario_attempt_limit" \
        --quarantine-incomplete \
        --tsv
    )
    IFS=$'\t' read -r disposition checkpoint_run_id next_attempt quarantined_count <<<"$inspection"
    if [[ $disposition == complete ]]; then
      run_ids+=("$checkpoint_run_id")
      ((reused_count += 1))
    elif [[ $disposition == pending && $next_attempt =~ ^[1-9][0-9]*$ ]]; then
      pending_coordinates+=("$subject|$scenario|$next_attempt")
    else
      echo "MITM 场景 checkpoint 检查返回非法结果：$inspection" >&2
      exit 1
    fi
    if [[ ${quarantined_count:-0} != 0 ]]; then
      printf '已隔离未封存坐标：subject=%s scenario=%s count=%s\n' \
        "$subject" "$scenario" "$quarantined_count" >&2
    fi
  done
done
if (( ${#pending_coordinates[@]} == 0 )); then
  printf 'incremental_noop=true executed=0 reused=%s pcap_scanned_bytes=0\n' "$reused_count"
  printf 'run_ids=%s\n' "${run_ids[*]}"
  exit 0
fi

# 指纹转发器及画像必须在任何账号、CA 或代理变更前完成离线自检。
for path in "$fingerprint_proxy_host_path" "$codex_profile_host_path"; do
  if [[ -L $path || ! -f $path ]]; then
    echo "指纹转发运行文件不存在或不可信：$path" >&2
    exit 1
  fi
done
if [[ ! -x $fingerprint_proxy_host_path ]]; then
  echo "指纹转发器不可执行：$fingerprint_proxy_host_path" >&2
  exit 1
fi
docker exec "$capture_container" test -x "$fingerprint_proxy_container_path"
docker exec "$capture_container" test -f "$codex_profile_container_path"
docker exec "$capture_container" "$fingerprint_proxy_container_path" \
  --validate-only \
  --profile "$codex_profile_container_path" \
  --version "$codex_version" \
  --tcp-max-segment "$fingerprint_tcp_max_segment" \
  --target-host chatgpt.com

actual_codex_version=$(docker exec "$capture_container" "$codex_bin" --version)
if [[ $actual_codex_version != "codex-cli $codex_version" ]]; then
  echo "Codex 二进制版本不一致：预期 codex-cli $codex_version，实际 $actual_codex_version。" >&2
  exit 2
fi
ca_source="$capture_root/state/mitm/mitmproxy-ca-cert.pem"
custom_ca_path=/usr/local/share/ca-certificates/oauth-capture.crt
backup_path="$capture_root/runtime/ca-certificates.crt.before-$window_id"
proxy_name="review-fix-sub2api-mitm-$window_id"
isolated_codex_home="$capture_root/runtime/codex-mitm-home-$window_id"
container_codex_home="$capture_mount/runtime/codex-mitm-home-$window_id"

db_user=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_USER=//p'
)
db_name=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_DB=//p'
)

db_query() {
  docker exec "$postgres_container" \
    psql -U "$db_user" -d "$db_name" -qAtc "$1"
}

account_count=$(db_query "select count(*) from accounts where id = $codex_account_id and status = 'active' and deleted_at is null")
if [[ $account_count != 1 ]]; then
  echo "测试账号不存在、未启用或已软删除。" >&2
  exit 1
fi
api_key=$(db_query "select key from api_keys where id = $api_key_id and status = 'active' and deleted_at is null")
if [[ -z $api_key ]]; then
  echo "测试 API Key 不存在、未启用或已软删除。" >&2
  exit 1
fi

wait_healthy() {
  local current_status
  for _ in $(seq 1 90); do
    current_status=$(
      docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$service_container"
    )
    if [[ $current_status == healthy || $current_status == running ]]; then
      return 0
    fi
    sleep 1
  done
  echo "Sub2API 未在 90 秒内恢复健康。" >&2
  return 1
}

restart_service() {
  local -a maintenance_args=()
  [[ ${1:-} != cleanup ]] || maintenance_args+=(--cleanup)
  python3 "$(dirname "${BASH_SOURCE[0]}")/codex_upgrade_supervisor.py" egress-transition --container "$service_container" \
    "${maintenance_args[@]}" -- docker restart "$service_container" >/dev/null
  wait_healthy
}

ingress_started=0
mitm_started=0
proxy_id=""
proxy_created=0
ca_installed=0
backup_created=0
isolated_codex_home_created=0
keeper_was_running=false
restore_failed=0
active_run_id=""
active_run_root=""
active_subject=""
active_scenario=""
active_driver_return_code=1

stop_pair() {
  if [[ $ingress_started == 1 ]]; then
    docker exec "$capture_container" /capture/scripts/stop_ingress.sh || true
    ingress_started=0
  fi
  if [[ $mitm_started == 1 ]]; then
    docker exec "$capture_container" "$capture_runtime_root/stop_mitm.sh" || true
    mitm_started=0
  fi
}

preserve_active_failure() {
  local failed_root
  [[ -n $active_run_id && -n $active_run_root && -d $active_run_root ]] || return 0
  if [[ ! -e $active_run_root/run-summary.json ]]; then
    python3 "$checkpoint_tool" seal \
      --run-root "$active_run_root" \
      --run-id "$active_run_id" \
      --subject "$active_subject" \
      --scenario "$active_scenario" \
      --model "$codex_model" \
      --driver-return-code "$active_driver_return_code" >/dev/null || true
    if [[ ! -f $active_run_root/run-summary.json || -L $active_run_root/run-summary.json ]]; then
      echo "MITM 失败坐标未能写入 checkpoint：$active_run_root" >&2
      return 1
    fi
  fi
  failed_root="$active_run_root.failed"
  if [[ -e $failed_root || -L $failed_root ]]; then
    echo "MITM 失败证据隔离目标已存在：$failed_root" >&2
    return 1
  fi
  mv -- "$active_run_root" "$failed_root"
  printf '已封存失败坐标：subject=%s scenario=%s root=%s\n' \
    "$active_subject" "$active_scenario" "$failed_root" >&2
}

restore_environment() {
  local original_exit_code=$?
  trap - EXIT ERR INT TERM
  set +e

  stop_pair
  if [[ $original_exit_code != 0 ]]; then
    preserve_active_failure || restore_failed=1
  fi
  if [[ $proxy_created == 1 && $proxy_id =~ ^[0-9]+$ ]]; then
    db_query "update accounts set proxy_id = null, proxy_fallback_origin_id = null where id = $codex_account_id" >/dev/null ||
      restore_failed=1
    db_query "delete from proxies where id = $proxy_id and name = '$proxy_name'" >/dev/null ||
      restore_failed=1
  fi
  if [[ $ca_installed == 1 ]]; then
    docker exec "$service_container" rm -f "$custom_ca_path" || restore_failed=1
    docker exec "$service_container" update-ca-certificates >/dev/null 2>&1 || restore_failed=1
  fi
  if [[ $backup_created == 1 && -f $backup_path ]]; then
    docker cp "$backup_path" "$service_container:/etc/ssl/certs/ca-certificates.crt" || restore_failed=1
  fi
  restart_service cleanup || restore_failed=1
  if [[ $keeper_was_running == true ]]; then
    docker start "$keeper_container" >/dev/null || restore_failed=1
  fi

  current_proxy_state=$(db_query "select coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $codex_account_id")
  [[ $current_proxy_state == "NULL|NULL" ]] || restore_failed=1
  proxy_count=$(db_query "select count(*) from proxies where id = ${proxy_id:-0} and name = '$proxy_name'")
  [[ $proxy_count == 0 ]] || restore_failed=1
  if [[ $backup_created == 1 && -f $backup_path ]]; then
    restored_hash=$(docker exec "$service_container" sha256sum /etc/ssl/certs/ca-certificates.crt | awk '{print $1}')
    [[ $restored_hash == "$original_ca_hash" ]] || restore_failed=1
  fi
  if [[ $isolated_codex_home_created == 1 ]]; then
    case "$isolated_codex_home" in
      "$capture_root"/runtime/codex-mitm-home-*) rm -rf -- "$isolated_codex_home" || restore_failed=1 ;;
      *) restore_failed=1 ;;
    esac
  fi
  if [[ $restore_failed == 0 && -f $backup_path ]]; then
    rm -f "$backup_path"
  fi
  if [[ $restore_failed != 0 ]]; then
    echo "OpenAI MITM 环境恢复失败，CA 备份保留在：$backup_path" >&2
    exit 97
  fi
  echo "环境已恢复：#${codex_account_id} 代理为空、临时代理已删除、CA 哈希一致、keeper 状态已恢复。"
  exit "$original_exit_code"
}

trap restore_environment EXIT ERR INT TERM

# MITM 会放大模型请求时延，不能再继承 capture-cli 的账号配置和 0.151 可选
# MCP／插件发现。独占空 CODEX_HOME 只保留最小隐私配置，避免后台连接占用
# 同一个 120 秒场景预算；目录在统一恢复钩子中删除。
if [[ -e $isolated_codex_home ]]; then
  echo "隔离 CODEX_HOME 已存在，拒绝覆盖：$isolated_codex_home" >&2
  exit 1
fi
install -d -m 0700 "$isolated_codex_home"
isolated_codex_home_created=1
cat >"$isolated_codex_home/config.toml" <<'EOF'
check_for_update_on_startup = false
analytics.enabled = false
feedback.enabled = false
otel.exporter = "none"
otel.log_user_prompt = false

[features]
plugins = false
EOF
chmod 0600 "$isolated_codex_home/config.toml"

# 首次 app-server 初始化单独计时，不占用后续场景请求 timeout；它不发送 live 请求，
# 但仍位于本 Job 和 Campaign 的总墙钟预算内。
docker exec \
  -e HOME="$container_codex_home" \
  -e CODEX_HOME="$container_codex_home" \
  "$capture_container" python3 "$prewarm_tool" \
  --codex-bin "$codex_bin" \
  --codex-version "$codex_version" \
  --output "$container_codex_home/prewarm.json" \
  --timeout 30

current_proxy=$(db_query "select coalesce(proxy_id::text,'NULL') from accounts where id = $codex_account_id")
current_fallback=$(db_query "select coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $codex_account_id")
if [[ $current_proxy != NULL || $current_fallback != NULL ]]; then
  echo "账号 #$codex_account_id 已绑定代理或 fallback，拒绝覆盖。" >&2
  exit 1
fi
if ! docker exec "$service_container" getent hosts "$capture_container" >/dev/null; then
  echo "Sub2API 容器无法解析 capture-cli。" >&2
  exit 1
fi
test -s "$ca_source"

install -d -m 0700 "$capture_root/runtime"
docker cp "$service_container:/etc/ssl/certs/ca-certificates.crt" "$backup_path"
chmod 0600 "$backup_path"
backup_created=1
original_ca_hash=$(sha256sum "$backup_path" | awk '{print $1}')

keeper_was_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
if [[ $keeper_was_running == true ]]; then
  docker stop "$keeper_container" >/dev/null
fi

proxy_id=$(
  # host 必须用 $capture_container，不能写死容器名：第 135 行的 DNS 可达性检查用的就是
  # 它，两处不一致时检查照样通过，而服务真正出站时解析不到写死的名字，报
  # `connect to proxy: lookup capture-cli: no such host`，账号随即被判 upstream
  # transport error 而临时熔断——后续 job 全部拿到 503／WS 1013，看起来像时序问题。
  db_query "insert into proxies (name,protocol,host,port,status,fallback_mode) values ('$proxy_name','http','$capture_container',18080,'active','none') returning id"
)
if [[ ! $proxy_id =~ ^[0-9]+$ ]]; then
  echo "创建临时代理失败。" >&2
  exit 1
fi
proxy_created=1

# setup 样本隔离安装 CA、绑定代理和重启造成的伴随流量。
setup_run=""
for setup_attempt in $(seq 1 9); do
  setup_candidate="$run_id_prefix.setup-a$setup_attempt-$window_id"
  if [[ ! -e $capture_root/runs/$setup_candidate && ! -L $capture_root/runs/$setup_candidate ]]; then
    setup_run=$setup_candidate
    break
  fi
done
if [[ -z $setup_run ]]; then
  echo "MITM setup 尝试次数已到上限，必须停线诊断。" >&2
  exit 1
fi
docker exec \
  -e CAPTURE_TASK=api \
  -e CAPTURE_BOUNDARY=official_cli_to_sub2api \
  -e CAPTURE_SCENARIO=setup \
  -e CAPTURE_TARGET_HOSTS=chatgpt.com \
  -e CAPTURE_HOST_SCOPE=targets \
  "$capture_container" "$capture_runtime_root/start_mitm.sh" "$setup_run" sub2api-setup
mitm_started=1
db_query "update accounts set proxy_id = $proxy_id, proxy_fallback_origin_id = null where id = $codex_account_id" >/dev/null
docker cp "$ca_source" "$service_container:$custom_ca_path"
docker exec "$service_container" update-ca-certificates >/dev/null
ca_installed=1
restart_service
docker exec "$capture_container" "$capture_runtime_root/stop_mitm.sh"
mitm_started=0

executed_count=0
for coordinate in "${pending_coordinates[@]}"; do
  IFS='|' read -r subject scenario attempt_index <<<"$coordinate"
  case "$subject" in
    codex-http) mode=sub2api-http ;;
    codex-ws) mode=sub2api-ws ;;
    codex-compact) mode=sub2api-compact ;;
    *) echo "不支持的 OpenAI 主体：$subject" >&2; exit 2 ;;
  esac
  run_id="$run_id_prefix-$subject-$scenario-a$attempt_index-$window_id"
  run_root="$capture_root/runs/$run_id"
  active_run_id=$run_id
  active_run_root=$run_root
  active_subject=$subject
  active_scenario=$scenario
  active_driver_return_code=1
  docker exec \
    -e CAPTURE_TASK=api \
    -e CAPTURE_BOUNDARY=official_cli_to_sub2api \
    -e CAPTURE_SCENARIO="$scenario" \
    -e CAPTURE_TARGET_HOSTS=chatgpt.com \
    -e CAPTURE_HOST_SCOPE=targets \
    -e CAPTURE_FINGERPRINT_PROXY_BIN="$fingerprint_proxy_container_path" \
    -e CAPTURE_CODEX_PROFILE="$codex_profile_container_path" \
    -e CAPTURE_CODEX_VERSION="$codex_version" \
    -e CAPTURE_FINGERPRINT_TCP_MAXSEG="$fingerprint_tcp_max_segment" \
    "$capture_container" "$capture_runtime_root/start_mitm.sh" "$run_id" "$subject"
  mitm_started=1
  docker exec "$capture_container" /capture/scripts/start_ingress.sh "$run_id" "$subject"
  ingress_started=1
  output_dir="/capture/runs/$run_id/result/$scenario"
  set +e
  if [[ $mode == sub2api-compact ]]; then
    docker exec -e SUB2API_API_KEY="$api_key" -e CODEX_HOME="$container_codex_home" "$capture_container" \
      python3 "$capture_tool_root/run_codex_compact_scenario.py" \
      --codex-bin "$codex_bin" \
      --mode sub2api-http --model "$codex_model" --codex-version "$codex_version" \
      --output-dir "$output_dir" --timeout "$scenario_timeout_seconds"
  else
    docker exec -e SUB2API_API_KEY="$api_key" -e CODEX_BIN="$codex_bin" \
      -e CODEX_HOME="$container_codex_home" \
      -e CODEX_VERSION="$codex_version" "$capture_container" \
      python3 "$capture_tool_root/run_codex_scenario_target.py" \
      --mode "$mode" --scenario "$scenario" --model "$codex_model" \
      --output-dir "$output_dir" --timeout "$scenario_timeout_seconds"
  fi
  active_driver_return_code=$?
  set -e
  stop_pair
  if ! python3 "$checkpoint_tool" seal \
    --run-root "$run_root" \
    --run-id "$run_id" \
    --subject "$subject" \
    --scenario "$scenario" \
    --model "$codex_model" \
    --driver-return-code "$active_driver_return_code"; then
    exit_code=$active_driver_return_code
    (( exit_code == 0 )) && exit_code=1
    exit "$exit_code"
  fi
  active_run_id=""
  active_run_root=""
  active_subject=""
  active_scenario=""
  run_ids+=("$run_id")
  ((executed_count += 1))
done

printf 'incremental_complete=true executed=%s reused=%s pcap_scanned_bytes=0\n' \
  "$executed_count" "$reused_count"
printf 'run_ids=%s\n' "${run_ids[*]}"
