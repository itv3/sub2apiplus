#!/bin/bash
# 本机（macOS）check-egress-spec 门禁：在 DC 提交工作树（候选 commit + 承接收据）上执行 make check-egress-spec，
# 同一次运行同时产出 (a) VC-5 外部门禁三件套 check-egress-spec.{gate.json,stdout.log,stderr.log}
# 与 (b) VC-4 实现测试日志 check-egress-spec.log；随后在 C 工作树（候选 commit 自身）上跑 check-egress-spec-ci 做交叉核对。
# 用法：bash local-gate.sh <ROUND> <C> <DC> <RECEIPT 相对路径> <输出根>（产物：<输出根>/local-gates、<输出根>/impl-logs）
#   环境：REPO（仓库根，默认 ~/Developer/sub2apiplus）、GATES_ROOT（两棵工作树 wt-C/wt-D 所在，默认 ~/Developer/.sub2apiplus-gates）
set -u
ROUND="$1"; C="$2"; DC="$3"; RECEIPT="$4"; OUTROOT="$5"
REPO=${REPO:-$HOME/Developer/sub2apiplus}; GATES_ROOT=${GATES_ROOT:-$HOME/Developer/.sub2apiplus-gates}
OUT=$OUTROOT/local-gates; IMPL=$OUTROOT/impl-logs; T=$GATES_ROOT/wt-D; TC=$GATES_ROOT/wt-C
rm -rf "$OUT" "$IMPL"; mkdir -p "$OUT" "$IMPL/cross-check"
: "${HISTORICAL_SOURCE_ROOT:?请显式提供本机历史门禁源码路径}"
export CODEX_0_149_1_SOURCE_ROOT="$HISTORICAL_SOURCE_ROOT"
TS=$REPO/frontend/node_modules/typescript/lib/typescript.js
test "$(git -C $T rev-parse HEAD)" = "$DC"; test "$(git -C $TC rev-parse HEAD)" = "$C"
cd "$T"; START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
make check-egress-spec CODEX_0_149_1_SOURCE_ROOT="${CODEX_0_149_1_SOURCE_ROOT}" CAPTURE_TYPESCRIPT_MODULE="${TS}" > "$OUT/check-egress-spec.stdout.log" 2> "$OUT/check-egress-spec.stderr.log"; RC=$?
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 - "$OUT/check-egress-spec.gate.json" "$START" "$END" "$RC" "$T" "$(git -C $T rev-parse HEAD)" <<'PY'
import json, sys, socket, platform
out, start, end, rc, tree, head = sys.argv[1:]
json.dump({"gate_id": "check-egress-spec", "command": ["make", "check-egress-spec"], "working_directory": ".", "host": socket.gethostname().split(".")[0], "architecture": f"{platform.system().lower()}/{platform.machine()}", "started_at_utc": start, "completed_at_utc": end, "exit_code": int(rc), "tree": tree, "tree_head": head}, open(out, "w"), ensure_ascii=False, indent=2)
PY
{
  echo "# VC-4 实现测试：make check-egress-spec（${ROUND}）"
  echo "candidate_commit=${C}（候选源码树，record-candidate-build 绑定的 commit）"
  echo "executed_on_commit=${DC}（= 候选 commit + 描述它的冻结承接收据 ${RECEIPT}）"
  echo "reason=冻结承接收据按工具约束不得进入它所描述的提交，而 check-egress-spec 含 Go 冻结承接测试；在候选 commit 上单独执行 check-egress-spec-ci 作交叉核对（见 cross-check/）"
  echo "worktree=${T}"
  echo "CODEX_0_149_1_SOURCE_ROOT=${CODEX_0_149_1_SOURCE_ROOT}（本机只读源码基线，不在仓库内）"
  echo "${START}"; echo "host=$(uname -srm)"; echo "go: $(go version)"; echo "python: $(python3 --version)"; echo "node: $(node --version)"
  echo "backend diff candidate..executed: [$(git -C $T diff --stat $C $DC -- backend | tail -1)]"
  echo; echo "## make check-egress-spec"; echo '$ make check-egress-spec'
  cat "$OUT/check-egress-spec.stdout.log"; echo "--- stderr ---"; cat "$OUT/check-egress-spec.stderr.log"
  echo "exit_code=${RC}"; echo "${END}"
  echo "## 工作树洁净度（含忽略文件）"; echo "git_status=[$(git -C $T status --porcelain --untracked-files=all)]"; echo "git_status_ignored=[$(git -C $T status --porcelain --ignored | grep -c '^!!') ignored entries]"
} > "$IMPL/check-egress-spec.log"
echo "check-egress-spec rc=${RC} ${START} -> ${END}"
cd "$TC"
{
  echo "# 交叉核对：候选 commit 自身上的 make check-egress-spec-ci（不含承接收据，预期仅冻结承接测试红）"
  echo "commit=${C}"; date -u +%Y-%m-%dT%H:%M:%SZ
  echo "## make check-egress-spec-ci"; echo '$ make check-egress-spec-ci'
  make check-egress-spec-ci CODEX_0_149_1_SOURCE_ROOT="${CODEX_0_149_1_SOURCE_ROOT}" CAPTURE_TYPESCRIPT_MODULE="${TS}" 2>&1; echo "exit_code=${PIPESTATUS[0]}"
  echo "## 工作树洁净度（含忽略文件）"; echo "git_status=[$(git -C $TC status --porcelain --untracked-files=all)]"
  echo "CROSS_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$IMPL/cross-check/check-egress-spec.C-only.local.log" 2>&1
grep -n "^exit_code=\|CROSS_DONE\|🔴\|FAIL" "$IMPL/cross-check/check-egress-spec.C-only.local.log" | head -n 8 | cut -c1-200
echo "LOCAL_GATE_DONE rc=${RC}"
