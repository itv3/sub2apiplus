#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 在目标容器的网络命名空间中启动一次隔离 tcpdump。脚本本身运行在 capture-cli
# 内，通过只读挂载的 Docker CLI 与宿主 Docker socket 创建同镜像 sidecar。

run_id=${1:?必须提供 RUN_ID}
subject=${2:?必须提供 SUBJECT}
source_container=${3:?必须提供来源容器}
control_container=${CAPTURE_CONTROL_CONTAINER:-$(hostname)}
capture_mount=${CAPTURE_MOUNT:-/capture}
state_root=${CAPTURE_STATE_ROOT:-/run/sub2apiplus-official-capture}
target_port=${CAPTURE_TARGET_PORT:-443}

safe_id='^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
if [[ ! $run_id =~ $safe_id || ! $subject =~ $safe_id ]]; then
  echo "RUN_ID／SUBJECT 格式非法。" >&2
  exit 2
fi
if [[ ! $source_container =~ $safe_id || ! $control_container =~ $safe_id ]]; then
  echo "容器名称格式非法。" >&2
  exit 2
fi
if [[ ! $target_port =~ ^[0-9]+$ ]] || (( target_port < 1 || target_port > 65535 )); then
  echo "CAPTURE_TARGET_PORT 超出合法范围。" >&2
  exit 2
fi
if [[ $capture_mount != /* || $capture_mount == / ]]; then
  echo "CAPTURE_MOUNT 必须是非根绝对路径。" >&2
  exit 2
fi

for command_name in docker sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "缺少 direct 抓包依赖：$command_name" >&2
    exit 1
  fi
done

if [[ $(docker inspect -f '{{.State.Running}}' "$source_container" 2>/dev/null) != true ]]; then
  echo "来源容器未运行：$source_container" >&2
  exit 1
fi

install -d -m 0700 "$state_root/direct"
state_path="$state_root/direct/$subject.state"
if [[ -e $state_path || -L $state_path ]]; then
  echo "已有 direct 抓包状态，必须先恢复：$state_path" >&2
  exit 1
fi

capture_host_root=$(
  docker inspect -f \
    '{{range .Mounts}}{{if eq .Destination "/capture"}}{{.Source}}{{end}}{{end}}' \
    "$control_container"
)
if [[ $capture_host_root != /* || $capture_host_root == / ]]; then
  echo "无法从控制容器解析宿主证据根。" >&2
  exit 1
fi
control_image_id=$(docker inspect -f '{{.Image}}' "$control_container")
if [[ ! $control_image_id =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "控制容器镜像 ID 非法。" >&2
  exit 1
fi

output_dir="$capture_mount/runs/$run_id/direct/$subject"
if [[ -e $output_dir || -L $output_dir ]]; then
  echo "direct 输出目录已存在，拒绝覆盖：$output_dir" >&2
  exit 1
fi
install -d -m 0700 "$output_dir"

sidecar_suffix=$(printf '%s' "$run_id/$subject" | sha256sum | awk '{print substr($1,1,20)}')
sidecar_name="official-direct-$sidecar_suffix"
if docker inspect "$sidecar_name" >/dev/null 2>&1; then
  echo "direct sidecar 已存在，拒绝接管：$sidecar_name" >&2
  exit 1
fi

started=0
cleanup_failed_start() {
  local original_exit_code=$?
  trap - EXIT ERR INT TERM
  set +e
  if [[ $started == 1 ]]; then
    docker rm -f "$sidecar_name" >/dev/null 2>&1 || true
  fi
  rm -f -- "$state_path"
  exit "$original_exit_code"
}
trap cleanup_failed_start EXIT ERR INT TERM

container_id=$(
  docker run --detach \
    --name "$sidecar_name" \
    --label sub2apiplus.capture.role=direct \
    --label "sub2apiplus.capture.run_id=$run_id" \
    --label "sub2apiplus.capture.subject=$subject" \
    --network "container:$source_container" \
    --cap-add NET_ADMIN \
    --cap-add NET_RAW \
    --volume "$capture_host_root:$capture_mount" \
    --entrypoint /bin/bash \
    "$control_image_id" \
    -lc 'umask 077; exec /usr/bin/tcpdump "$@"' _ \
    -i any -U -s 0 \
    -w "$capture_mount/runs/$run_id/direct/$subject/egress.pcap" \
    "tcp port $target_port"
)
started=1
if [[ ! $container_id =~ ^[a-f0-9]{64}$ ]]; then
  echo "direct sidecar 未返回合法容器 ID。" >&2
  exit 1
fi

temporary_state="$state_path.$$.tmp"
{
  printf 'schema=direct-capture-state/v1\n'
  printf 'run_id=%s\n' "$run_id"
  printf 'subject=%s\n' "$subject"
  printf 'source_container=%s\n' "$source_container"
  printf 'sidecar_name=%s\n' "$sidecar_name"
  printf 'container_id=%s\n' "$container_id"
  printf 'output_dir=%s\n' "$output_dir"
} >"$temporary_state"
chmod 0600 "$temporary_state"
mv -- "$temporary_state" "$state_path"

ready=0
for _ in $(seq 1 100); do
  if [[ $(docker inspect -f '{{.State.Running}}' "$sidecar_name" 2>/dev/null || true) != true ]]; then
    docker logs "$sidecar_name" >&2 2>/dev/null || true
    echo "direct sidecar 在就绪前退出。" >&2
    exit 1
  fi
  # tcpdump 会先创建 pcap，随后才完成抓包接口与 BPF filter 的装配。只看文件
  # 存在会让调用方过早发出首个请求，造成首个 ClientHello 随机丢失。必须等
  # 容器日志明确进入 listening 状态，才把 sidecar 交给场景驱动。
  if [[ -e $output_dir/egress.pcap ]] &&
    [[ $(docker logs "$sidecar_name" 2>&1 || true) == *"listening on "* ]]; then
    ready=1
    break
  fi
  sleep 0.1
done
if [[ $ready != 1 ]]; then
  echo "direct sidecar 未在 10 秒内进入监听状态。" >&2
  exit 1
fi

trap - EXIT ERR INT TERM
started=0
printf 'direct_started=%s\n' "$sidecar_name"
