#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

state_root=${CAPTURE_STATE_ROOT:-/run/sub2apiplus-official-capture}
state_path="$state_root/mitm.state"
if [[ -L $state_path || ! -f $state_path ]]; then
  echo "MITM 抓包状态不存在或不可信：$state_path" >&2
  exit 1
fi
if [[ $(stat -c '%a' "$state_path") != 600 ]]; then
  echo "MITM 抓包状态权限必须为 0600。" >&2
  exit 1
fi

schema=""
pid=""
pgid=""
start_ticks=""
port=""
while IFS='=' read -r key value; do
  case "$key" in
    schema) schema=$value ;;
    pid) pid=$value ;;
    pgid) pgid=$value ;;
    start_ticks) start_ticks=$value ;;
    port) port=$value ;;
  esac
done <"$state_path"
if (
  [[ $schema != mitm-capture-state/v1 ]] ||
  [[ ! $pid =~ ^[0-9]+$ ]] ||
  [[ ! $pgid =~ ^[0-9]+$ ]] ||
  [[ $pid != "$pgid" ]] ||
  [[ ! $start_ticks =~ ^[0-9]+$ ]] ||
  [[ ! $port =~ ^[0-9]+$ ]]
); then
  echo "MITM 抓包状态字段非法。" >&2
  exit 1
fi

cleanup_status=0
if [[ -r /proc/$pid/stat ]]; then
  actual_start_ticks=$(awk '{print $22}' "/proc/$pid/stat")
  if [[ $actual_start_ticks != "$start_ticks" ]]; then
    echo "MITM PID 已复用，拒绝向非受管进程发送信号。" >&2
    exit 1
  fi
  kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
  for _ in $(seq 1 100); do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL -- "-$pgid" >/dev/null 2>&1 || cleanup_status=1
  fi
fi

for _ in $(seq 1 50); do
  if ! python3 - "$port" <<'PY'
import socket
import sys

with socket.socket() as client:
    client.settimeout(0.2)
    raise SystemExit(0 if client.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
  then
    break
  fi
  sleep 0.1
done
if python3 - "$port" <<'PY'
import socket
import sys

with socket.socket() as client:
    client.settimeout(0.2)
    raise SystemExit(0 if client.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
then
  echo "MITM 端口未恢复：$port" >&2
  cleanup_status=1
fi

rm -f -- "$state_path"
if [[ $cleanup_status != 0 ]]; then
  exit 1
fi
printf 'mitm_stopped_pid=%s\n' "$pid"
