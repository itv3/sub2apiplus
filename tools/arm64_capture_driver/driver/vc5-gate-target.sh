#!/bin/bash
# 目标平台外部门禁（禁写字节码、只读使用预编译的树外字节码缓存）：gate_before 环境收据 → 在 DC 提交测试树上隔离 make test → gate_after 环境收据。
# 用法：bash vc5-gate-target.sh <attempt_id> <gate_root> <test_tree> [<字节码缓存目录>]
#   VC-5 accept（vc5-accept.sh、gates.sh target）只传前三个参数，缓存目录用默认的 $RUNROOT/pycache-target-platform；
#   VC-0 预跑（vc0-gate-target.sh）以同一套执行方式调用本脚本，主体标识与门禁根各自独立，并传第 4 个参数把缓存放在
#   预跑目录里，与 VC-5 不共用任何目录（缓存每次清空重建，共用时两边同时跑会互相清掉对方的缓存）。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
ATT="$1"; GATE="$2"; T="$3"; SRC1491=$HISTORICAL_SOURCE_ROOT
mkdir -p "$GATE/environment" "$GATE/logs"; chmod 700 "$GATE" "$GATE/environment" "$GATE/logs"
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$GATE" --output "environment/$ATT-before-facts.json" --phase gate_before --subject-id "$ATT" --rust-tls-codex-version "$TARGET_VERSION" | cut -c1-160
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$GATE" --facts "environment/$ATT-before-facts.json" --output "environment/$ATT-before.json" | cut -c1-160
# 树外只读字节码缓存（修好接着跑第 67 项）：lib.sh 全局禁写字节码。缓存前缀若是空目录，解释器改到前缀下找全部 .pyc
# （含标准库自带的）而全部落空，每个子进程都从源码重编，ARM64 监督器 CLI 启动 584 毫秒，候选树监督器计时用例确定性失败；
# 不设前缀也要每次从源码编译测试树模块（约 323 毫秒），心跳间隔用例只剩约 20 毫秒余量。make test 前把标准库与测试树
# tools 预编译进本次重建的缓存目录（bytecode_cache.py，失败即停），make test 期间只读使用（约 187 毫秒），测试树不留
# __pycache__。PYTHONPATH=. 会让当前目录里的同名文件遮住标准库，调用辅助脚本时去掉。
PYC="${4:-$RUNROOT/pycache-target-platform}"
env -u PYTHONPATH python3 "$DRV/bytecode_cache.py" "$PYC" "$T/tools" | tail -n 1 | cut -c1-300
export PYTHONPYCACHEPREFIX="$PYC"
export CODEX_0_149_1_SOURCE_ROOT="$SRC1491"
export CAPTURE_TYPESCRIPT_MODULE="$T/frontend/node_modules/typescript/lib/typescript.js"
# 采集主机上 /root/oauth-capture 是受管工具树的 bind 别名，候选测试树会把它当执行副本比对；
# 私有挂载命名空间里用空 tmpfs 遮住别名根，与 CI/本机环境一致。
cd "$T"; START=$(utc_now); set +e; unshare -m --propagation private bash -c 'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec make test' > "$GATE/logs/target-platform.stdout.log" 2> "$GATE/logs/target-platform.stderr.log"; RC=$?; set -e; END=$(utc_now)
echo "make test rc=$RC $START -> $END"; tail -n 3 "$GATE/logs/target-platform.stdout.log"
cd "$D"; python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$GATE" --output "environment/$ATT-after-facts.json" --phase gate_after --subject-id "$ATT" --rust-tls-codex-version "$TARGET_VERSION" | cut -c1-160
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$GATE" --facts "environment/$ATT-after-facts.json" --output "environment/$ATT-after.json" | cut -c1-160
python3 - "$GATE/logs/target-platform.gate.json" "$START" "$END" "$RC" "$T" <<'PY'
import json, sys, socket
out, start, end, rc, tree = sys.argv[1:]
json.dump({"gate_id": "target-platform", "command": ["make", "test"], "working_directory": ".", "host": socket.gethostname(), "architecture": "linux/arm64", "started_at_utc": start, "completed_at_utc": end, "exit_code": int(rc), "tree": tree, "isolation": "unshare -m --propagation private; tmpfs(ro) over /root/oauth-capture (host managed-tool alias hidden)"}, open(out, "w"), ensure_ascii=False, indent=2)
PY
chmod 600 "$GATE"/logs/* "$GATE"/environment/*; echo "GATE_TARGET_DONE rc=$RC"
