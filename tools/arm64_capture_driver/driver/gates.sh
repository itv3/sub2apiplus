#!/bin/bash
# 外部门禁（ARM64 侧）：bash gates.sh <mode> ...
#   prepare <DC>               重建 test-tree（DC 提交 + 完整历史 + 前端 node_modules，不注入 vendor）
#   full-regression-isolated   在 test-tree 上隔离 make test，产出 $G/local/full-regression.*
#   target <ATT>               目标平台门禁（gate_before/make test/gate_after）
set -Eeuo pipefail; umask 022
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; T=$B/test-tree
MODE="$1"
if [ "$MODE" = prepare ]; then
  DCX="$2"; NM=$RUNROOT/node_modules-cache
  [ -d "$T/frontend/node_modules" ] && { rm -rf "$NM"; mv "$T/frontend/node_modules" "$NM"; }
  # 完整历史克隆、从 bundle 取分支、检出与“完整历史／不含 vendor”断言见 lib.sh 的 clone_test_tree
  # （VC-0 预跑 vc0-gate-target.sh 用同一函数建自己的测试树）。
  clone_test_tree "$T" "$BUNDLE" "$BUNDLE_BRANCH" "$DCX"
  if [ -d "$NM" ]; then mv "$NM" "$T/frontend/node_modules"; else cp -a "$B/frontend-build/frontend/node_modules" "$T/frontend/node_modules"; fi
  echo "test-tree HEAD=$(git -C $T rev-parse HEAD) status=[$(git -C $T status --porcelain --untracked-files=all)]"
  sha256sum "$T/frontend/node_modules/typescript/lib/typescript.js" | cut -c1-16
  exit 0
fi
mkdir -p "$G/local" "$G/logs"; chmod 700 "$G" "$G/local" "$G/logs"
if [ "$MODE" = full-regression-isolated ]; then
  # 采集主机上 /root/oauth-capture 是真实受管工具树的 bind 别名，候选测试树按默认
  # --capture-root 会把它当执行副本比对而整批报「执行位置与受管工具树不一致」；
  # 在私有挂载命名空间里用空 tmpfs 遮住别名根，与 CI/本机（无此路径）环境一致。
  export CODEX_0_149_1_SOURCE_ROOT=$HISTORICAL_SOURCE_ROOT
  export CAPTURE_TYPESCRIPT_MODULE=$T/frontend/node_modules/typescript/lib/typescript.js
  # 树外只读字节码缓存（原因同 vc5-gate-target.sh）：先把标准库与测试树 tools 预编译进本次重建的缓存目录
  # （失败即停），make test 期间只读使用，测试树不留 __pycache__。
  PYC="$RUNROOT/pycache-full-regression"
  env -u PYTHONPATH python3 "$DRV/bytecode_cache.py" "$PYC" "$T/tools" | tail -n 1 | cut -c1-300
  export PYTHONPYCACHEPREFIX="$PYC"
  # 身份记忆化不用 lib.sh 导出的生产目录（与 isolated_run、目标平台门禁相同）。
  cd "$T"; START=$(utc_now); set +e
  env -u CODEX_UPGRADE_IDENTITY_MEMO unshare -m --propagation private bash -c 'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec make test' > "$G/local/full-regression.stdout.log" 2> "$G/local/full-regression.stderr.log"; RC=$?; set -e; END=$(utc_now)
  python3 - "$G/local/full-regression.gate.json" "$START" "$END" "$RC" "$T" "$(git -C $T rev-parse HEAD)" <<'PY'
import json, sys, socket
out, start, end, rc, tree, head = sys.argv[1:]
json.dump({"gate_id": "full-regression", "command": ["make", "test"], "working_directory": ".", "host": socket.gethostname(), "architecture": "linux/arm64", "started_at_utc": start, "completed_at_utc": end, "exit_code": int(rc), "tree": tree, "tree_head": head, "isolation": "unshare -m --propagation private; tmpfs(ro) over /root/oauth-capture (host managed-tool alias hidden)"}, open(out, "w"), ensure_ascii=False, indent=2)
PY
  echo "full-regression rc=$RC $START -> $END"; tail -n 3 "$G/local/full-regression.stdout.log"; grep -E "^FAIL|^ERROR|Error" "$G/local/full-regression.stderr.log" | head -5; exit 0
fi
if [ "$MODE" = target ]; then
  ATT="$2"; bash "$DRV/vc5-gate-target.sh" "$ATT" "$G" "$T" 2>&1 | tail -n 4 | cut -c1-200; exit 0
fi
echo "unknown mode"; exit 2
