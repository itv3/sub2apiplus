#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$capture_root/tools/official_client_capture}
capture_runtime_root=${CAPTURE_RUNTIME_ROOT:-$capture_tool_root/runtime_scripts}
# CAPTURE_ROOT 是容器内逻辑根，不能再被宿主 shell 当作可写路径。Formal 工具
# 默认从自身受管部署位置反推宿主数据根；显式变量用于离线夹具与非标准部署。
host_tool_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
capture_host_data_root=${CAPTURE_HOST_DATA_ROOT:-$(cd -- "$host_tool_root/../.." && pwd -P)}
codex_model=${CODEX_MODEL:-gpt-5.4}
codex_version=${CODEX_VERSION:-0.145.0}
codex_bin=${CODEX_BIN:-/root/.local/bin/codex}
run_id=${RUN_ID:-"official-codex-compact-$(date -u +%Y%m%dT%H%M%SZ)"}
subject=codex-compact
direct_started=0
mitm_started=0

verify_storage_mapping() {
  if [[ $capture_host_data_root != /* || $capture_host_data_root == / || -L $capture_host_data_root || ! -d $capture_host_data_root ]]; then
    echo "CAPTURE_HOST_DATA_ROOT 必须是可信的非根绝对目录。" >&2
    exit 2
  fi
  local host_runs="$capture_host_data_root/runs"
  local container_runs="$capture_root/runs"
  if [[ -L $host_runs || ! -d $host_runs ]]; then
    echo "宿主 runs 根不存在或不可信：$host_runs" >&2
    exit 1
  fi
  local host_runs_identity container_runs_identity
  host_runs_identity=$(stat -Lc '%d:%i' "$host_runs")
  container_runs_identity=$(docker exec "$capture_container" stat -Lc '%d:%i' "$container_runs")
  if [[ $host_runs_identity != "$container_runs_identity" ]]; then
    echo "宿主与容器 runs 根不同源，拒绝发送请求。" >&2
    exit 1
  fi
}

cleanup() {
  local original_exit_code=$?
  trap - EXIT ERR INT TERM
  set +e
  if [[ $direct_started == 1 ]]; then
    docker exec "$capture_container" "$capture_runtime_root/stop_direct.sh" "$subject" || true
  fi
  if [[ $mitm_started == 1 ]]; then
    docker exec "$capture_container" "$capture_runtime_root/stop_mitm.sh" || true
  fi
  exit "$original_exit_code"
}
trap cleanup EXIT ERR INT TERM

verify_storage_mapping
host_run_root="$capture_host_data_root/runs/$run_id"
if [[ -e $host_run_root || -L $host_run_root ]]; then
  echo "RUN_ID 已存在，拒绝混写旧样本：$host_run_root" >&2
  exit 2
fi
install -d -m 0700 "$host_run_root"
direct_output="/capture/runs/$run_id/result/direct"
docker exec "$capture_container" "$capture_runtime_root/start_direct.sh" \
  "$run_id" "$subject" "$capture_container"
direct_started=1
docker exec "$capture_container" \
  python3 "$capture_tool_root/run_codex_compact_scenario.py" \
  --mode official-http --model "$codex_model" --codex-version "$codex_version" \
  --codex-bin "$codex_bin" \
  --output-dir "$direct_output" --timeout 300
docker exec "$capture_container" "$capture_runtime_root/stop_direct.sh" "$subject"
direct_started=0

docker exec \
  -e CAPTURE_TASK=oauth \
  -e CAPTURE_BOUNDARY=official_cli_to_official_platform \
  -e CAPTURE_SCENARIO=compact \
  -e CAPTURE_TARGET_HOSTS=chatgpt.com \
  -e CAPTURE_HOST_SCOPE=targets \
  "$capture_container" "$capture_runtime_root/start_mitm.sh" "$run_id" "$subject"
mitm_started=1
docker exec \
  -e HTTP_PROXY=http://127.0.0.1:18080 \
  -e HTTPS_PROXY=http://127.0.0.1:18080 \
  -e http_proxy=http://127.0.0.1:18080 \
  -e https_proxy=http://127.0.0.1:18080 \
  -e SSL_CERT_FILE=/opt/mitm/mitmproxy-ca-cert.pem \
  "$capture_container" \
  python3 "$capture_tool_root/run_codex_compact_scenario.py" \
  --mode official-http --model "$codex_model" --codex-version "$codex_version" \
  --codex-bin "$codex_bin" \
  --output-dir "/capture/runs/$run_id/result/mitm" --timeout 300
docker exec "$capture_container" "$capture_runtime_root/stop_mitm.sh"
mitm_started=0

python3 - "$host_run_root" "$run_id" "$codex_model" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
direct = json.loads((root / "result/direct/summary.json").read_text())
mitm = json.loads((root / "result/mitm/summary.json").read_text())
pcap = root / "direct/codex-compact/egress.pcap"
jsonl = sorted(root.glob("mitm/codex-compact/*.jsonl"))
payload = {
    "schema_version": "official-codex-compact-capture/v1",
    "run_id": sys.argv[2],
    "status": "complete" if direct.get("valid") and mitm.get("valid") else "failed",
    "model": sys.argv[3],
    "direct": {
        "valid": bool(direct.get("valid")),
        "pcap_bytes": pcap.stat().st_size,
        "pcap_sha256": hashlib.sha256(pcap.read_bytes()).hexdigest(),
    },
    "mitm": {
        "valid": bool(mitm.get("valid")),
        "jsonl": [
            {"path": str(path.relative_to(root)), "records": sum(1 for _ in path.open(encoding="utf-8"))}
            for path in jsonl
        ],
    },
}
output = root / "run-summary.json"
output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(output, 0o600)
if payload["status"] != "complete" or payload["direct"]["pcap_bytes"] <= 24:
    raise SystemExit("官方 compact direct/MITM 场景校验失败")
if not any(item["records"] > 0 for item in payload["mitm"]["jsonl"]):
    raise SystemExit("官方 compact MITM 未记录请求")
print(json.dumps(payload, ensure_ascii=False))
PY

printf 'run_id=%s\n' "$run_id"
