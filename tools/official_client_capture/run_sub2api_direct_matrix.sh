#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

capture_container=${CAPTURE_CONTAINER:-capture-cli}
service_container=${SERVICE_CONTAINER:-sub2apiplus}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
claude_account_id=${CLAUDE_ACCOUNT_ID:-50}
codex_account_id=${CODEX_ACCOUNT_ID:-90}
api_key_id=${API_KEY_ID:-1}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}
subjects=${SUBJECTS:-"codex-http codex-ws"}
# A02 的 TLS 扩展多样性需要四份独立 WS pcap；s3 不是可选样本。
scenarios=${SCENARIOS:-"s1 s2 s3 s4"}
claude_model=${CLAUDE_MODEL:-claude-sonnet-5}
codex_model=${CODEX_MODEL:-gpt-5.6-luna}
codex_version=${CODEX_VERSION:?必须由 Campaign 提供 CODEX_VERSION}
if [[ ! $codex_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "CODEX_VERSION 必须是完整的 x.y.z 版本。" >&2
  exit 2
fi
run_id_prefix=${RUN_ID_PREFIX:-p0-p2-review-fix-direct-0.1.165-3}
run_id=${RUN_ID:-"$run_id_prefix-$(date -u +%Y%m%dT%H%M%SZ)"}

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
  for _ in $(seq 1 45); do
    current_status=$(
      docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$service_container"
    )
    if [[ $current_status == healthy || $current_status == running ]]; then
      return 0
    fi
    sleep 1
  done
  echo "Sub2API 未在 45 秒内恢复健康。" >&2
  return 1
}

restart_service() {
  docker restart "$service_container" >/dev/null
  wait_healthy
}

active_subject=""
direct_started=0
ingress_started=0
original_schedulable_state=""
restore_failed=0

stop_pair() {
  if [[ $ingress_started == 1 ]]; then
    docker exec "$capture_container" /capture/scripts/stop_ingress.sh || true
    ingress_started=0
  fi
  if [[ $direct_started == 1 && -n $active_subject ]]; then
    docker exec "$capture_container" /opt/oauth-capture/scripts/stop_direct.sh \
      "$active_subject" || true
    direct_started=0
  fi
  active_subject=""
}

restore_environment() {
  local original_exit_code=$?
  trap - EXIT ERR INT TERM
  set +e

  stop_pair
  if [[ -n $original_schedulable_state ]]; then
    while IFS=: read -r account_id schedulable; do
      case "$schedulable" in
        true) db_query "update accounts set schedulable = true where id = $account_id" >/dev/null || restore_failed=1 ;;
        false) db_query "update accounts set schedulable = false where id = $account_id" >/dev/null || restore_failed=1 ;;
        *) restore_failed=1 ;;
      esac
    done <<<"$original_schedulable_state"
  fi
  restart_service || restore_failed=1

  current_schedulable_state=$(
    db_query "select id || ':' || schedulable::text from accounts where id in ($claude_account_id,$codex_account_id) order by id"
  )
  [[ $current_schedulable_state == "$original_schedulable_state" ]] || restore_failed=1

  current_proxy_state=$(
    db_query "select id || ':' || coalesce(proxy_id::text,'NULL') from accounts where id in ($claude_account_id,$codex_account_id) order by id"
  )
  expected_proxy_state=$(printf '%s\n%s' "$claude_account_id:NULL" "$codex_account_id:NULL")
  [[ $current_proxy_state == "$expected_proxy_state" ]] || restore_failed=1

  if [[ $restore_failed != 0 ]]; then
    echo "direct 验收环境恢复失败，请人工检查账号调度状态。" >&2
    exit 97
  fi
  echo "环境已恢复：OAuth 验收账号调度状态和代理状态均与运行前一致。"
  exit "$original_exit_code"
}

trap restore_environment EXIT ERR INT TERM

original_schedulable_state=$(
  db_query "select id || ':' || schedulable::text from accounts where id in ($claude_account_id,$codex_account_id) order by id"
)
account_state_count=$(printf '%s\n' "$original_schedulable_state" | awk 'NF { count++ } END { print count + 0 }')
if [[ $account_state_count != 2 ]]; then
  echo "未能精确读取两个 OAuth 验收账号的初始调度状态，拒绝继续。" >&2
  exit 1
fi

for account_id in "$claude_account_id" "$codex_account_id"; do
  current_proxy=$(db_query "select coalesce(proxy_id::text,'NULL') from accounts where id = $account_id")
  current_fallback=$(db_query "select coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $account_id")
  if [[ $current_proxy != NULL || $current_fallback != NULL ]]; then
    echo "账号 #$account_id 存在代理或 fallback，拒绝覆盖。" >&2
    exit 1
  fi
done

api_key=$(db_query "select key from api_keys where id = $api_key_id")
if [[ -z $api_key ]]; then
  echo "测试 API Key 不存在。" >&2
  exit 1
fi

# 两个固定 OAuth 验收账号在抓包窗口保持启用，退出钩子按运行前原值精确恢复。
db_query "update accounts set schedulable = true where id in ($claude_account_id,$codex_account_id)" >/dev/null
restart_service
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

run_case() {
  local subject=$1
  local scenario=$2
  local case_id="$subject-$scenario"
  local output_dir="/capture/runs/$run_id/result/$subject/$scenario"

  # HTTP Transport 会跨请求复用连接；每个 direct 单元前重启服务，
  # 确保该 pcap 自己包含可归因的 ClientHello，而不是只记录旧连接上的数据帧。
  restart_service
  active_subject=$case_id
  docker exec "$capture_container" /opt/oauth-capture/scripts/start_direct.sh \
    "$run_id" "$case_id" "$service_container"
  direct_started=1
  docker exec "$capture_container" /capture/scripts/start_ingress.sh \
    "$run_id" "$case_id"
  ingress_started=1

  case "$subject" in
    claude-http)
      docker exec \
        -e ANTHROPIC_API_KEY="$api_key" \
        -e ANTHROPIC_BASE_URL=http://127.0.0.1:18081 \
        "$capture_container" python3 /capture/scripts/run_claude_scenario.py \
        --mode sub2api --scenario "$scenario" --model "$claude_model" \
        --output-dir "$output_dir" --timeout 70
      ;;
    codex-http)
      docker exec -e SUB2API_API_KEY="$api_key" "$capture_container" \
        python3 /capture/scripts/run_codex_scenario.py \
        --mode sub2api-http --scenario "$scenario" --model "$codex_model" \
        --output-dir "$output_dir" --timeout 70
      ;;
    codex-ws)
      docker exec -e SUB2API_API_KEY="$api_key" "$capture_container" \
        python3 /capture/scripts/run_codex_scenario.py \
        --mode sub2api-ws --scenario "$scenario" --model "$codex_model" \
        --output-dir "$output_dir" --timeout 70
      ;;
    codex-compact)
      if [[ $scenario != compact ]]; then
        echo "codex-compact 只接受 compact 场景。" >&2
        return 2
      fi
      docker exec -e SUB2API_API_KEY="$api_key" "$capture_container" \
        python3 "$capture_tool_root/run_codex_compact_scenario.py" \
        --mode sub2api-http --model "$codex_model" --codex-version "$codex_version" \
        --output-dir "$output_dir" --timeout 70
      ;;
    *)
      echo "未知主体：$subject" >&2
      return 2
      ;;
  esac

  stop_pair
  pcap="$capture_root/runs/$run_id/direct/$case_id/egress.pcap"
  if [[ ! -s $pcap ]]; then
    echo "direct pcap 缺失或为空：$pcap" >&2
    return 1
  fi
}

for subject in $subjects; do
  for scenario in $scenarios; do
    run_case "$subject" "$scenario"
  done
done

db_query "select account_id || '|' || requested_model || '|' || openai_ws_mode || '|' || count(*) from usage_logs where created_at >= '$started_at'::timestamptz group by account_id,requested_model,openai_ws_mode order by account_id,openai_ws_mode"

python3 - \
  "$capture_root/runs/$run_id" "$run_id" "$subjects" "$scenarios" \
  "$claude_model" "$codex_model" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
run_id = sys.argv[2]
subjects = sys.argv[3].split()
scenarios = sys.argv[4].split()
claude_model = sys.argv[5]
codex_model = sys.argv[6]
cases = []
for subject in subjects:
    for scenario in scenarios:
        summary_path = root / "result" / subject / scenario / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        pcap_path = root / "direct" / f"{subject}-{scenario}" / "egress.pcap"
        cases.append(
            {
                "subject": subject,
                "scenario": scenario,
                "valid": bool(summary.get("valid")),
                "pcap_bytes": pcap_path.stat().st_size,
                "pcap_sha256": hashlib.sha256(pcap_path.read_bytes()).hexdigest(),
            }
        )
if not all(item["valid"] and item["pcap_bytes"] > 24 for item in cases):
    raise SystemExit("direct 场景或 pcap 校验失败")
payload = {
    "schema_version": "sub2api-direct-capture/v1",
    "run_id": run_id,
    "status": "complete",
    "models": {
        "claude": claude_model,
        "codex": codex_model,
    },
    "cases": cases,
}
path = root / "run-summary.json"
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
print(json.dumps(payload, ensure_ascii=False))
PY

printf 'run_id=%s\n' "$run_id"
