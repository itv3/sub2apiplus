#!/bin/sh
# Claude 侧 R 类原始字节采集：用 upstream_byte_relay.py 在客户端与真实上游之间
# 只复制明文应用字节，从而保留 h1 的字面大小写、顺序与 body 原始字节。
# relay 本身是 Codex 侧建好的通用工具，其 chatgpt/openai 硬编码都在条件分支内，
# Anthropic 流量不会命中。
set -eu
OUT=${1:-/work/relay-claude}
UPSTREAM_HOST=api.anthropic.com
UPSTREAM_IP=160.79.104.10
CA=/opt/mitm/mitmproxy-ca.pem
WORK=/work/relay-tmp

rm -rf "$WORK" "$OUT"; mkdir -p "$WORK" "$OUT"

echo "=== 用抓包 CA 签发 $UPSTREAM_HOST 证书 ==="
openssl req -new -newkey rsa:2048 -nodes -keyout "$WORK/leaf.key" \
  -subj "/CN=$UPSTREAM_HOST" -out "$WORK/leaf.csr" 2>/dev/null
cat > "$WORK/ext.cnf" <<EXT
subjectAltName=DNS:$UPSTREAM_HOST
extendedKeyUsage=serverAuth
EXT
openssl x509 -req -in "$WORK/leaf.csr" -CA "$CA" -CAkey "$CA" -CAcreateserial \
  -out "$WORK/leaf.crt" -days 3 -extfile "$WORK/ext.cnf" 2>/dev/null
cat "$WORK/leaf.crt" "$CA" > "$WORK/chain.pem"
echo "证书就绪"

echo "=== 启动 relay ==="
python3 /capture/tools/official_client_capture/upstream_byte_relay.py \
  --cert "$WORK/chain.pem" --key "$WORK/leaf.key" \
  --mode direct --port 443 \
  --upstream-host "$UPSTREAM_HOST" --upstream-ip "$UPSTREAM_IP" \
  --assume-alpn http/1.1 \
  --output "$OUT" > /work/relay.log 2>&1 &
RELAY_PID=$!
sleep 4
if ! kill -0 "$RELAY_PID" 2>/dev/null; then echo "relay 启动失败"; tail -20 /work/relay.log; exit 1; fi
echo "relay pid=$RELAY_PID"

echo "=== hosts 劫持 ==="
cp /etc/hosts /work/hosts.bak
printf '127.0.0.1 %s\n' "$UPSTREAM_HOST" >> /etc/hosts

cleanup() {
  cp /work/hosts.bak /etc/hosts 2>/dev/null || true
  kill "$RELAY_PID" 2>/dev/null || true
  sleep 2
  kill -9 "$RELAY_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "=== 跑 claude ==="
NODE_EXTRA_CA_CERTS=$CA timeout 120 /root/.local/bin/claude -p \
  --model claude-sonnet-5 --safe-mode --no-chrome --disable-slash-commands \
  --prompt-suggestions false --no-session-persistence --max-budget-usd 0.10 \
  --tools "" --output-format stream-json --verbose \
  "只回复 RELAY_OK，不调用任何工具。" 2>&1 | tail -c 400
echo
cleanup
trap - EXIT
sleep 1
echo "=== 产物 ==="
ls -la "$OUT" | head -12
