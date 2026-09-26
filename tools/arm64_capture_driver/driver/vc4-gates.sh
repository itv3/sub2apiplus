#!/bin/bash
# 按本轮已批准 gate-plan 执行完整实现门禁，工作目录和命令均由合同校验。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
E="$1"; mkdir -p "$E/logs"; chmod 700 "$E"
cd "$D"
python3 "$DRV/implementation_gates.py" run "$E"
grep -E '^exit_code=|GATES_DONE|gate_tree_sha256' "$E/logs/implementation.log"
