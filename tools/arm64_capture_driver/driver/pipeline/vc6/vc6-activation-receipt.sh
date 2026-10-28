#!/bin/bash
# 四阶段（canary／production_switch／rollback／target_restore）都 STAGE_DONE 之后，
# 构造生产激活 facts（v2）→ finalize → replay，产出 activation-receipt.json（后面的 vc6-canonical-activation.sh 要求它存在）。
set -Eeuo pipefail; umask 077
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
W=$V6; R=$(cat "$W/vc6-root.txt"); P=$D/candidates/$CAND/production-tree
for s in canary production_switch rollback target_restore; do test -s "$R/stages/$s/stage.json" || { echo "缺阶段证据 $s"; exit 3; }; done
python3 -B "$HERE/build_activation_facts.py" "$R" "$P" | tee "$W/activation-facts.out"
python3 -m tools.official_client_capture.production_activation_receipt finalize --evidence-root "$R" --facts activation-facts.json --output activation-receipt.json | tee "$W/activation-finalize.out"
python3 -m tools.official_client_capture.production_activation_receipt replay --evidence-root "$R" --receipt activation-receipt.json | tee "$W/activation-replay.out"
test -s "$R/activation-receipt.json"
echo "VC6_ACTIVATION_RECEIPT_DONE $(date -u +%H:%M:%SZ)"
