#!/bin/bash
# post-promotion 门禁跑完、本机同步提交（vc6-local-commit.sh sync）之后，
# 构造 post-promotion 门禁 facts（v4）→ finalize → replay，产物都在 VC-6 证据根（vc6-root.txt）里。
# 文件名：post-promotion-gates.facts.json／post-promotion-gates.receipt.json。
set -Eeuo pipefail; umask 077
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
W=$V6; R=$(cat "$W/vc6-root.txt"); ATT=$(cat "$W/vc6-gates-attempt.txt"); P=$D/candidates/$CAND/production-tree
test -s "$R/acceptance-result.json" && test -s "$R/catalog-promotion-receipt.json"
python3 -B "$HERE/build_gate_facts_vc6.py" "$R" "$ATT" "$P" "$R/acceptance-result.json" "$R/catalog-promotion-receipt.json" | tee "$W/gate-facts.out"
python3 -m tools.official_client_capture.codex_upgrade_gate_receipt finalize --evidence-root "$R" --facts post-promotion-gates.facts.json --output post-promotion-gates.receipt.json | tee "$W/gate-finalize.out"
python3 -m tools.official_client_capture.codex_upgrade_gate_receipt replay --evidence-root "$R" --receipt post-promotion-gates.receipt.json | tee "$W/gate-replay.out"
test -s "$R/post-promotion-gates.receipt.json"
echo "VC6_GATE_RECEIPT_DONE $(date -u +%H:%M:%SZ)"
