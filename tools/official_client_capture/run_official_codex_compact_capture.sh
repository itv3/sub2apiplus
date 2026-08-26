#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_tool_root=${CAPTURE_TOOL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}
capture_runtime_root=${CAPTURE_RUNTIME_ROOT:-$capture_tool_root/runtime_scripts}
codex_model=${CODEX_MODEL:-gpt-5.4}
codex_version=${CODEX_VERSION:-0.145.0}
codex_bin=${CODEX_BIN:-/root/.local/bin/codex}
run_id=${RUN_ID:-"official-codex-compact-$(date -u +%Y%m%dT%H%M%SZ)"}
subject=codex-compact
direct_started=0
mitm_started=0

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

python3 - "$capture_root/runs/$run_id" "$run_id" "$codex_model" <<'PY'
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
