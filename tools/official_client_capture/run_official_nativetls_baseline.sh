#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 采集**官方 Codex CLI 在 native-tls 默认分支下**经代理的形态。
#
# 为什么必须经代理：官方直连画像是空 ALPN，恒为 HTTP/1.1，根本不产生 h2 流量；只有
# 经代理时 reqwest 才换用含 h2 的 ClientHello。因此探针同时扮演 CONNECT 代理与 h2
# 服务端两个角色（见 h2_wire_probe.py）。
#
# 与 h1 基线脚本的区别：走代理不需要改 hosts，因此没有 hosts 污染的清理负担。
# 探针不转发上游，本脚本不产生真实业务请求，也不消耗账号配额。

capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
run_id=${RUN_ID:?必须提供 RUN_ID}
model=${MODEL:-gpt-5.6-luna}
probe_port=${PROBE_PORT:-18081}
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
    docker exec "$capture_container" pkill -f h2_wire_probe.py >/dev/null 2>&1 || true
  fi
  docker exec "$capture_container" rm -f /usr/local/share/ca-certificates/mitm-baseline.crt >/dev/null 2>&1 || true
  docker exec "$capture_container" update-ca-certificates --fresh >/dev/null 2>&1 || true
  echo "环境已恢复：探针已停止，系统信任库中的临时 CA 已移除。"
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

docker exec -d "$capture_container" python3 \
  /capture/tools/official_client_capture/h2_wire_probe.py \
  --cert "/capture/runs/$run_id/tls/probe.crt" --key "/capture/runs/$run_id/tls/probe.key" \
  --port "$probe_port" --output "/capture/runs/$run_id/h2-wire.json" \
  --expect "$expect_connections" --timeout 90 --idle-timeout 10
probe_started=1
sleep 2

# 关键差异：**不设** CODEX_CA_CERTIFICATE / SSL_CERT_FILE。
# 官方 custom_ca.rs:398 的 configured_ca_bundle() 一旦读到这两个变量，就会调用
# use_rustls_tls() 切到 rustls——那正是此前 h2 基线被污染的成因。改为把抓包 CA
# 装进容器系统信任库，native-tls 走 OpenSSL 系统根，既信任探针又不触发该分支。
docker cp "$ca_cert" "$capture_container:/usr/local/share/ca-certificates/mitm-baseline.crt" >/dev/null
docker exec "$capture_container" update-ca-certificates >/dev/null 2>&1

docker exec \
  -e HTTP_PROXY="http://127.0.0.1:$probe_port" \
  -e HTTPS_PROXY="http://127.0.0.1:$probe_port" \
  -e http_proxy="http://127.0.0.1:$probe_port" \
  -e https_proxy="http://127.0.0.1:$probe_port" \
  "$capture_container" timeout 60 "$codex_bin" exec \
  --model "$model" --skip-git-repo-check "只回复 H2_BASELINE_OK" >/dev/null 2>&1 || true

for _ in $(seq 1 60); do
  docker exec "$capture_container" test -f "/capture/runs/$run_id/h2-wire.json" && break
  sleep 1
done
docker exec "$capture_container" cat "/capture/runs/$run_id/h2-wire.json"
printf 'run_id=%s\n' "$run_id"
