#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 用真实上游字节中继采集**官方 Codex CLI 经代理时的 h2 帧层**。
#
# 与 direct 版的关键差异：官方在 native-tls 默认分支下恒不 offer ALPN（走 h1），
# 只有配了 CODEX_CA_CERTIFICATE 触发 rustls 才 offer h2。要采 h2 帧层就**必须**
# 走后者——因此本脚本刻意设该变量，采到的是**官方在 rustls 分支下的形态**，
# 属条件样本，不得写成官方默认行为（§2.4 的观测污染教训）。
#
# 与既有探针的区别：探针自己应答、不转发上游，客户端拿不到真实响应就不会有后续
# 动作；本脚本走 upstream_byte_relay.py，两条 TLS 腿之间只复制明文字节，因此
# **多轮对话、工具调用、压缩触发这些依赖模型自主决策的链路才可能发生**。
#
# ALPN 必须为空
# -------------
# 官方在 native-tls 默认分支下**恒不 offer ALPN**——h1 探针三份基线与 nativetls
# 基线的 negotiated_alpn 全为 None，只有配了 CODEX_CA_CERTIFICATE 触发 rustls 时
# 才 offer h2（official-h2-20260727T131936Z）。因此本脚本：
#
#   - 不设 CODEX_CA_CERTIFICATE / SSL_CERT_FILE（否则切 rustls，污染 TLS 结论）
#   - 改把抓包 CA 装进容器系统信任库，native-tls 走 OpenSSL 系统根
#   - 中继的 --assume-alpn 留空，即不向上游 offer ALPN
#
# 会消耗真实配额：本脚本与真实上游完成真实往返。

capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
run_id=${RUN_ID:?必须提供 RUN_ID}
model=${MODEL:-gpt-5.6-luna}
# 直接监听 443：客户端打的就是 443，容器内该端口空闲，且 iptables 重定向在
# 无 NET_ADMIN 的容器里不可用。
relay_port=${RELAY_PORT:-18443}
turns=${TURNS:-3}
codex_bin=${CODEX_BIN:-/root/.local/bin/codex}

if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

work_dir="$capture_root/runs/$run_id"
tls_dir="$work_dir/tls"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"

relay_started=0

cleanup() {
  local status=$?
  if [[ $relay_started == 1 ]]; then
    docker exec "$capture_container" pkill -f upstream_byte_relay.py >/dev/null 2>&1 || true
  fi
  # hosts 与临时 CA 一律还原，避免污染后续采集
  docker exec "$capture_container" sh -c \
    'grep -v " chatgpt.com$" /etc/hosts > /tmp/.hr && cat /tmp/.hr > /etc/hosts && rm -f /tmp/.hr' \
    >/dev/null 2>&1 || true
  docker exec "$capture_container" rm -f /usr/local/share/ca-certificates/relay-ca.crt >/dev/null 2>&1 || true
  docker exec "$capture_container" update-ca-certificates --fresh >/dev/null 2>&1 || true
  echo "环境已恢复：中继已停止，hosts 与系统信任库中的临时 CA 均已还原。"
  exit $status
}
trap cleanup EXIT

install -d -m 0700 "$work_dir" "$tls_dir"

# 中继面向客户端的证书
openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/relay.key" \
  -out "$tls_dir/relay.csr" -subj "/CN=chatgpt.com" >/dev/null 2>&1
printf 'subjectAltName=DNS:chatgpt.com\n' > "$tls_dir/relay.ext"
openssl x509 -req -in "$tls_dir/relay.csr" -CA "$ca_full" -CAkey "$ca_full" \
  -CAcreateserial -out "$tls_dir/relay.crt" -days 1 -sha256 \
  -extfile "$tls_dir/relay.ext" >/dev/null 2>&1
chmod 600 "$tls_dir"/*

# 上游真实 IP：direct 模式必须绕开被劫持的 hosts，否则中继会连回自身
upstream_ip=$(docker exec "$capture_container" getent ahostsv4 chatgpt.com | head -1 | cut -d' ' -f1)
if [[ -z $upstream_ip ]]; then
  echo "无法解析 chatgpt.com 的上游 IP。" >&2
  exit 1
fi
echo "上游真实 IP：$upstream_ip"

# CA 装系统信任库（不设环境变量，避免切到 rustls）
docker cp "$ca_cert" "$capture_container:/usr/local/share/ca-certificates/relay-ca.crt" >/dev/null
docker exec "$capture_container" update-ca-certificates >/dev/null 2>&1

# 起中继（--assume-alpn 留空 = 不 offer ALPN，与官方 native-tls 实测一致）
docker exec -d "$capture_container" python3 \
  /capture/tools/official_client_capture/upstream_byte_relay.py \
  --cert "/capture/runs/$run_id/tls/relay.crt" --key "/capture/runs/$run_id/tls/relay.key" \
  --mode connect --port "$relay_port" --assume-alpn h2,http/1.1 \
  --upstream-host chatgpt.com --upstream-ip "$upstream_ip" \
  --output "/capture/runs/$run_id/relay" --timeout 300
relay_started=1
sleep 2

# CONNECT 模式无需 hosts 劫持，由代理环境变量把客户端引到中继

echo "=== 开始 $turns 轮真实对话 ==="
for i in $(seq 1 "$turns"); do
  echo "--- 第 $i 轮 ---"
  docker exec \
    -e HTTPS_PROXY="http://127.0.0.1:$relay_port" -e https_proxy="http://127.0.0.1:$relay_port" \
    -e CODEX_CA_CERTIFICATE=/opt/mitm/mitmproxy-ca-cert.pem \
    "$capture_container" timeout 90 "$codex_bin" exec \
    --model "$model" --skip-git-repo-check \
    "这是第 $i 轮，请只回复：ROUND-$i-OK" 2>&1 | tail -2 || true
done

docker exec "$capture_container" pkill -f upstream_byte_relay.py >/dev/null 2>&1 || true
sleep 3
docker exec "$capture_container" cat "/capture/runs/$run_id/relay/relay.json" 2>/dev/null || echo "无产物"
printf 'run_id=%s\n' "$run_id"
