#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 采集候选出站在 HTTP/1.1 上的原始请求形态（header 大小写与顺序）。
#
# 做法是把 chatgpt.com 指向本机探针而非配置代理：Sub2API 认为自己在直连，于是使用
# 空 ALPN 的直连画像，服务端据此协商 h1。经代理的 MITM 抓包一律是 h2，HPACK 强制
# 小写并重排 header，因此那条路永远看不到这两项差异。
#
# 退出时恢复 hosts、CA 与 keeper。探针不转发到真实上游，不消耗配额。

service_container=${SERVICE_CONTAINER:-sub2apiplus}
keeper_container=${KEEPER_CONTAINER:-sub2apiplus-keeper}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
account_id=${ACCOUNT_ID:?必须提供 ACCOUNT_ID}
api_key_id=${API_KEY_ID:-1}
image_model=${IMAGE_MODEL:-gpt-image-1}
model=${MODEL:-gpt-5.6-luna}
capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}
service_port=${SERVICE_PORT:-}
run_id=${RUN_ID:?必须提供 RUN_ID}
window_id=$(date -u +%Y%m%dT%H%M%SZ)

if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

work_dir="$capture_root/runs/$run_id"
tls_dir="$work_dir/tls"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
custom_ca_path=/usr/local/share/ca-certificates/h1-wire-probe.crt
hosts_backup="$capture_root/runtime/hosts.before-$window_id"
ca_backup="$capture_root/runtime/ca-certificates.crt.before-h1-$window_id"

probe_started=0
ca_installed=0
model_mapping_restore_armed=0
original_model_mapping_state=""
group_image_restore_armed=0
original_group_allow_image_generation=""
hosts_patched=0
keeper_was_running=false
account_gate_before=""

# 探针把 chatgpt.com 劫持到容器内端口；探针停止后仍在途的真实出站会拿到
# connection refused，Sub2API 据此把账号临时熔断。熔断是本脚本自身的副作用，
# 不恢复就会让同一 attempt 的后续任务全部 503，因此按运行前值精确回写。
restore_account_gate() {
  local until_hex reason_hex
  [[ -n $account_gate_before ]] || return 0
  until_hex=${account_gate_before%%|*}
  reason_hex=${account_gate_before##*|}
  [[ $until_hex =~ ^[0-9a-f]*$ && $reason_hex =~ ^[0-9a-f]*$ ]] || return 1
  db_query "update accounts set temp_unschedulable_until = nullif(convert_from(decode('$until_hex','hex'),'UTF8'),'')::timestamptz, temp_unschedulable_reason = nullif(convert_from(decode('$reason_hex','hex'),'UTF8'),'') where id = $account_id" >/dev/null
}

# 劫持生效后，Sub2API 的任何出站（含后台任务）都会打到只接受特定请求的探针上；被拒的
# 连接会让账号进入临时熔断，紧接着要采集的请求就拿到 503。运行期间主动清一次熔断，
# 退出时仍按 account_gate_before 的原值恢复，真实故障照旧以请求失败的形式暴露。
clear_account_gate() {
  db_query "update accounts set temp_unschedulable_until = null, temp_unschedulable_reason = null where id = $account_id" >/dev/null
}

account_gate_state() {
  db_query "select coalesce(encode(convert_to(coalesce(temp_unschedulable_until::text,''),'UTF8'),'hex'),'') || '|' || coalesce(encode(convert_to(coalesce(temp_unschedulable_reason,''),'UTF8'),'hex'),'') from accounts where id = $account_id"
}

cleanup() {
  local status=$?
  if [[ $probe_started == 1 ]]; then
    docker exec "$capture_container" pkill -f h1_wire_probe.py >/dev/null 2>&1 || true
  fi
  if [[ $hosts_patched == 1 ]]; then
    docker exec "$service_container" sh -c 'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hosts.restore && cat /tmp/.hosts.restore > /etc/hosts && rm -f /tmp/.hosts.restore' >/dev/null 2>&1 || true
  fi
  if [[ $ca_installed == 1 ]]; then
    docker exec "$service_container" rm -f "$custom_ca_path" >/dev/null 2>&1 || true
    docker exec "$service_container" update-ca-certificates --fresh >/dev/null 2>&1 || true
  fi
  # 分组图片权限是入口级门禁，必须在最终重启前按原值恢复。这样服务重启后加载的
  # 一定是原始权限，不能把验收期间的临时 true 留在认证缓存中。
  if [[ $group_image_restore_armed == 1 ]]; then
    if ! db_query "update groups set allow_image_generation = $original_group_allow_image_generation
      where id = $group_id" >/dev/null 2>&1; then
      status=97
    elif [[ $(db_query "select allow_image_generation::text from groups where id = $group_id") != "$original_group_allow_image_generation" ]]; then
      echo "API Key #$api_key_id 所属分组的图片权限未能按原值恢复。" >&2
      status=97
    fi
  fi
  if [[ $ca_installed == 1 ]]; then
    docker restart "$service_container" >/dev/null 2>&1 || true
    for _ in $(seq 1 90); do
      local health
      health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service_container" 2>/dev/null || echo "")
      [[ $health == healthy || $health == running ]] && break
      sleep 1
    done
  fi
  if [[ $keeper_was_running == true ]]; then
    docker start "$keeper_container" >/dev/null 2>&1 || true
  fi
  # 图片端点要求图片模型出现在账号的显式 model_mapping 白名单里，否则请求在入口
  # 就被判 model_not_found、根本不出站，h1 探针永远等不到数据。只恢复该字段，不
  # 重写 credentials 中的 OAuth 凭据。
  if [[ $model_mapping_restore_armed == 1 ]]; then
    case "$original_model_mapping_state" in
      present:*)
        db_query "update accounts set credentials = jsonb_set(
          coalesce(credentials,'{}'::jsonb),
          '{model_mapping}',
          convert_from(decode('${original_model_mapping_state#present:}','hex'),'UTF8')::jsonb,
          true
        ) where id = $account_id" >/dev/null 2>&1 || status=97
        ;;
      missing:)
        db_query "update accounts set credentials = coalesce(credentials,'{}'::jsonb) - 'model_mapping'
          where id = $account_id" >/dev/null 2>&1 || status=97
        ;;
      *)
        echo "账号 #$account_id 的 model_mapping 原值缺失，无法恢复。" >&2
        status=97
        ;;
    esac
  fi
  if ! restore_account_gate; then
    echo "账号 #$account_id 的临时熔断状态未能恢复，请人工检查。" >&2
    status=97
  elif [[ -n $account_gate_before && $(account_gate_state) != "$account_gate_before" ]]; then
    echo "账号 #$account_id 的临时熔断状态与运行前不一致。" >&2
    status=97
  fi
  echo "环境已恢复：hosts、CA、keeper、账号调度门、分组图片权限与 model_mapping 均回到采集前状态。"
  exit $status
}
trap cleanup EXIT

db_user=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" | sed -n 's/^POSTGRES_USER=//p')
db_name=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" | sed -n 's/^POSTGRES_DB=//p')
db_query() { docker exec "$postgres_container" psql -U "$db_user" -d "$db_name" -qAtc "$1"; }

current_proxy=$(db_query "select coalesce(proxy_id::text,'NULL') from accounts where id = $account_id")
if [[ $current_proxy != NULL ]]; then
  echo "账号 #$account_id 已绑定代理，探针要求直连画像，拒绝继续。" >&2
  exit 1
fi
account_shape=$(db_query \
  "select platform || '|' || type || '|' || coalesce(parent_account_id::text,'NULL') from accounts where id = $account_id")
if [[ $account_shape != "openai|oauth|NULL" ]]; then
  echo "账号 #$account_id 不是非影子的 OpenAI OAuth 专用账号，拒绝继续。" >&2
  exit 1
fi
account_gate_before=$(account_gate_state)
if [[ ! $account_gate_before =~ ^[0-9a-f]*\|[0-9a-f]*$ ]]; then
  echo "无法读取账号 #$account_id 的调度门状态，拒绝继续。" >&2
  exit 1
fi

# 图片入口同样通过 API Key 分组选择 OAuth 账号。必须在修改 model_mapping、CA 或 hosts
# 之前证明分组只会调度 ACCOUNT_ID，避免验收请求落到其他账号。
api_key=$(db_query "select key from api_keys where id = $api_key_id")
group_id=$(db_query "select group_id from api_keys where id = $api_key_id")
token_present=$(db_query \
  "select case when length(coalesce(credentials->>'access_token','')) > 0 then 'true' else 'false' end from accounts where id = $account_id")
if [[ -z $api_key || ! $group_id =~ ^[0-9]+$ || $token_present != true ]]; then
  echo "API Key/分组不存在，或账号 #$account_id 缺少当前 access token。" >&2
  exit 1
fi
eligible_shape=$(db_query "
select count(*)::text || '|' || count(*) filter (where a.id = $account_id)::text
from account_groups ag
join accounts a on a.id = ag.account_id
where ag.group_id = $group_id
  and a.platform = 'openai'
  and a.type = 'oauth'
  and a.status = 'active'
  and a.schedulable = true")
if [[ $eligible_shape != "1|1" ]]; then
  echo "API Key #$api_key_id 的分组不是账号 #$account_id 的单账号隔离分组，拒绝继续。" >&2
  exit 1
fi
active_api_key_shape=$(db_query "
select count(*)::text || '|' || count(*) filter (where id = $api_key_id)::text
from api_keys
where group_id = $group_id
  and status = 'active'
  and deleted_at is null")
if [[ $active_api_key_shape != "1|1" ]]; then
  echo "分组 #$group_id 不是 API Key #$api_key_id 的单 Key 隔离分组，拒绝临时修改图片权限。" >&2
  exit 1
fi

install -d -m 0700 "$work_dir" "$tls_dir" "$capture_root/runtime"

# 用抓包 CA 签发 chatgpt.com 证书，使 Sub2API 在装入该 CA 后信任本探针。
openssl req -x509 -newkey rsa:2048 -sha256 -days 1 -nodes \
  -keyout "$tls_dir/probe.key" -out "$tls_dir/probe.crt" \
  -subj "/CN=chatgpt.com" \
  -addext "subjectAltName=DNS:chatgpt.com" \
  -CA "$ca_full" -CAkey "$ca_full" >/dev/null 2>&1 || {
  # 旧版 openssl 不支持 req -CA，退回 CSR + x509 两步签发。
  openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/probe.key" \
    -out "$tls_dir/probe.csr" -subj "/CN=chatgpt.com" >/dev/null 2>&1
  printf 'subjectAltName=DNS:chatgpt.com\n' > "$tls_dir/probe.ext"
  openssl x509 -req -in "$tls_dir/probe.csr" -CA "$ca_full" -CAkey "$ca_full" \
    -CAcreateserial -out "$tls_dir/probe.crt" -days 1 -sha256 \
    -extfile "$tls_dir/probe.ext" >/dev/null 2>&1
}
chmod 600 "$tls_dir"/*

# capture-cli 可能同时加入抓包专网和 Sub2API 业务网，Docker map 的第一个 IP 不保证
# 对 Sub2API 可达。由服务容器解析 capture-cli 别名，固定使用两者共享网络的 IPv4。
probe_ip=$(docker exec "$service_container" getent ahostsv4 "$capture_container" 2>/dev/null \
  | awk 'NR == 1 {print $1}' || true)
if [[ ! $probe_ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "无法从 $service_container 解析 $capture_container 的共享网络 IPv4 地址。" >&2
  exit 1
fi

keeper_was_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
if [[ $keeper_was_running == true ]]; then
  docker stop "$keeper_container" >/dev/null
fi

docker exec "$capture_container" mkdir -p "/capture/runs/$run_id/tls"
docker exec -d "$capture_container" python3 \
  "$capture_tool_root/h1_wire_probe.py" \
  --cert "/capture/runs/$run_id/tls/probe.crt" --key "/capture/runs/$run_id/tls/probe.key" \
  --port 443 --output "/capture/runs/$run_id/h1-wire.json" \
  --expect "${EXPECT_REQUESTS:-3}" --timeout 120 --idle-timeout 8 \
  --record-path-prefix /backend-api/codex/images/
probe_started=1
sleep 2

docker cp "$service_container:/etc/hosts" "$hosts_backup" >/dev/null
chmod 600 "$hosts_backup"
docker cp "$service_container:/etc/ssl/certs/ca-certificates.crt" "$ca_backup" >/dev/null
chmod 600 "$ca_backup"

# 图片模型必须先进入账号的显式 model_mapping 白名单，否则生图请求在入口就被判
# model_not_found（HTTP 404）、根本不会出站。原值先冻结成 hex，由 EXIT 钩子逐字回写。
if [[ ! $image_model =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "IMAGE_MODEL 含非法字符。" >&2
  exit 1
fi
original_model_mapping_state=$(db_query "select case
  when credentials ? 'model_mapping'
  then 'present:' || encode(convert_to((credentials->'model_mapping')::text,'UTF8'),'hex')
  else 'missing:' end from accounts where id = $account_id")
case "$original_model_mapping_state" in
  present:*|missing:) ;;
  *) echo "无法读取账号 #$account_id 的 model_mapping 初始状态。" >&2; exit 1 ;;
esac
model_mapping_restore_armed=1
db_query "update accounts set credentials = jsonb_set(
  coalesce(credentials,'{}'::jsonb),
  '{model_mapping}',
  coalesce(credentials->'model_mapping','{}'::jsonb) ||
    jsonb_build_object('$image_model','$image_model'),
  true
) where id = $account_id" >/dev/null

# `/v1/images/*` 在选择账号前先检查分组权限。专用分组已经同时通过单 Key、单账号
# 隔离门禁，因此只在本次窗口临时启用，并由 EXIT 钩子在最终重启前按原值恢复。
original_group_allow_image_generation=$(db_query \
  "select allow_image_generation::text from groups where id = $group_id")
if [[ $original_group_allow_image_generation != true && $original_group_allow_image_generation != false ]]; then
  echo "无法读取分组 #$group_id 的图片权限初始状态。" >&2
  exit 1
fi
group_image_restore_armed=1
db_query "update groups set allow_image_generation = true where id = $group_id" >/dev/null

docker cp "$ca_cert" "$service_container:$custom_ca_path" >/dev/null
docker exec "$service_container" update-ca-certificates >/dev/null 2>&1
ca_installed=1

# restart 返回后立即写 hosts；若等到健康检查完成，启动期模型刷新会先连真实上游并
# 缓存连接，之后的图片请求即使看到新 hosts 也可能绕过探针。
docker restart "$service_container" >/dev/null
docker exec "$service_container" sh -c 'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hosts.pre && cat /tmp/.hosts.pre > /etc/hosts && rm -f /tmp/.hosts.pre'
docker exec "$service_container" sh -c "printf '%s chatgpt.com\n' '$probe_ip' >> /etc/hosts"
hosts_patched=1
for _ in $(seq 1 90); do
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service_container")
  [[ $health == healthy ]] && break
  sleep 1
done

docker exec "$service_container" sh -c "grep chatgpt.com /etc/hosts" >/dev/null
clear_account_gate

if [[ -z $service_port ]]; then
  service_port=$(docker port "$service_container" 2>/dev/null \
    | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -1)
fi
if [[ ! $service_port =~ ^[0-9]+$ ]]; then
  echo "无法解析 $service_container 的宿主机发布端口；可显式设置 SERVICE_PORT。" >&2
  exit 1
fi
# 生图入口：验证 images 模板的 body 形态（tool_choice 等），普通 responses 请求
# 覆盖不到该模板。
curl -s -m 60 -o /dev/null -X POST "http://127.0.0.1:$service_port/v1/images/generations" \
  -H "Authorization: Bearer $api_key" \
  -H "Content-Type: application/json" \
  -H "User-Agent: h1-wire-probe/1.0" \
  -d "{\"model\":\"$image_model\",\"prompt\":\"a red fox\",\"n\":1,\"size\":\"1024x1024\"}" || true

for _ in $(seq 1 30); do
  docker exec "$capture_container" test -f "/capture/runs/$run_id/h1-wire.json" && break
  sleep 1
done
docker exec "$capture_container" cat "/capture/runs/$run_id/h1-wire.json"
printf 'run_id=%s\n' "$run_id"
