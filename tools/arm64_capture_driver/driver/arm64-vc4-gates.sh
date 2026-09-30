#!/bin/bash
# ARM64 版 VC-4／VC-5 本地门禁（替代本机 driver/local/local-vc4.sh，门禁一律在采集主机的隔离测试树上执行）。
#
# 门禁分工沿用本机脚本的现有合同：
#   * DC（候选提交＋描述它的冻结承接收据）上执行 make check-egress-spec 与 make test，两项都必须通过——产出 VC-5 外部门禁的
#     check-egress-spec 与 full-regression 两项（local-gates 六件套），以及 VC-4 实现测试日志 impl-logs/check-egress-spec.log；
#   * C（候选提交自身）上只执行 make check-egress-spec-ci 作交叉核对（impl-logs/cross-check/check-egress-spec.C-only.local.log），
#     只允许缺冻结承接收据导致的预期失败：冻结承接收据按工具约束不得进入它所描述的提交，C 上的冻结承接测试必然红。
#     本脚本只记录交叉核对结果、不据此判定，由操作员按日志确认失败项只有冻结承接测试。
# 产物直接写入 $RUNROOT/local-gates 与 $RUNROOT/impl-logs（旧目录先整体归档），文件名、gate.json 字段与日志格式都与本机
# 上传逐字段相同，随后用 upload_manifest.py 生成上传清单并写 READY：vc4-all.sh 的上传等待与清单核验、vc5-all.sh 与
# build_gate_facts.py 都不用改（门禁收据只要求 target-platform 一项的架构等于候选架构）。
# 门禁期间每 30 秒更新 impl-logs/HEARTBEAT；门禁结论（rc 非零）照常交付并写 READY，由 vc4-all.sh 与 VC-5 按日志和 gate.json
# 判定；脚本自身失败（bundle、测试树、前端依赖、字节码缓存、并发锁、提交链不符）不交付、不写 READY。
# 顺序：先跑本脚本，完成后再启动 vc4-all.sh（READY 已在，上传等待立即通过），两者不并行，避免 4 核争用把计时用例拖红。
#
# 用法（采集主机 root；make test 里有挂断检测用例，必须 setsid -f 启动，不能 nohup）：
#   ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash arm64-vc4-gates.sh [<前端依赖目录>] > $RUNROOT/arm64-vc4-gates.out 2>&1 < /dev/null
#   候选提交 C、承接提交 DC、承接收据 RECEIPT、bundle 与分支（BUNDLE、BUNDLE_BRANCH）都取自本轮参数文件；
#   前端依赖目录：同时含 node_modules 与 pnpm-lock.yaml 的目录，默认 $HISTORY_TEST_TREE/frontend。
# 退出码：0 DC 两项门禁通过并已交付；1 门禁未通过（结论已交付、READY 已写）；2 用法错误；3 准备或执行失败（未交付、无 READY）。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
usage() { echo "用法：bash arm64-vc4-gates.sh [<前端依赖目录绝对路径>]（C、DC、RECEIPT、BUNDLE、BUNDLE_BRANCH 取自本轮参数文件）" >&2; }
if [ "$#" -gt 1 ]; then usage; exit 2; fi
NM_DIR="${1:-$HISTORY_TEST_TREE/frontend}"
ZERO=0000000000000000000000000000000000000000
for pair in "C=$C" "DC=$DC"; do
  value="${pair#*=}"
  if ! [[ "$value" =~ ^[0-9a-f]{40}$ ]] || [ "$value" = "$ZERO" ]; then echo "${pair%%=*} 必须是本轮真实的完整 40 位提交：$value" >&2; exit 2; fi
done
if ! [[ "$RECEIPT" =~ ^docs/egress/maintenance/[A-Za-z0-9._-]+\.json$ ]]; then echo "RECEIPT 必须是 docs/egress/maintenance/ 下的 JSON 相对路径：$RECEIPT" >&2; exit 2; fi
if [[ "$BUNDLE" != /* ]] || [ -L "$BUNDLE" ] || [ ! -f "$BUNDLE" ]; then echo "BUNDLE 必须是已存在的普通文件（绝对路径）：$BUNDLE" >&2; exit 2; fi
if ! [[ "$BUNDLE_BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then echo "BUNDLE_BRANCH 只允许字母、数字与 ._/-：$BUNDLE_BRANCH" >&2; exit 2; fi
if [[ "$NM_DIR" != /* ]]; then echo "前端依赖目录必须是绝对路径：$NM_DIR" >&2; exit 2; fi
WORK="$RUNROOT/arm64-vc4-gates"; TD="$WORK/wt-D"; TC="$WORK/wt-C"; PYCD="$WORK/pycache-D"; PYCC="$WORK/pycache-C"
GATES="$RUNROOT/local-gates"; IMPL="$RUNROOT/impl-logs"; OUT=""; HEARTBEAT_PID=""
on_error() {
  local rc=$? line="$1"
  trap - ERR
  echo "ARM64_VC4_GATES_ABORTED：准备或执行失败（arm64-vc4-gates.sh 第 ${line} 行，rc=${rc}），未交付、未写 READY；原因见上方输出，主体目录 ${OUT:-（未建立）}"
  exit 3
}
trap 'on_error $LINENO' ERR
mkdir -p "$WORK"; chmod 700 "$WORK"
LOCK="$WORK/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then echo "ARM64_VC4_GATES_ABORTED：已有 ARM64 VC-4 门禁在运行（PID ${HOLDER}），拒绝并发"; exit 3; fi
  rm -rf "$LOCK"; mkdir "$LOCK"
fi
printf '%s\n' "$$" > "$LOCK/pid"
stop_heartbeat() {
  if [ -n "$HEARTBEAT_PID" ]; then kill "$HEARTBEAT_PID" 2>/dev/null || true; wait "$HEARTBEAT_PID" 2>/dev/null || true; HEARTBEAT_PID=""; fi
}
trap 'stop_heartbeat; rm -rf "$LOCK"' EXIT
SUBJECT="arm64-vc4-gates-$(date -u +%Y%m%dt%H%M%Sz)"
if [ -e "$WORK/$SUBJECT" ]; then SUBJECT="$SUBJECT-$$"; fi
OUT="$WORK/$SUBJECT"
mkdir -m 0700 "$OUT"
echo "=== ARM64 版 VC-4 门禁 ${SUBJECT}（${ROUND}：C=${C} DC=${DC}）$(utc_now)"
# 旧的门禁产物整体归档（上传清单按目录全集核验，残留文件会让核验失败），再建新目录并立即开始心跳。
for name in local-gates impl-logs; do
  if [ -e "$RUNROOT/$name" ]; then mkdir -p "$OUT/superseded"; mv "$RUNROOT/$name" "$OUT/superseded/$name"; fi
done
mkdir -m 0700 "$GATES" "$IMPL" "$IMPL/cross-check"
touch "$IMPL/HEARTBEAT"
# 心跳循环的输出接到 /dev/null：否则残留的 sleep 子进程会继续占着本脚本的标准输出，调用方要多等一个心跳周期。
( while sleep 30; do touch "$IMPL/HEARTBEAT" 2>/dev/null || true; done ) > /dev/null 2>&1 &
HEARTBEAT_PID=$!
echo "=== 测试树：DC 与 C 各一棵（clone_test_tree，umask 022）$(utc_now)"
umask 022
clone_test_tree "$TD" "$BUNDLE" "$BUNDLE_BRANCH" "$DC"
clone_test_tree "$TC" "$BUNDLE" "$BUNDLE_BRANCH" "$C"
# DC 必须恰好是 C 加上本轮承接收据这一个文件，否则 C 上的交叉核对不能说明“只差承接收据”。
git -C "$TD" merge-base --is-ancestor "$C" "$DC" || { echo "提交链不符：C 不是 DC 的祖先"; false; }
CHANGED=$(git -C "$TD" diff --name-only "$C" "$DC")
[ "$CHANGED" = "$RECEIPT" ] || { echo "提交链不符：C..DC 的改动必须只有 ${RECEIPT}，实际 [${CHANGED}]"; false; }
for tree in "$TD" "$TC"; do
  if [ ! -d "$NM_DIR/node_modules" ] || ! cmp -s "$NM_DIR/pnpm-lock.yaml" "$tree/frontend/pnpm-lock.yaml"; then
    echo "前端依赖不可用：${NM_DIR}/node_modules 不存在，或 ${NM_DIR}/pnpm-lock.yaml 与 ${tree}/frontend/pnpm-lock.yaml 不同"
    false
  fi
  cp -a "$NM_DIR/node_modules" "$tree/frontend/node_modules"
done
umask 077
echo "wt-D HEAD=$(git -C "$TD" rev-parse HEAD) status=[$(tree_status "$TD")]"
echo "wt-C HEAD=$(git -C "$TC" rev-parse HEAD) status=[$(tree_status "$TC")]"
echo "=== 树外字节码缓存 $(utc_now)"
env -u PYTHONPATH python3 "$DRV/bytecode_cache.py" "$PYCD" "$TD/tools" | tail -n 1 | cut -c1-300
env -u PYTHONPATH python3 "$DRV/bytecode_cache.py" "$PYCC" "$TC/tools" | tail -n 1 | cut -c1-300
# 命令替换里一律 `|| true`：set -E 会把 ERR 陷阱带进命令替换，工具缺失或 head 截断触发的 SIGPIPE 不能把中止信息混进日志。
host_line() { echo "host=$(uname -srm)"; echo "go: $(go version 2>&1 || true)"; echo "python: $(python3 --version 2>&1 || true)"; echo "node: $(node --version 2>&1 || true)"; }
tree_status() { git -C "$1" status --porcelain --untracked-files=all 2>&1 | head -n "${2:-1000000}" || true; }
ISOLATION="采集主机隔离测试树（私有挂载命名空间里只读 tmpfs 遮住 /root/oauth-capture，树外只读字节码缓存）"

echo "=== DC：make check-egress-spec $(utc_now)"
START=$(utc_now); SPEC_RC=0
isolated_run "$TD" . "$PYCD" "$GATES/check-egress-spec.stdout.log" "$GATES/check-egress-spec.stderr.log" make check-egress-spec || SPEC_RC=$?
END=$(utc_now)
write_gate_json "$GATES/check-egress-spec.gate.json" check-egress-spec "$START" "$END" "$SPEC_RC" "$TD" . make check-egress-spec
{
  echo "# VC-4 实现测试：make check-egress-spec（${ROUND}）"
  echo "candidate_commit=${C}（候选源码树，record-candidate-build 绑定的 commit）"
  echo "executed_on_commit=${DC}（= 候选 commit + 描述它的冻结承接收据 ${RECEIPT}）"
  echo "reason=冻结承接收据按工具约束不得进入它所描述的提交，而 check-egress-spec 含 Go 冻结承接测试；在候选 commit 上单独执行 check-egress-spec-ci 作交叉核对（见 cross-check/）"
  echo "worktree=${TD}"
  echo "executed_on=${ISOLATION}"
  echo "CODEX_0_149_1_SOURCE_ROOT=${HISTORICAL_SOURCE_ROOT}（采集主机只读源码基线，不在仓库内）"
  echo "${START}"; host_line
  echo "backend diff candidate..executed: [$(git -C "$TD" diff --stat "$C" "$DC" -- backend 2>&1 | tail -1 || true)]"
  echo; echo "## make check-egress-spec"; echo '$ make check-egress-spec'
  cat "$GATES/check-egress-spec.stdout.log"; echo "--- stderr ---"; cat "$GATES/check-egress-spec.stderr.log"
  echo "exit_code=${SPEC_RC}"; echo "${END}"
  echo "## 工作树洁净度（含忽略文件）"; echo "git_status=[$(tree_status "$TD")]"
  echo "git_status_ignored=[$(git -C "$TD" status --porcelain --ignored 2>/dev/null | grep -c '^!!' || true) ignored entries]"
} > "$IMPL/check-egress-spec.log"
echo "check-egress-spec rc=${SPEC_RC} ${START} -> ${END}"

echo "=== C：make check-egress-spec-ci 交叉核对 $(utc_now)"
CROSS_START=$(utc_now); CROSS_RC=0
isolated_run "$TC" . "$PYCC" "$OUT/cross.stdout.log" "$OUT/cross.stderr.log" make check-egress-spec-ci || CROSS_RC=$?
{
  echo "# 交叉核对：候选 commit 自身上的 make check-egress-spec-ci（不含承接收据，预期仅冻结承接测试红）"
  echo "commit=${C}"; echo "${CROSS_START}"
  echo "executed_on=${ISOLATION}"
  echo "## make check-egress-spec-ci"; echo '$ make check-egress-spec-ci'
  cat "$OUT/cross.stdout.log"; echo "--- stderr ---"; cat "$OUT/cross.stderr.log"
  echo "exit_code=${CROSS_RC}"
  echo "## 工作树洁净度（含忽略文件）"; echo "git_status=[$(tree_status "$TC")]"
  echo "CROSS_DONE $(utc_now)"
} > "$IMPL/cross-check/check-egress-spec.C-only.local.log"
grep -n "^exit_code=\|CROSS_DONE\|🔴\|FAIL" "$IMPL/cross-check/check-egress-spec.C-only.local.log" | head -n 8 | cut -c1-200 || true

echo "=== DC：make test（full-regression）$(utc_now)"
DIRTY=$(tree_status "$TD" 3)
[ -z "$DIRTY" ] || { echo "full-regression 前测试树不干净：[${DIRTY}]"; false; }
START=$(utc_now); REG_RC=0
isolated_run "$TD" . "$PYCD" "$GATES/full-regression.stdout.log" "$GATES/full-regression.stderr.log" make test || REG_RC=$?
END=$(utc_now)
write_gate_json "$GATES/full-regression.gate.json" full-regression "$START" "$END" "$REG_RC" "$TD" . make test
echo "full-regression rc=${REG_RC} ${START} -> ${END}"; grep -o "Ran [0-9]* tests in [0-9.]*s" "$GATES/full-regression.stderr.log" | tail -n 3 || true
echo "git_status_after=[$(tree_status "$TD" 3)]"

echo "=== 交付：权限收口 → 上传清单 → READY $(utc_now)"
# 与 local-upload.sh 落地后的收口相同；只有 root 才改属主（离线测试以普通用户运行）。
if [ "$(id -u)" = 0 ]; then chown -R root:root "$GATES" "$IMPL"; fi
chmod -R u=rwX,go= "$GATES" "$IMPL"
python3 "$DRV/upload_manifest.py" create "$RUNROOT"
touch "$IMPL/READY"
stop_heartbeat
python3 - "$OUT/summary.json" "$SUBJECT" "$ROUND" "$C" "$DC" "$RECEIPT" "$SPEC_RC" "$CROSS_RC" "$REG_RC" <<'PY'
import json, sys
out, subject, round_id, c, dc, receipt, spec, cross, reg = sys.argv[1:]
payload = {"schema_version": "arm64-vc4-gates/v1", "subject_id": subject, "round": round_id,
           "candidate_commit": c, "executed_on_commit": dc, "freeze_successor_receipt": receipt,
           "status": "passed" if spec == "0" and reg == "0" else "failed",
           "gates": {"check-egress-spec": int(spec), "full-regression": int(reg)},
           "cross_check_c_only": {"exit_code": int(cross), "expected": "只允许缺冻结承接收据导致的预期失败，由操作员按日志确认"},
           "delivered": ["local-gates", "impl-logs"], "ready": True}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
chmod 600 "$OUT/summary.json"
trap - ERR
if [ "$SPEC_RC" = 0 ] && [ "$REG_RC" = 0 ]; then
  rm -rf "$TD" "$TC" "$PYCD" "$PYCC"
  echo "DC 两项门禁通过并已交付（交叉核对 C rc=${CROSS_RC}，按日志确认只差冻结承接收据）；测试树与字节码缓存已删除"
  echo "ARM64_VC4_GATES_DONE rc=0 subject=${SUBJECT}"
  exit 0
fi
echo "DC 门禁未通过（check-egress-spec rc=${SPEC_RC}，full-regression rc=${REG_RC}）；结论已交付并写 READY，测试树保留在 ${WORK}"
echo "ARM64_VC4_GATES_FAILED subject=${SUBJECT}"
exit 1
