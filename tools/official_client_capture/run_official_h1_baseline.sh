#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 采集**官方 Codex CLI** 在 HTTP/1.1 上的原始请求形态，作为 header 大小写与顺序的基线。
#
# 与 run_h1_wire_probe.sh 同构，区别只在被观测方：这里把 capture-cli 自身的 hosts
# 指向本地探针，并用 CODEX_CA_CERTIFICATE 让官方 CLI 信任探针证书（官方 custom_ca
# 支持该变量，且是追加到系统根而非替换）。官方直连同样是空 ALPN，故协商为 h1。
#
# 探针不转发上游，因此本脚本不会产生真实业务请求，也不消耗账号配额。

capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
run_id=${RUN_ID:?必须提供 RUN_ID}
model=${MODEL:-gpt-5.6-luna}
expect_requests=${EXPECT_REQUESTS:-3}
codex_bin=${CODEX_BIN:-/root/.local/bin/codex}
window_id=$(date -u +%Y%m%dT%H%M%SZ)

if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

work_dir="$capture_root/runs/$run_id"
tls_dir="$work_dir/tls"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
hosts_backup="$capture_root/runtime/capture-hosts.before-$window_id"

probe_started=0
hosts_patched=0

cleanup() {
  local status=$?
  if [[ $probe_started == 1 ]]; then
    docker exec "$capture_container" pkill -f h1_wire_probe.py >/dev/null 2>&1 || true
  fi
  if [[ $hosts_patched == 1 ]]; then
    docker exec "$capture_container" sh -c 'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hosts.restore && cat /tmp/.hosts.restore > /etc/hosts && rm -f /tmp/.hosts.restore' >/dev/null 2>&1 || true
  fi
  echo "环境已恢复：capture-cli 的 hosts 回到采集前状态。"
  exit $status
}
trap cleanup EXIT

install -d -m 0700 "$work_dir" "$tls_dir" "$capture_root/runtime"

openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/probe.key" \
  -out "$tls_dir/probe.csr" -subj "/CN=chatgpt.com" >/dev/null 2>&1
printf 'subjectAltName=DNS:chatgpt.com\n' > "$tls_dir/probe.ext"
openssl x509 -req -in "$tls_dir/probe.csr" -CA "$ca_full" -CAkey "$ca_full" \
  -CAcreateserial -out "$tls_dir/probe.crt" -days 1 -sha256 \
  -extfile "$tls_dir/probe.ext" >/dev/null 2>&1
chmod 600 "$tls_dir"/*

docker exec -d "$capture_container" python3 \
  /capture/tools/official_client_capture/h1_wire_probe.py \
  --cert "/capture/runs/$run_id/tls/probe.crt" --key "/capture/runs/$run_id/tls/probe.key" \
  --port 443 --output "/capture/runs/$run_id/h1-wire.json" \
  --expect "$expect_requests" --timeout 90 --idle-timeout 8
probe_started=1
sleep 2

docker cp "$capture_container:/etc/hosts" "$hosts_backup" >/dev/null
chmod 600 "$hosts_backup"
docker exec "$capture_container" sh -c 'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hosts.pre && cat /tmp/.hosts.pre > /etc/hosts && rm -f /tmp/.hosts.pre'
docker exec "$capture_container" sh -c "printf '127.0.0.1 chatgpt.com\n' >> /etc/hosts"
hosts_patched=1

# 官方 CLI 用容器内既有 OAuth 状态；CODEX_CA_CERTIFICATE 让它信任探针证书。
# 探针只回一个固定响应，CLI 大概率会报错退出——请求形态已被记录，这不影响采集。
docker exec \
  -e CODEX_CA_CERTIFICATE=/opt/mitm/mitmproxy-ca-cert.pem \
  "$capture_container" timeout 60 "$codex_bin" exec \
  --model "$model" --skip-git-repo-check "只回复 H1_BASELINE_OK" >/dev/null 2>&1 || true

for _ in $(seq 1 60); do
  docker exec "$capture_container" test -f "/capture/runs/$run_id/h1-wire.json" && break
  sleep 1
done
docker exec "$capture_container" cat "/capture/runs/$run_id/h1-wire.json"
printf 'run_id=%s\n' "$run_id"
