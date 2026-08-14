#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

capture_container=${CAPTURE_CONTAINER:-capture-cli}
service_container=${SERVICE_CONTAINER:-sub2apiplus}
keeper_container=${KEEPER_CONTAINER:-sub2apiplus-keeper}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
codex_account_id=${CODEX_ACCOUNT_ID:-90}
api_key_id=${API_KEY_ID:-1}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}
subjects=${SUBJECTS:-"codex-http codex-ws"}
# 与 direct 矩阵保持同一四场景覆盖，避免 A02 只落三份 WS pcap。
scenarios=${SCENARIOS:-"s1 s2 s3 s4"}
codex_model=${CODEX_MODEL:-gpt-5.6-luna}
codex_version=${CODEX_VERSION:?必须由 Campaign 提供 CODEX_VERSION}
if [[ ! $codex_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "CODEX_VERSION 必须是完整的 x.y.z 版本。" >&2
  exit 2
fi
run_id_prefix=${RUN_ID_PREFIX:-p0-p2-review-fix-mitm-openai-0.1.165-3}
window_id=${WINDOW_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
ca_source="$capture_root/state/mitm/mitmproxy-ca-cert.pem"
custom_ca_path=/usr/local/share/ca-certificates/oauth-capture.crt
backup_path="$capture_root/runtime/ca-certificates.crt.before-$window_id"
proxy_name="review-fix-sub2api-mitm-$window_id"

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
run_ids=()

stop_pair() {
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

  stop_pair
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
  restart_service || restore_failed=1
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

api_key=$(db_query "select key from api_keys where id = $api_key_id")
if [[ -z $api_key ]]; then
  echo "测试 API Key 不存在。" >&2
  exit 1
fi

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
setup_run="$run_id_prefix-setup-$window_id"
docker exec "$capture_container" /opt/oauth-capture/scripts/start_mitm.sh "$setup_run" sub2api-setup
mitm_started=1
db_query "update accounts set proxy_id = $proxy_id, proxy_fallback_origin_id = null where id = $codex_account_id" >/dev/null
docker cp "$ca_source" "$service_container:$custom_ca_path"
docker exec "$service_container" update-ca-certificates >/dev/null
ca_installed=1
restart_service
docker exec "$capture_container" /opt/oauth-capture/scripts/stop_mitm.sh
mitm_started=0

for subject in $subjects; do
  case "$subject" in
    codex-http) mode=sub2api-http ;;
    codex-ws) mode=sub2api-ws ;;
    codex-compact) mode=sub2api-compact ;;
    *) echo "不支持的 OpenAI 主体：$subject" >&2; exit 2 ;;
  esac
  run_id="$run_id_prefix-$subject-$window_id"
  run_ids+=("$run_id")
  docker exec "$capture_container" /opt/oauth-capture/scripts/start_mitm.sh "$run_id" "$subject"
  mitm_started=1
  docker exec "$capture_container" /capture/scripts/start_ingress.sh "$run_id" "$subject"
  ingress_started=1
  for scenario in $scenarios; do
    output_dir="/capture/runs/$run_id/result/$scenario"
    if [[ $mode == sub2api-compact ]]; then
      if [[ $scenario != compact ]]; then
        echo "codex-compact 只接受 compact 场景。" >&2
        exit 2
      fi
      docker exec -e SUB2API_API_KEY="$api_key" "$capture_container" \
        python3 "$capture_tool_root/run_codex_compact_scenario.py" \
        --mode sub2api-http --model "$codex_model" --codex-version "$codex_version" \
        --output-dir "$output_dir" --timeout 70
    else
      docker exec -e SUB2API_API_KEY="$api_key" "$capture_container" \
        python3 /capture/scripts/run_codex_scenario.py \
        --mode "$mode" --scenario "$scenario" --model "$codex_model" \
        --output-dir "$output_dir" --timeout 70
    fi
  done
  stop_pair
done

python3 - "$capture_root/runs" "$codex_model" "$scenarios" "${run_ids[@]}" <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
model = sys.argv[2]
scenarios = sys.argv[3].split()
for run_id in sys.argv[4:]:
    run_root = root / run_id
    results = []
    for scenario in scenarios:
        summary = json.loads((run_root / "result" / scenario / "summary.json").read_text())
        results.append({"scenario": scenario, "valid": bool(summary.get("valid"))})
    jsonl = sorted(run_root.glob("mitm/*/*.jsonl"))
    payload = {
        "schema_version": "sub2api-openai-mitm/v1",
        "run_id": run_id,
        "status": "complete" if all(item["valid"] for item in results) else "failed",
        "model": model,
        "scenarios": results,
        "jsonl": [
            {"path": str(path.relative_to(run_root)), "records": sum(1 for _ in path.open(encoding="utf-8"))}
            for path in jsonl
        ],
    }
    output = run_root / "run-summary.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(output, 0o600)
    if payload["status"] != "complete" or not any(item["records"] > 0 for item in payload["jsonl"]):
        raise SystemExit(f"{run_id} 的场景或 MITM 记录校验失败")
    print(json.dumps(payload, ensure_ascii=False))
PY

printf 'run_ids=%s\n' "${run_ids[*]}"
