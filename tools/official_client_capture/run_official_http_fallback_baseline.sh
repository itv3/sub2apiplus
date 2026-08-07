#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 采集**官方 Codex CLI 降级到 HTTP 后** POST /responses 的 h1 形态。
#
# 官方默认走 WebSocket，HTTP 是 force_http_fallback 的降级路径（core/src/client.rs:509）。
# 触发条件是 **WS 连接被拒**（Connection refused）——此前试过让探针对握手回 HTTP 400，
# 官方走的是错误退出而非降级，采不到。让 TCP 连接直接被拒才会打印
# `Falling back from WebSockets to HTTPS transport`。
#
# 做法：hosts 把 chatgpt.com 指向探针（h1，不支持 WS 升级），官方 WS 握手失败后降级，
# 随后的 POST /responses 即被 h1 探针记录。
#
# 本场景不经代理，而是临时修改 hosts 并把抓包 CA 安装进系统信任库，使官方客户端仍走
# native-tls 的 HTTP/1.1 分支。探针只返回受控响应，不转发真实上游，因此不消耗账号配额。

capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}
run_id=${RUN_ID:?必须提供 RUN_ID}
model=${MODEL:-gpt-5.6-luna}
expect_connections=${EXPECT_CONNECTIONS:-3}
codex_bin=${CODEX_BIN:-/root/.local/bin/codex}

if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

work_dir="$capture_root/runs/$run_id"
tls_dir="$work_dir/tls"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"

probe_started=0

cleanup() {
  local status=$?
  if [[ $probe_started == 1 ]]; then
    docker exec "$capture_container" pkill -f h1_wire_probe.py >/dev/null 2>&1 || true
  fi
  docker exec "$capture_container" sh -c 'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hr && cat /tmp/.hr > /etc/hosts && rm -f /tmp/.hr' >/dev/null 2>&1 || true
  docker exec "$capture_container" rm -f /usr/local/share/ca-certificates/mitm-baseline.crt >/dev/null 2>&1 || true
  docker exec "$capture_container" update-ca-certificates --fresh >/dev/null 2>&1 || true
  echo "环境已恢复：探针已停止，hosts 与系统信任库中的临时 CA 均已还原。"
  exit $status
}
trap cleanup EXIT

install -d -m 0700 "$work_dir" "$tls_dir"

openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/probe.key" \
  -out "$tls_dir/probe.csr" -subj "/CN=chatgpt.com" >/dev/null 2>&1
printf 'subjectAltName=DNS:chatgpt.com\n' > "$tls_dir/probe.ext"
openssl x509 -req -in "$tls_dir/probe.csr" -CA "$ca_full" -CAkey "$ca_full" \
  -CAcreateserial -out "$tls_dir/probe.crt" -days 1 -sha256 \
  -extfile "$tls_dir/probe.ext" >/dev/null 2>&1
chmod 600 "$tls_dir"/*

docker exec -d -e H1_PROBE_DROP_WS=1 "$capture_container" python3 \
  "$capture_tool_root/h1_wire_probe.py" \
  --cert "/capture/runs/$run_id/tls/probe.crt" --key "/capture/runs/$run_id/tls/probe.key" \
  --port 443 --output "/capture/runs/$run_id/h1-wire.json" \
  --expect "$expect_connections" --timeout 90 --idle-timeout 10
probe_started=1
sleep 2

# 关键差异：**不设** CODEX_CA_CERTIFICATE / SSL_CERT_FILE。
# 官方 custom_ca.rs:398 的 configured_ca_bundle() 一旦读到这两个变量，就会调用
# use_rustls_tls() 切到 rustls——那正是此前 h2 基线被污染的成因。改为把抓包 CA
# 装进容器系统信任库，native-tls 走 OpenSSL 系统根，既信任探针又不触发该分支。
docker cp "$ca_cert" "$capture_container:/usr/local/share/ca-certificates/mitm-baseline.crt" >/dev/null
docker exec "$capture_container" update-ca-certificates >/dev/null 2>&1

# hosts 指向探针（顺序：先起探针，再改 hosts）
docker exec "$capture_container" sh -c 'grep -v " chatgpt.com$" /etc/hosts > /tmp/.h && cat /tmp/.h > /etc/hosts && rm -f /tmp/.h'
docker exec "$capture_container" sh -c "printf '127.0.0.1 chatgpt.com\n' >> /etc/hosts"

docker exec \
  "$capture_container" timeout 60 "$codex_bin" exec \
  --model "$model" --skip-git-repo-check "只回复 H2_BASELINE_OK" >/dev/null 2>&1 || true

for _ in $(seq 1 60); do
  docker exec "$capture_container" test -f "/capture/runs/$run_id/h1-wire.json" && break
  sleep 1
done
docker exec "$capture_container" cat "/capture/runs/$run_id/h1-wire.json"
printf 'run_id=%s\n' "$run_id"
