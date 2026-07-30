#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# R11 正式 A01 direct 抓包外层安全包装。
#
# 本脚本只允许在已部署的 R11 normal 容器上执行，不切换镜像、不重建容器，
# 仅临时把 group #9 的 platform 从 composite 改为 openai。退出钩子会恢复
# group、账号调度状态、SHA-256 API Key 认证缓存，并核对所有容器与镜像身份。

service_container=${SERVICE_CONTAINER:-sub2apiplus}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
redis_container=${REDIS_CONTAINER:-sub2apiplus-redis}
keeper_container=${KEEPER_CONTAINER:-sub2apiplus-keeper}
capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
r11_source_root=${R11_SOURCE_ROOT:?必须提供 R11_SOURCE_ROOT}
expected_r11_image_id=${EXPECTED_R11_IMAGE_ID:?必须提供 EXPECTED_R11_IMAGE_ID}
expected_r11_image_ref=${EXPECTED_R11_IMAGE_REF:?必须提供 EXPECTED_R11_IMAGE_REF}
run_id=${RUN_ID:?必须提供唯一的 RUN_ID}

group_id=9
claude_account_id=50
codex_account_id=99
api_key_id=15
codex_model=gpt-5.6-sol
codex_version=0.145.0
expected_direct_script_sha256=e69b0363fa2b9cf584a9a0b22a54f9312ad131e03f2dc609f6327c816dc6fb78
direct_script="$r11_source_root/tools/official_client_capture/run_sub2api_direct_matrix.sh"
pcap_parser="$r11_source_root/tools/official_client_capture/pcap_clienthello.py"

die() {
  echo "$1" >&2
  exit 1
}

if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  die "RUN_ID 只能包含字母、数字、点、下划线和连字符。"
fi
if [[ ! $expected_r11_image_id =~ ^sha256:[0-9a-f]{64}$ ]]; then
  die "EXPECTED_R11_IMAGE_ID 格式非法。"
fi
if [[ $expected_r11_image_ref == *[[:space:]]* || $expected_r11_image_ref != *-r11 ]]; then
  die "EXPECTED_R11_IMAGE_REF 必须是以 -r11 结尾的不可变镜像标签。"
fi
if [[ $r11_source_root != /* || ! -d $r11_source_root ]]; then
  die "R11_SOURCE_ROOT 必须是存在的绝对目录。"
fi
if [[ ! -f $direct_script || -L $direct_script || ! -f $pcap_parser || -L $pcap_parser ]]; then
  die "R11 抓包工具缺失或是符号链接。"
fi
actual_direct_script_sha256=$(sha256sum "$direct_script" | awk '{print $1}')
if [[ $actual_direct_script_sha256 != "$expected_direct_script_sha256" ]]; then
  die "R11 direct 抓包脚本摘要不匹配。"
fi
if [[ -e $capture_root/runs/$run_id ]]; then
  die "正式抓包目录已经存在，拒绝覆盖。"
fi

db_user=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_USER=//p'
)
db_name=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_DB=//p'
)
if [[ -z $db_user || -z $db_name ]]; then
  die "无法读取 PostgreSQL 非敏感连接元数据。"
fi

db_query() {
  docker exec "$postgres_container" \
    psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" -qAtc "$1"
}

wait_healthy() {
  local health
  for _ in $(seq 1 90); do
    health=$(docker inspect -f \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$service_container" 2>/dev/null || true)
    if [[ $health == healthy ]]; then
      return 0
    fi
    sleep 1
  done
  echo "R11 normal 服务未在 90 秒内恢复 healthy。" >&2
  return 1
}

check_no_capture_environment() {
  if docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" |
    awk 'index($0, "SUB2API_LIVE_ATTESTATION_CAPTURE_") == 1 { found=1 } END { exit found ? 0 : 1 }'; then
    echo "R11 normal 容器意外携带 Live candidate-capture 环境变量。" >&2
    return 1
  fi
}

check_normal_runtime() {
  local current_image_id current_image_ref
  current_image_id=$(docker inspect -f '{{.Image}}' "$service_container" 2>/dev/null || true)
  current_image_ref=$(docker inspect -f '{{.Config.Image}}' "$service_container" 2>/dev/null || true)
  if [[ $current_image_id != "$expected_r11_image_id" ||
        $current_image_ref != "$expected_r11_image_ref" ]]; then
    echo "当前服务不是指定的 R11 normal 镜像。" >&2
    return 1
  fi
  check_no_capture_environment || return 1
  docker exec "$service_container" test ! -e \
    /usr/local/share/ca-certificates/candidate-core-capture.crt || return 1
  docker exec "$service_container" test ! -e \
    /usr/local/share/ca-certificates/candidate-aux-capture.crt || return 1
  if docker exec "$service_container" grep -Eq \
    '(^|[[:space:]])chatgpt\.com([[:space:]]|$)' /etc/hosts; then
    echo "R11 normal 容器仍存在 chatgpt.com hosts 劫持。" >&2
    return 1
  fi
}

invalidate_api_key_auth_cache() {
  local deleted published
  deleted=$(
    printf 'apikey:auth:%s' "$auth_digest" |
      docker exec -i "$redis_container" redis-cli --raw -x DEL 2>/dev/null
  ) || return 1
  [[ $deleted =~ ^[01]$ ]] || return 1
  published=$(
    printf '%s' "$auth_digest" |
      docker exec -i "$redis_container" redis-cli --raw -x \
        PUBLISH auth:cache:invalidate 2>/dev/null
  ) || return 1
  [[ $published =~ ^[0-9]+$ ]]
}

auth_cache_absent() {
  local exists
  exists=$(
    printf 'apikey:auth:%s' "$auth_digest" |
      docker exec -i "$redis_container" redis-cli --raw -x EXISTS 2>/dev/null
  ) || return 1
  [[ $exists == 0 ]]
}

wait_healthy
check_normal_runtime

service_container_id=$(docker inspect -f '{{.Id}}' "$service_container")
postgres_container_id=$(docker inspect -f '{{.Id}}' "$postgres_container")
redis_container_id=$(docker inspect -f '{{.Id}}' "$redis_container")
keeper_container_id=$(docker inspect -f '{{.Id}}' "$keeper_container")
capture_container_id=$(docker inspect -f '{{.Id}}' "$capture_container")
for container in "$postgres_container" "$redis_container" "$keeper_container" "$capture_container"; do
  if [[ $(docker inspect -f '{{.State.Running}}' "$container") != true ]]; then
    die "正式 A01 前置容器未运行。"
  fi
done

baseline_group_state=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups
where id = $group_id and status = 'active' and deleted_at is null")
if [[ $baseline_group_state != 'composite|false|true|false' ]]; then
  die "group #9 基线不是预期的 composite 版本画像。"
fi

api_key_state=$(db_query "
select id::text || '|' || group_id::text || '|' || status
from api_keys
where id = $api_key_id and deleted_at is null")
if [[ $api_key_state != '15|9|active' ]]; then
  die "API Key #15 的非敏感状态不符合正式验收前提。"
fi

# 直接使用仓库迁移已使用的 PostgreSQL sha256(bytea)，只取得摘要，
# 不把 API Key 明文读入外层 shell，也不依赖未安装的 pgcrypto digest()。
auth_digest=$(db_query "
select encode(sha256(convert_to(key, 'UTF8')), 'hex')
from api_keys
where id = $api_key_id and group_id = $group_id
  and status = 'active' and deleted_at is null")
if [[ ! $auth_digest =~ ^[0-9a-f]{64}$ ]]; then
  die "无法安全取得 API Key #15 的 SHA-256 摘要。"
fi

codex_account_shape=$(db_query "
select platform || '|' || type || '|' || status || '|' ||
       coalesce(parent_account_id::text, 'NULL')
from accounts
where id = $codex_account_id and deleted_at is null")
if [[ $codex_account_shape != 'openai|oauth|active|NULL' ]]; then
  die "账号 #99 不是正式验收所需的非影子 OpenAI OAuth 账号。"
fi

baseline_claude_schedulable=$(db_query \
  "select schedulable::text from accounts where id = $claude_account_id")
baseline_codex_schedulable=$(db_query \
  "select schedulable::text from accounts where id = $codex_account_id")
if [[ ! $baseline_claude_schedulable =~ ^(true|false)$ ||
      ! $baseline_codex_schedulable =~ ^(true|false)$ ]]; then
  die "无法精确读取账号 #50/#99 的初始调度状态。"
fi
baseline_proxy_state=$(db_query "
select id::text || '|' || coalesce(proxy_id::text, 'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text, 'NULL')
from accounts
where id in ($claude_account_id, $codex_account_id)
order by id")
expected_proxy_state=$(printf '%s\n%s' \
  "$claude_account_id|NULL|NULL" "$codex_account_id|NULL|NULL")
if [[ $baseline_proxy_state != "$expected_proxy_state" ]]; then
  die "账号 #50/#99 存在 proxy 或 fallback，拒绝覆盖。"
fi

# direct 脚本会临时启用 #99；这里提前证明届时 group #9 中唯一可调度的
# 活跃 OpenAI OAuth 主账号仍然是 #99，避免抓包串到其他生产账号。
eligible_codex_accounts=$(db_query "
select coalesce(string_agg(a.id::text, ',' order by a.id), '')
from account_groups ag
join accounts a on a.id = ag.account_id
where ag.group_id = $group_id
  and a.platform = 'openai'
  and a.type = 'oauth'
  and a.status = 'active'
  and a.deleted_at is null
  and a.parent_account_id is null
  and (a.schedulable = true or a.id = $codex_account_id)")
if [[ $eligible_codex_accounts != "$codex_account_id" ]]; then
  die "group #9 的 OpenAI OAuth 账号不是 #99 单账号隔离形态。"
fi

restore_armed=1
restore_environment() {
  local original_exit_code=$?
  local restore_failed=0
  local current_group_state restored_count
  trap - EXIT ERR INT TERM
  set +e

  if [[ $restore_armed == 1 ]]; then
    current_group_state=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id = $group_id and deleted_at is null" 2>/dev/null)
    case "$current_group_state" in
      'openai|false|true|false')
        restored_count=$(db_query "
with restored as (
  update groups
  set platform = 'composite'
  where id = $group_id
    and platform = 'openai'
    and require_oauth_only = false
    and allow_live = true
    and allow_image_generation = false
    and deleted_at is null
  returning id
)
select count(*) from restored" 2>/dev/null)
        [[ $restored_count == 1 ]] || restore_failed=1
        ;;
      'composite|false|true|false')
        ;;
      *)
        restore_failed=1
        ;;
    esac

    db_query "
update accounts
set schedulable = case id
  when $claude_account_id then $baseline_claude_schedulable
  when $codex_account_id then $baseline_codex_schedulable
end
where id in ($claude_account_id, $codex_account_id)" \
      >/dev/null 2>&1 || restore_failed=1

    invalidate_api_key_auth_cache || restore_failed=1
    docker restart "$service_container" >/dev/null 2>&1 || restore_failed=1
    wait_healthy || restore_failed=1
  fi

  [[ $(docker inspect -f '{{.Id}}' "$service_container" 2>/dev/null) == "$service_container_id" ]] || restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$postgres_container" 2>/dev/null) == "$postgres_container_id" ]] || restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$redis_container" 2>/dev/null) == "$redis_container_id" ]] || restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$keeper_container" 2>/dev/null) == "$keeper_container_id" ]] || restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$capture_container" 2>/dev/null) == "$capture_container_id" ]] || restore_failed=1
  for container in "$postgres_container" "$redis_container" "$keeper_container" "$capture_container"; do
    [[ $(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null) == true ]] || restore_failed=1
  done

  current_group_state=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id = $group_id and deleted_at is null" 2>/dev/null)
  [[ $current_group_state == "$baseline_group_state" ]] || restore_failed=1
  [[ $(db_query "select schedulable::text from accounts where id = $claude_account_id" 2>/dev/null) == "$baseline_claude_schedulable" ]] || restore_failed=1
  [[ $(db_query "select schedulable::text from accounts where id = $codex_account_id" 2>/dev/null) == "$baseline_codex_schedulable" ]] || restore_failed=1
  [[ $(db_query "
select id::text || '|' || coalesce(proxy_id::text, 'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text, 'NULL')
from accounts where id in ($claude_account_id, $codex_account_id)
order by id" 2>/dev/null) == "$baseline_proxy_state" ]] || restore_failed=1
  check_normal_runtime || restore_failed=1
  auth_cache_absent || restore_failed=1

  auth_digest=''
  if [[ $restore_failed != 0 ]]; then
    echo "A01 外层环境恢复或状态门禁失败，请保持现场；退出码 97。" >&2
    exit 97
  fi
  echo "A01 外层环境已精确恢复：R11 normal 镜像、group、认证缓存、账号与数据容器均通过门禁。"
  exit "$original_exit_code"
}
trap restore_environment EXIT ERR INT TERM

changed_count=$(db_query "
with changed as (
  update groups
  set platform = 'openai'
  where id = $group_id
    and platform = 'composite'
    and require_oauth_only = false
    and allow_live = true
    and allow_image_generation = false
    and deleted_at is null
  returning id
)
select count(*) from changed")
if [[ $changed_count != 1 ]]; then
  die "group #9 临时切换没有精确命中一行。"
fi
invalidate_api_key_auth_cache
docker restart "$service_container" >/dev/null
wait_healthy
check_normal_runtime
if [[ $(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id = $group_id and deleted_at is null") != 'openai|false|true|false' ]]; then
  die "group #9 临时 OpenAI 形态没有生效。"
fi

log_root="$capture_root/runtime/formal-a01-r11"
install -d -m 0700 "$log_root"
direct_log="$log_root/$run_id.direct.log"
if [[ -e $direct_log ]]; then
  die "A01 direct 日志已经存在，拒绝覆盖。"
fi
install -m 0600 /dev/null "$direct_log"

env -u BASH_ENV -u ENV -u BASH_XTRACEFD \
  CAPTURE_CONTAINER="$capture_container" \
  SERVICE_CONTAINER="$service_container" \
  POSTGRES_CONTAINER="$postgres_container" \
  CAPTURE_ROOT="$capture_root" \
  RUN_ID="$run_id" \
  SUBJECTS=codex-http \
  SCENARIOS=s1 \
  CLAUDE_ACCOUNT_ID="$claude_account_id" \
  CODEX_ACCOUNT_ID="$codex_account_id" \
  API_KEY_ID="$api_key_id" \
  CODEX_MODEL="$codex_model" \
  CODEX_VERSION="$codex_version" \
  bash --noprofile --norc "$direct_script" >"$direct_log" 2>&1

run_root="$capture_root/runs/$run_id"
pcap="$run_root/direct/codex-http-s1/egress.pcap"
python3 - "$run_root" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
summary_path = root / "run-summary.json"
pcap = root / "direct/codex-http-s1/egress.pcap"
if not summary_path.is_file() or not pcap.is_file() or pcap.stat().st_size <= 24:
    raise SystemExit("A01 direct 汇总或有效 pcap 缺失")
summary = json.loads(summary_path.read_text(encoding="utf-8"))
expected_case = {
    "subject": "codex-http",
    "scenario": "s1",
}
cases = summary.get("cases")
if summary.get("status") != "complete" or not isinstance(cases, list) or len(cases) != 1:
    raise SystemExit("A01 direct 汇总状态或 case 数量非法")
case = cases[0]
if any(case.get(key) != value for key, value in expected_case.items()):
    raise SystemExit("A01 direct case 身份不匹配")
if case.get("valid") is not True or case.get("pcap_bytes", 0) <= 24:
    raise SystemExit("A01 direct case 未通过脚本内门禁")
PY

# 在最终 manifest/assertion 之前先做同一冻结解析器的 fail-closed 预检：
# 至少存在 chatgpt.com ClientHello，且本 pcap 内每条 ClientHello 都是
# 30 cipher、无 ALPN 扩展、无 ALPN offer 的默认系统 CA HTTP 画像。
python3 - "$pcap_parser" "$pcap" <<'PY'
import runpy
import sys
from pathlib import Path

module = runpy.run_path(sys.argv[1], run_name="candidate_pcap_parser")
hellos = []
for linktype, packet in module["iter_packets"](Path(sys.argv[2])):
    parsed_tcp = module["tcp_payload"](linktype, packet)
    if parsed_tcp is None:
        continue
    parsed_hello = module["parse_client_hello"](parsed_tcp[2])
    if parsed_hello is not None:
        hellos.append(parsed_hello)
if not hellos:
    raise SystemExit("A01 pcap 没有可解析 ClientHello")
if not any(sni == "chatgpt.com" for sni, _, _, _ in hellos):
    raise SystemExit("A01 pcap 缺少 chatgpt.com SNI")
if any(len(ciphers) != 30 or 16 in extensions or alpn
       for _, extensions, ciphers, alpn in hellos):
    raise SystemExit("A01 pcap 存在非默认系统 CA HTTP ClientHello")
PY

echo "A01 direct 抓包完成并通过前置线形态检查；退出钩子将立即恢复正式环境。"
