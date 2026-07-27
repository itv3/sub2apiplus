#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 采集**官方 Codex CLI** 经代理时的 HTTP/2 帧层基线（SETTINGS、WINDOW_UPDATE、伪头顺序）。
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

probe_started=0

cleanup() {
  local status=$?
  if [[ $probe_started == 1 ]]; then
    docker exec "$capture_container" pkill -f h2_wire_probe.py >/dev/null 2>&1 || true
  fi
  echo "环境已恢复：探针已停止（本脚本不改 hosts 与 CA，无其它残留）。"
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

# 官方 CLI 用容器内既有 OAuth 状态；CODEX_CA_CERTIFICATE 让它信任探针证书。
# 探针不完成业务应答，CLI 大概率报错退出——帧层形态已被记录，不影响采集。
docker exec \
  -e HTTP_PROXY="http://127.0.0.1:$probe_port" \
  -e HTTPS_PROXY="http://127.0.0.1:$probe_port" \
  -e http_proxy="http://127.0.0.1:$probe_port" \
  -e https_proxy="http://127.0.0.1:$probe_port" \
  -e CODEX_CA_CERTIFICATE=/opt/mitm/mitmproxy-ca-cert.pem \
  "$capture_container" timeout 60 "$codex_bin" exec \
  --model "$model" --skip-git-repo-check "只回复 H2_BASELINE_OK" >/dev/null 2>&1 || true

for _ in $(seq 1 60); do
  docker exec "$capture_container" test -f "/capture/runs/$run_id/h2-wire.json" && break
  sleep 1
done
docker exec "$capture_container" cat "/capture/runs/$run_id/h2-wire.json"
printf 'run_id=%s\n' "$run_id"
