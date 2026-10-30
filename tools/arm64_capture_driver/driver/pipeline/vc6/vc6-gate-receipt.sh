#!/bin/bash
# post-promotion 门禁跑完、本机同步提交（vc6-local-commit.sh sync）之后，
# 构造 post-promotion 门禁 facts（v4）→ finalize → replay，产物都在 VC-6 证据根（vc6-root.txt）里。
# 文件名：post-promotion-gates.facts.json／post-promotion-gates.receipt.json。
set -Eeuo pipefail; umask 077
# 必须在 cd 之前把脚本所在目录解析成绝对路径：用相对路径调用本脚本时（例如 bash ./vc6-gate-receipt.sh），
# 下一行 cd 到数据根之后，相对的 dirname 会指向数据根下并不存在的位置，source vc6-env.sh 就找不到文件。
VC6_SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1
source "${VC6_SELF_DIR}/vc6-env.sh"
HERE=${VC6_SELF_DIR}
W=$V6; R=$(cat "$W/vc6-root.txt"); ATT=$(cat "$W/vc6-gates-attempt.txt"); P=$D/candidates/$CAND/production-tree
test -s "$R/acceptance-result.json" && test -s "$R/catalog-promotion-receipt.json"
python3 -B "$HERE/build_gate_facts_vc6.py" "$R" "$ATT" "$P" "$R/acceptance-result.json" "$R/catalog-promotion-receipt.json" | tee "$W/gate-facts.out"
python3 -m tools.official_client_capture.codex_upgrade_gate_receipt finalize --evidence-root "$R" --facts post-promotion-gates.facts.json --output post-promotion-gates.receipt.json | tee "$W/gate-finalize.out"
python3 -m tools.official_client_capture.codex_upgrade_gate_receipt replay --evidence-root "$R" --receipt post-promotion-gates.receipt.json | tee "$W/gate-replay.out"
test -s "$R/post-promotion-gates.receipt.json"
echo "VC6_GATE_RECEIPT_DONE $(date -u +%H:%M:%SZ)"
