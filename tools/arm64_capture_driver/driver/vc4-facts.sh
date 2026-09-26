#!/bin/bash
# 从本轮批准门禁及成功日志生成 facts，再由统一 producer 签发与重放完整实现测试收据。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
E="$1"; TREE="$2"
python3 "$DRV/implementation_gates.py" facts "$E" --tree "$TREE"
chmod 600 "$E/facts.json"
python3 -m tools.official_client_capture.codex_upgrade_vc_receipt finalize --evidence-root "$E" --facts facts.json --output receipt.json
python3 -m tools.official_client_capture.codex_upgrade_vc_receipt replay --evidence-root "$E" --receipt receipt.json
