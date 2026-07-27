#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 对真实第三方形态请求同时采集 ingress 与 Sub2API 出站，退出时恢复账号、代理与 CA 状态。
capture_container=${CAPTURE_CONTAINER:-capture-cli}
service_container=${SERVICE_CONTAINER:-sub2apiplus}
keeper_container=${KEEPER_CONTAINER:-sub2apiplus-keeper}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
account_id=${ACCOUNT_ID:?必须提供 ACCOUNT_ID}
api_key_id=${API_KEY_ID:-1}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
mode=${MODE:?必须提供 MODE}
model=${MODEL:?必须提供 MODEL}
run_id=${RUN_ID:?必须提供 RUN_ID}
window_id=${WINDOW_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
request_count=${REQUEST_COUNT:-1}

case "$mode" in
  openai-http) subject=third-party-openai-http ;;
  openai-ws) subject=third-party-openai-ws ;;
  anthropic-http) subject=third-party-anthropic-http ;;
  *) echo "不支持的模式：$mode" >&2; exit 2 ;;
esac

safe_pattern='^[A-Za-z0-9._-]+$'
if [[ ! $run_id =~ $safe_pattern ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi
if [[ ! $request_count =~ ^[1-9][0-9]*$ ]]; then
  echo "REQUEST_COUNT 必须是正整数。" >&2
  exit 2
fi

ca_source="$capture_root/state/mitm/mitmproxy-ca-cert.pem"
custom_ca_path=/usr/local/share/ca-certificates/oauth-capture.crt
backup_path="$capture_root/runtime/ca-certificates.crt.before-$window_id"
proxy_name="third-party-mitm-$account_id-$window_id"

db_user=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" | sed -n 's/^POSTGRES_USER=//p')
db_name=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" | sed -n 's/^POSTGRES_DB=//p')

db_query() {
  docker exec "$postgres_container" psql -U "$db_user" -d "$db_name" -qAtc "$1"
}

wait_healthy() {
  local status
  for _ in $(seq 1 90); do
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service_container")
    if [[ $status == healthy || $status == running ]]; then
      return 0
    fi
    sleep 1
  done
  echo "Sub2API 未在 90 秒内恢复健康。" >&2
  return 1
}

restart_service() {
  docker restart "$service_container" >/dev/null
  wait_healthy
}

ingress_started=0
mitm_started=0
proxy_id=""
proxy_created=0
ca_installed=0
backup_created=0
keeper_was_running=false
restore_failed=0

stop_capture() {
  if [[ $ingress_started == 1 ]]; then
    docker exec "$capture_container" /capture/scripts/stop_ingress.sh || true
    ingress_started=0
  fi
  if [[ $mitm_started == 1 ]]; then
    docker exec "$capture_container" /opt/oauth-capture/scripts/stop_mitm.sh || true
    mitm_started=0
  fi
}

restore_environment() {
  local original_exit_code=$?
  trap - EXIT ERR INT TERM
  set +e
  stop_capture

  if [[ $proxy_created == 1 && $proxy_id =~ ^[0-9]+$ ]]; then
    db_query "update accounts set status = '$original_status', schedulable = $original_schedulable, proxy_id = $original_proxy_sql, proxy_fallback_origin_id = $original_fallback_sql where id = $account_id" >/dev/null || restore_failed=1
    db_query "delete from proxies where id = $proxy_id and name = '$proxy_name'" >/dev/null || restore_failed=1
  fi
  if [[ $ca_installed == 1 ]]; then
    docker exec "$service_container" rm -f "$custom_ca_path" || restore_failed=1
    docker exec "$service_container" update-ca-certificates >/dev/null 2>&1 || restore_failed=1
  fi
  if [[ $backup_created == 1 && -f $backup_path ]]; then
    docker cp "$backup_path" "$service_container:/etc/ssl/certs/ca-certificates.crt" >/dev/null || restore_failed=1
  fi
  restart_service || restore_failed=1
  if [[ $keeper_was_running == true ]]; then
    docker start "$keeper_container" >/dev/null || restore_failed=1
  fi

  current_state=$(db_query "select status || '|' || schedulable::text || '|' || coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $account_id")
  [[ $current_state == "$original_state" ]] || restore_failed=1
  proxy_count=$(db_query "select count(*) from proxies where id = ${proxy_id:-0} and name = '$proxy_name'")
  [[ $proxy_count == 0 ]] || restore_failed=1
  if [[ $backup_created == 1 && -f $backup_path ]]; then
    restored_hash=$(docker exec "$service_container" sha256sum /etc/ssl/certs/ca-certificates.crt | awk '{print $1}')
    [[ $restored_hash == "$original_ca_hash" ]] || restore_failed=1
  fi
  if [[ $restore_failed == 0 && -f $backup_path ]]; then
    rm -f "$backup_path"
  fi
  if [[ $restore_failed != 0 ]]; then
    echo "第三方 MITM 环境恢复失败，CA 备份保留在：$backup_path" >&2
    exit 97
  fi
  echo "环境已恢复：#$account_id 状态、代理、CA 与 keeper 均与采集前一致。"
  exit "$original_exit_code"
}

trap restore_environment EXIT ERR INT TERM

original_state=$(db_query "select status || '|' || schedulable::text || '|' || coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $account_id")
IFS='|' read -r original_status original_schedulable original_proxy original_fallback <<<"$original_state"
if [[ $original_proxy != NULL || $original_fallback != NULL ]]; then
  echo "账号 #$account_id 已绑定代理或 fallback，拒绝覆盖。" >&2
  exit 1
fi
original_proxy_sql=null
original_fallback_sql=null

api_key=$(db_query "select key from api_keys where id = $api_key_id")
if [[ -z $api_key ]]; then
  echo "测试 API Key 不存在。" >&2
  exit 1
fi
if ! docker exec "$service_container" getent hosts "$capture_container" >/dev/null; then
  echo "Sub2API 容器无法解析 capture-cli。" >&2
  exit 1
fi
test -s "$ca_source"

install -d -m 0700 "$capture_root/runtime"
docker cp "$service_container:/etc/ssl/certs/ca-certificates.crt" "$backup_path" >/dev/null
chmod 0600 "$backup_path"
backup_created=1
original_ca_hash=$(sha256sum "$backup_path" | awk '{print $1}')

keeper_was_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
if [[ $keeper_was_running == true ]]; then
  docker stop "$keeper_container" >/dev/null
fi

proxy_id=$(db_query "insert into proxies (name,protocol,host,port,status,fallback_mode) values ('$proxy_name','http','capture-cli',18080,'active','none') returning id")
if [[ ! $proxy_id =~ ^[0-9]+$ ]]; then
  echo "创建临时代理失败。" >&2
  exit 1
fi
proxy_created=1

# setup 样本隔离安装 CA、绑定代理和重启产生的伴随流量。
setup_run="$run_id-setup"
docker exec "$capture_container" /opt/oauth-capture/scripts/start_mitm.sh "$setup_run" third-party-setup
mitm_started=1
db_query "update accounts set status = 'active', schedulable = true, proxy_id = $proxy_id, proxy_fallback_origin_id = null where id = $account_id" >/dev/null
docker cp "$ca_source" "$service_container:$custom_ca_path" >/dev/null
docker exec "$service_container" update-ca-certificates >/dev/null
ca_installed=1
restart_service
docker exec "$capture_container" /opt/oauth-capture/scripts/stop_mitm.sh
mitm_started=0

docker exec "$capture_container" /opt/oauth-capture/scripts/start_mitm.sh "$run_id" "$subject"
mitm_started=1
docker exec "$capture_container" /capture/scripts/start_ingress.sh "$run_id" "$subject"
ingress_started=1
for request_index in $(seq 1 "$request_count"); do
  output_dir="/capture/runs/$run_id/result"
  if [[ $request_count -gt 1 ]]; then
    output_dir="$output_dir/attempt-$request_index"
  fi
  docker exec -e SUB2API_API_KEY="$api_key" "$capture_container" \
    python3 /capture/tools/official_client_capture/run_third_party_profile_scenario.py \
    --mode "$mode" --model "$model" --output-dir "$output_dir" --timeout 300
done
stop_capture

python3 - "$capture_root/runs/$run_id" "$account_id" "$mode" "$model" <<'PY'
import json
import os
import sys
from pathlib import Path

run_root = Path(sys.argv[1])
scenario_paths = sorted((run_root / "result").rglob("summary.json"))
scenarios = [json.loads(path.read_text()) for path in scenario_paths]
jsonl = sorted(run_root.glob("mitm/*/*.jsonl"))
payload = {
    "schema_version": "third-party-mitm-capture/v1",
    "run_id": run_root.name,
    "status": "complete" if scenarios and all(item.get("request_completed") for item in scenarios) and any(path.stat().st_size for path in jsonl) else "failed",
    "account_id": int(sys.argv[2]),
    "mode": sys.argv[3],
    "model": sys.argv[4],
    "upstream_success": bool(scenarios) and all(item.get("upstream_success") for item in scenarios),
    "scenarios": scenarios,
    "jsonl": [
        {"path": str(path.relative_to(run_root)), "records": sum(1 for _ in path.open(encoding="utf-8"))}
        for path in jsonl
    ],
}
output = run_root / "run-summary.json"
output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(output, 0o600)
if payload["status"] != "complete":
    raise SystemExit("第三方 MITM 请求或出站记录未完成。")
print(json.dumps(payload, ensure_ascii=False))
PY

printf 'run_id=%s\n' "$run_id"
