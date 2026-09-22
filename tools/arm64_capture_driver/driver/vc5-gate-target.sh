#!/bin/bash
# 目标平台外部门禁（字节码缓存指到测试树外）：gate_before 环境收据 → 在 DC 提交测试树上隔离 make test → gate_after 环境收据。
# 用法：bash vc5-gate-target.sh <attempt_id> <gate_root> <test_tree>
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
ATT="$1"; GATE="$2"; T="$3"; SRC1491=$D/candidates/c0154-candidate-v1/local-analysis-sources/codex-cli-0.149.1
mkdir -p "$GATE/environment" "$GATE/logs"; chmod 700 "$GATE" "$GATE/environment" "$GATE/logs"
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$GATE" --output "environment/$ATT-before-facts.json" --phase gate_before --subject-id "$ATT" | cut -c1-160
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$GATE" --facts "environment/$ATT-before-facts.json" --output "environment/$ATT-before.json" | cut -c1-160
export PYTHONPYCACHEPREFIX=$RUNROOT/pycache
export CODEX_0_149_1_SOURCE_ROOT="$SRC1491"
export CAPTURE_TYPESCRIPT_MODULE="$T/frontend/node_modules/typescript/lib/typescript.js"
# 采集主机上 /root/oauth-capture 是受管工具树的 bind 别名，候选测试树会把它当执行副本比对；
# 私有挂载命名空间里用空 tmpfs 遮住别名根，与 CI/本机环境一致。
cd "$T"; START=$(utc_now); set +e; unshare -m --propagation private bash -c 'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec make test' > "$GATE/logs/target-platform.stdout.log" 2> "$GATE/logs/target-platform.stderr.log"; RC=$?; set -e; END=$(utc_now)
echo "make test rc=$RC $START -> $END"; tail -n 3 "$GATE/logs/target-platform.stdout.log"
cd "$D"; python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$GATE" --output "environment/$ATT-after-facts.json" --phase gate_after --subject-id "$ATT" | cut -c1-160
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$GATE" --facts "environment/$ATT-after-facts.json" --output "environment/$ATT-after.json" | cut -c1-160
python3 - "$GATE/logs/target-platform.gate.json" "$START" "$END" "$RC" "$T" <<'PY'
import json, sys, socket
out, start, end, rc, tree = sys.argv[1:]
json.dump({"gate_id": "target-platform", "command": ["make", "test"], "working_directory": ".", "host": socket.gethostname(), "architecture": "linux/arm64", "started_at_utc": start, "completed_at_utc": end, "exit_code": int(rc), "tree": tree, "isolation": "unshare -m --propagation private; tmpfs(ro) over /root/oauth-capture (host managed-tool alias hidden)"}, open(out, "w"), ensure_ascii=False, indent=2)
PY
chmod 600 "$GATE"/logs/* "$GATE"/environment/*; echo "GATE_TARGET_DONE rc=$RC"
