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
model=${MODEL:-gpt-5.6-luna}
capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}
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
  if ! restore_account_gate; then
    echo "账号 #$account_id 的临时熔断状态未能恢复，请人工检查。" >&2
    status=97
  elif [[ -n $account_gate_before && $(account_gate_state) != "$account_gate_before" ]]; then
    echo "账号 #$account_id 的临时熔断状态与运行前不一致。" >&2
    status=97
  fi
  echo "环境已恢复：hosts、CA、keeper 与账号调度门均回到采集前状态。"
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
account_gate_before=$(account_gate_state)
if [[ ! $account_gate_before =~ ^[0-9a-f]*\|[0-9a-f]*$ ]]; then
  echo "无法读取账号 #$account_id 的调度门状态，拒绝继续。" >&2
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

# 探针跑在 capture-cli 容器内：宿主机 443 已被占用，而 capture-cli 与 Sub2API 同网络，
# 容器内 443 空闲，hosts 指向它即可让 Sub2API 以为在直连 chatgpt.com。
probe_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$capture_container" | awk '{print $1}')
if [[ -z $probe_ip ]]; then
  echo "无法解析 $capture_container 的容器地址。" >&2
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
  --port 443 --output "/capture/runs/$run_id/h1-wire.json" --expect "${EXPECT_REQUESTS:-3}" --timeout 120 --idle-timeout 8
probe_started=1
sleep 2

docker cp "$service_container:/etc/hosts" "$hosts_backup" >/dev/null
chmod 600 "$hosts_backup"
docker cp "$service_container:/etc/ssl/certs/ca-certificates.crt" "$ca_backup" >/dev/null
chmod 600 "$ca_backup"

docker cp "$ca_cert" "$service_container:$custom_ca_path" >/dev/null
docker exec "$service_container" update-ca-certificates >/dev/null 2>&1
ca_installed=1

# 先重启让 CA 进入进程的根证书池，再改 hosts——docker restart 会重新生成
# /etc/hosts，顺序反了 hosts 会被冲掉。Go 的 resolver 每次解析都读该文件，
# 因此改完无需再次重启即可生效。
docker restart "$service_container" >/dev/null
for _ in $(seq 1 90); do
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service_container")
  [[ $health == healthy ]] && break
  sleep 1
done

docker exec "$service_container" sh -c 'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hosts.pre && cat /tmp/.hosts.pre > /etc/hosts && rm -f /tmp/.hosts.pre'
docker exec "$service_container" sh -c "printf '%s chatgpt.com\n' '$probe_ip' >> /etc/hosts"
hosts_patched=1
docker exec "$service_container" sh -c "grep chatgpt.com /etc/hosts" >/dev/null
clear_account_gate

api_key=$(db_query "select key from api_keys where id = $api_key_id")
port=$(docker port "$service_container" | sed -n 's/.*127.0.0.1:\([0-9]*\)/\1/p' | head -1)
# 生图入口：验证 images 模板的 body 形态（tool_choice 等），普通 responses 请求
# 覆盖不到该模板。
curl -s -m 60 -o /dev/null -X POST "http://127.0.0.1:${port:-3001}/v1/images/generations" \
  -H "Authorization: Bearer $api_key" \
  -H "Content-Type: application/json" \
  -H "User-Agent: h1-wire-probe/1.0" \
  -d '{"model":"gpt-image-1","prompt":"a red fox","n":1,"size":"1024x1024"}' || true

for _ in $(seq 1 30); do
  docker exec "$capture_container" test -f "/capture/runs/$run_id/h1-wire.json" && break
  sleep 1
done
docker exec "$capture_container" cat "/capture/runs/$run_id/h1-wire.json"
printf 'run_id=%s\n' "$run_id"
