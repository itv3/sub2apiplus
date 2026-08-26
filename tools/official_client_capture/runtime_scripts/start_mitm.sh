#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# mitmdump 必须跨两次 docker exec 存活，因此由本脚本建立独立进程组，并把 PID、
# 启动时钟与输出坐标写入 0600 状态文件；stop_mitm.sh 只终止该受管进程组。

run_id=${1:?必须提供 RUN_ID}
subject=${2:?必须提供 SUBJECT}
capture_mount=${CAPTURE_MOUNT:-/capture}
state_root=${CAPTURE_STATE_ROOT:-/run/sub2apiplus-official-capture}
tool_root=${CAPTURE_TOOL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}
mitmdump_bin=${CAPTURE_MITMDUMP_BIN:-/usr/bin/mitmdump}
mitm_addon=${CAPTURE_MITM_ADDON:-$tool_root/addons/mitm_capture.py}
mitm_confdir=${CAPTURE_MITM_CONFDIR:-/opt/mitm}
mitm_port=${CAPTURE_MITM_PORT:-18080}
capture_task=${CAPTURE_TASK:?必须提供 CAPTURE_TASK}
capture_boundary=${CAPTURE_BOUNDARY:?必须提供 CAPTURE_BOUNDARY}
capture_scenario=${CAPTURE_SCENARIO:?必须提供 CAPTURE_SCENARIO}
capture_target_hosts=${CAPTURE_TARGET_HOSTS:?必须提供 CAPTURE_TARGET_HOSTS}
capture_host_scope=${CAPTURE_HOST_SCOPE:-targets}
capture_fault_spec=${CAPTURE_FAULT_SPEC:-}

safe_id='^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
if [[ ! $run_id =~ $safe_id || ! $subject =~ $safe_id || ! $capture_scenario =~ $safe_id ]]; then
  echo "MITM 运行坐标格式非法。" >&2
  exit 2
fi
if [[ $capture_task != oauth && $capture_task != api ]]; then
  echo "CAPTURE_TASK 只能是 oauth 或 api。" >&2
  exit 2
fi
if [[ ! $capture_boundary =~ $safe_id ]]; then
  echo "CAPTURE_BOUNDARY 格式非法。" >&2
  exit 2
fi
if [[ $capture_host_scope != targets && $capture_host_scope != all ]]; then
  echo "CAPTURE_HOST_SCOPE 只能是 targets 或 all。" >&2
  exit 2
fi
if [[ ! $capture_target_hosts =~ ^[A-Za-z0-9.,:_-]+$ ]]; then
  echo "CAPTURE_TARGET_HOSTS 格式非法。" >&2
  exit 2
fi
if [[ -n $capture_fault_spec && ! $capture_fault_spec =~ ^[A-Za-z0-9_=,.-]+$ ]]; then
  echo "CAPTURE_FAULT_SPEC 格式非法。" >&2
  exit 2
fi
if [[ ! $mitm_port =~ ^[0-9]+$ ]] || (( mitm_port < 1024 || mitm_port > 65535 )); then
  echo "CAPTURE_MITM_PORT 超出合法范围。" >&2
  exit 2
fi
for path in "$mitmdump_bin" "$mitm_addon"; do
  if [[ -L $path || ! -f $path ]]; then
    echo "MITM 运行文件不存在或不可信：$path" >&2
    exit 1
  fi
done
if [[ ! -x $mitmdump_bin || ! -d $mitm_confdir || -L $mitm_confdir ]]; then
  echo "MITM 可执行文件或 CA 目录不可用。" >&2
  exit 1
fi

install -d -m 0700 "$state_root"
state_path="$state_root/mitm.state"
if [[ -e $state_path || -L $state_path ]]; then
  echo "已有 MITM 抓包状态，必须先恢复：$state_path" >&2
  exit 1
fi
if python3 - "$mitm_port" <<'PY'
import socket
import sys

with socket.socket() as client:
    client.settimeout(0.2)
    raise SystemExit(0 if client.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
then
  echo "MITM 端口已被占用：$mitm_port" >&2
  exit 1
fi

output_dir="$capture_mount/runs/$run_id/mitm/$subject"
if [[ -e $output_dir || -L $output_dir ]]; then
  echo "MITM 输出目录已存在，拒绝覆盖：$output_dir" >&2
  exit 1
fi
install -d -m 0700 "$output_dir"
log_path="$output_dir/mitmdump.log"

setsid env \
  CAPTURE_TASK="$capture_task" \
  CAPTURE_BOUNDARY="$capture_boundary" \
  CAPTURE_RUN_ID="$run_id" \
  CAPTURE_SUBJECT="$subject" \
  CAPTURE_SCENARIO="$capture_scenario" \
  CAPTURE_OUTPUT_DIR="$output_dir" \
  CAPTURE_TARGET_HOSTS="$capture_target_hosts" \
  CAPTURE_HOST_SCOPE="$capture_host_scope" \
  CAPTURE_FAULT_SPEC="$capture_fault_spec" \
  "$mitmdump_bin" \
  --listen-host 0.0.0.0 \
  --listen-port "$mitm_port" \
  --set "confdir=$mitm_confdir" \
  --set block_global=false \
  -s "$mitm_addon" \
  >"$log_path" 2>&1 </dev/null &
pid=$!
pgid=$pid
chmod 0600 "$log_path"

cleanup_failed_start() {
  local original_exit_code=$?
  trap - EXIT ERR INT TERM
  set +e
  kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
  sleep 0.2
  kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  rm -f -- "$state_path"
  exit "$original_exit_code"
}
trap cleanup_failed_start EXIT ERR INT TERM

if [[ ! -r /proc/$pid/stat ]]; then
  echo "MITM 进程未能启动。" >&2
  exit 1
fi
start_ticks=$(awk '{print $22}' "/proc/$pid/stat")
temporary_state="$state_path.$$.tmp"
{
  printf 'schema=mitm-capture-state/v1\n'
  printf 'run_id=%s\n' "$run_id"
  printf 'subject=%s\n' "$subject"
  printf 'pid=%s\n' "$pid"
  printf 'pgid=%s\n' "$pgid"
  printf 'start_ticks=%s\n' "$start_ticks"
  printf 'port=%s\n' "$mitm_port"
  printf 'output_dir=%s\n' "$output_dir"
} >"$temporary_state"
chmod 0600 "$temporary_state"
mv -- "$temporary_state" "$state_path"

ready=0
for _ in $(seq 1 200); do
  if ! kill -0 "$pid" 2>/dev/null; then
    tail -80 "$log_path" >&2 || true
    echo "mitmdump 在就绪前退出。" >&2
    exit 1
  fi
  if python3 - "$mitm_port" <<'PY'
import socket
import sys

with socket.socket() as client:
    client.settimeout(0.2)
    raise SystemExit(0 if client.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
  then
    ready=1
    break
  fi
  sleep 0.1
done
if [[ $ready != 1 ]]; then
  echo "mitmdump 未在 20 秒内监听端口。" >&2
  exit 1
fi

trap - EXIT ERR INT TERM
printf 'mitm_started_pid=%s\n' "$pid"
