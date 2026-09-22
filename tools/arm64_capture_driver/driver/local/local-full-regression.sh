#!/bin/bash
# 本机 full-regression 外部门禁：在 DC 提交工作树上执行 make test，产出 full-regression.{gate.json,stdout.log,stderr.log}
# 用法：bash local-full-regression.sh <DC> <输出根>（产物：<输出根>/local-gates）
#   环境：REPO、GATES_ROOT 同 local-gate.sh
set -u
DC="$1"; OUTROOT="$2"; OUT=$OUTROOT/local-gates
REPO=${REPO:-$HOME/Developer/sub2apiplus}; GATES_ROOT=${GATES_ROOT:-$HOME/Developer/.sub2apiplus-gates}; T=$GATES_ROOT/wt-D
export CODEX_0_149_1_SOURCE_ROOT=${CODEX_0_149_1_SOURCE_ROOT:-$REPO/local-analysis/sources/codex-cli-0.149.1}
TS=$REPO/frontend/node_modules/typescript/lib/typescript.js
mkdir -p "$OUT"
test "$(git -C $T rev-parse HEAD)" = "$DC"; test -z "$(git -C $T status --porcelain --untracked-files=all)"
cd "$T"; START=$(date -u +%Y-%m-%dT%H:%M:%SZ); S0=$(date +%s)
make test CODEX_0_149_1_SOURCE_ROOT="${CODEX_0_149_1_SOURCE_ROOT}" CAPTURE_TYPESCRIPT_MODULE="${TS}" > "$OUT/full-regression.stdout.log" 2> "$OUT/full-regression.stderr.log"; RC=$?
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 - "$OUT/full-regression.gate.json" "$START" "$END" "$RC" "$T" "$(git -C $T rev-parse HEAD)" <<'PY'
import json, sys, socket, platform
out, start, end, rc, tree, head = sys.argv[1:]
json.dump({"gate_id": "full-regression", "command": ["make", "test"], "working_directory": ".", "host": socket.gethostname().split(".")[0], "architecture": f"{platform.system().lower()}/{platform.machine()}", "started_at_utc": start, "completed_at_utc": end, "exit_code": int(rc), "tree": tree, "tree_head": head}, open(out, "w"), ensure_ascii=False, indent=2)
PY
echo "full-regression rc=${RC} elapsed=$(( $(date +%s) - S0 ))s ${START} -> ${END}"; grep -o "Ran [0-9]* tests in [0-9.]*s" "$OUT/full-regression.stderr.log" | tail -n 3; grep -E "^(OK|FAILED)" "$OUT/full-regression.stderr.log" | tail -n 3
echo "git_status_after=[$(git -C $T status --porcelain --untracked-files=all | head -n 3)]"
echo "LOCAL_FULL_REGRESSION_DONE rc=${RC}"
