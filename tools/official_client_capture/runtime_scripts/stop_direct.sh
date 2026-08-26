#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

subject=${1:?必须提供 SUBJECT}
state_root=${CAPTURE_STATE_ROOT:-/run/sub2apiplus-official-capture}
safe_id='^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
if [[ ! $subject =~ $safe_id ]]; then
  echo "SUBJECT 格式非法。" >&2
  exit 2
fi

state_path="$state_root/direct/$subject.state"
if [[ -L $state_path || ! -f $state_path ]]; then
  echo "direct 抓包状态不存在或不可信：$state_path" >&2
  exit 1
fi
if [[ $(stat -c '%a' "$state_path") != 600 ]]; then
  echo "direct 抓包状态权限必须为 0600。" >&2
  exit 1
fi

schema=""
recorded_subject=""
sidecar_name=""
container_id=""
output_dir=""
while IFS='=' read -r key value; do
  case "$key" in
    schema) schema=$value ;;
    subject) recorded_subject=$value ;;
    sidecar_name) sidecar_name=$value ;;
    container_id) container_id=$value ;;
    output_dir) output_dir=$value ;;
  esac
done <"$state_path"
if (
  [[ $schema != direct-capture-state/v1 ]] ||
  [[ $recorded_subject != "$subject" ]] ||
  [[ ! $sidecar_name =~ ^official-direct-[a-f0-9]{20}$ ]] ||
  [[ ! $container_id =~ ^[a-f0-9]{64}$ ]] ||
  [[ $output_dir != /* || $output_dir == / ]]
); then
  echo "direct 抓包状态字段非法。" >&2
  exit 1
fi

cleanup_status=0
if docker inspect "$sidecar_name" >/dev/null 2>&1; then
  actual_id=$(docker inspect -f '{{.Id}}' "$sidecar_name")
  actual_role=$(docker inspect -f '{{index .Config.Labels "sub2apiplus.capture.role"}}' "$sidecar_name")
  actual_subject=$(docker inspect -f '{{index .Config.Labels "sub2apiplus.capture.subject"}}' "$sidecar_name")
  if [[ $actual_id != "$container_id" || $actual_role != direct || $actual_subject != "$subject" ]]; then
    echo "direct sidecar 身份与状态文件不一致，拒绝发送信号。" >&2
    exit 1
  fi
  docker kill --signal SIGINT "$sidecar_name" >/dev/null 2>&1 || true
  for _ in $(seq 1 100); do
    if [[ $(docker inspect -f '{{.State.Running}}' "$sidecar_name" 2>/dev/null || true) != true ]]; then
      break
    fi
    sleep 0.1
  done
  if [[ $(docker inspect -f '{{.State.Running}}' "$sidecar_name" 2>/dev/null || true) == true ]]; then
    docker stop --time 3 "$sidecar_name" >/dev/null 2>&1 || cleanup_status=1
  fi
  docker logs "$sidecar_name" >"$output_dir/tcpdump.log" 2>&1 || true
  chmod 0600 "$output_dir/tcpdump.log" 2>/dev/null || true
  docker rm -f "$sidecar_name" >/dev/null 2>&1 || cleanup_status=1
fi

pcap="$output_dir/egress.pcap"
chmod 0600 "$pcap" 2>/dev/null || true
pcap_size=$(stat -c '%s' "$pcap" 2>/dev/null || printf '0')
if (( pcap_size <= 24 )); then
  echo "direct pcap 没有有效数据包：$pcap" >&2
  cleanup_status=1
elif ! tcpdump -nn -r "$pcap" -c 1 >/dev/null 2>&1; then
  echo "direct pcap 无法解析：$pcap" >&2
  cleanup_status=1
fi

rm -f -- "$state_path"
if [[ $cleanup_status != 0 ]]; then
  exit 1
fi
printf 'direct_stopped=%s\n' "$subject"
