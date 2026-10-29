#!/bin/bash
# ARM64 版 VC-4／VC-5 本地门禁（替代本机 driver/local/local-vc4.sh，门禁一律在采集主机的隔离测试树上执行）。
#
# 门禁分工沿用本机脚本的现有合同：
#   * DC（候选提交＋描述它的冻结承接收据）上执行 make check-egress-spec 与 make test 的全部内容，两项都必须通过——产出 VC-5 外部门禁的
#     check-egress-spec 与 full-regression 两项（local-gates 六件套），以及 VC-4 实现测试日志 impl-logs/check-egress-spec.log。
#     执行方式（修好接着跑）：经统一调度执行器一次运行（entry-gates.sh --profile vc4，组合 vc4 = make test 的组成，其中 check-egress-spec 门禁项即
#     make check-egress-spec 本身，不再跑两遍），产物由 vc4_gate_records.py 改写成原来的六件套与实现测试日志，文件名与 gate.json 字段不变；
#     记录库与 Go 缓存跨次运行保留（$WORK/entry-work、$(dirname $D)/unit-records），所以：
#       - 第一层（同提交）：重跑本脚本时 DC 不变、部署绑定不变，上一轮已通过且记录完整的单元直接沿用，只重跑失败、未完成的单元（缺省开）；
#       - 第二层（换提交）：缺省关——VC-4／VC-5 的 make test 保持全集（子集不能冒充 full-regression）。要用就显式设 VC4_GATE_SCOPE=auto，
#         此时 full-regression.gate.json 带 client_selection 标记，vc5-all.sh 默认拒绝带标记的本地门禁（设 VC5_ALLOW_SUBSET_LOCAL_GATES=1 才放行）；
#       - 环境修复不影响已通过单元时，用 VC4_RESUME_WAIVE_ENV="理由" 与 VC4_RESUME_WAIVE_ENV_NAMES="条目名,…" 显式豁免（记入清单，点名之外的差异仍拒绝沿用）；
#       - VC4_RESUME=off 关闭续跑沿用（整轮全集重跑）；
#   * C（候选提交自身）上只执行 make check-egress-spec-ci 作交叉核对（impl-logs/cross-check/check-egress-spec.C-only.local.log），
#     只允许缺冻结承接收据导致的预期失败：冻结承接收据按工具约束不得进入它所描述的提交，C 上的冻结承接测试必然红。
#     本脚本只记录交叉核对结果、不据此判定，由操作员按日志确认失败项只有冻结承接测试。
# 产物直接写入 $RUNROOT/local-gates 与 $RUNROOT/impl-logs（旧目录先整体归档），文件名、gate.json 字段与日志格式都与本机
# 上传逐字段相同，随后用 upload_manifest.py 生成上传清单并写 READY：vc4-all.sh 的上传等待与清单核验、vc5-all.sh 与
# build_gate_facts.py 都不用改（门禁收据只要求 target-platform 一项的架构等于候选架构）。
# 门禁期间每 30 秒更新 impl-logs/HEARTBEAT；门禁结论（rc 非零）照常交付并写 READY，由 vc4-all.sh 与 VC-5 按日志和 gate.json
# 判定；脚本自身失败（bundle、测试树、前端依赖、字节码缓存、并发锁、提交链不符、门禁之后测试树不干净）不交付、不写 READY。
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
WORK="$RUNROOT/arm64-vc4-gates"; TC="$WORK/wt-C"; PYCD="$WORK/pycache-D"; PYCC="$WORK/pycache-C"; EGW="$WORK/entry-work"; TD="$EGW/test-tree"
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
echo "=== 测试树：C 一棵（clone_test_tree，umask 022；DC 的树由入口门禁 entry-gates.sh 自己克隆）$(utc_now)"
umask 022
clone_test_tree "$TC" "$BUNDLE" "$BUNDLE_BRANCH" "$C"
# DC 必须恰好是 C 加上本轮承接收据这一个文件，否则 C 上的交叉核对不能说明“只差承接收据”。bundle 已取进 C 的树，DC 的对象在里面，不必检出。
git -C "$TC" merge-base --is-ancestor "$C" "$DC" || { echo "提交链不符：C 不是 DC 的祖先"; false; }
CHANGED=$(git -C "$TC" diff --name-only "$C" "$DC")
[ "$CHANGED" = "$RECEIPT" ] || { echo "提交链不符：C..DC 的改动必须只有 ${RECEIPT}，实际 [${CHANGED}]"; false; }
if [ ! -d "$NM_DIR/node_modules" ] || ! cmp -s "$NM_DIR/pnpm-lock.yaml" "$TC/frontend/pnpm-lock.yaml"; then
  echo "前端依赖不可用：${NM_DIR}/node_modules 不存在，或 ${NM_DIR}/pnpm-lock.yaml 与 ${TC}/frontend/pnpm-lock.yaml 不同"
  false
fi
cp -a "$NM_DIR/node_modules" "$TC/frontend/node_modules"
umask 077
# 洁净度检查先于第一次使用定义（10-01 的版本在定义之前调用，开头两行只打印出 command not found）。
tree_status() { git -C "$1" status --porcelain --untracked-files=all 2>&1 | head -n "${2:-1000000}" || true; }
echo "wt-C HEAD=$(git -C "$TC" rev-parse HEAD) status=[$(tree_status "$TC")]"
echo "=== 树外字节码缓存（C；DC 的缓存由入口门禁自己建）$(utc_now)"
env -u PYTHONPATH python3 "$DRV/bytecode_cache.py" "$PYCC" "$TC/tools" | tail -n 1 | cut -c1-300
# 命令替换里一律 `|| true`：set -E 会把 ERR 陷阱带进命令替换，工具缺失或 head 截断触发的 SIGPIPE 不能把中止信息混进日志。
host_line() { echo "host=$(uname -srm)"; echo "go: $(go version 2>&1 || true)"; echo "python: $(python3 --version 2>&1 || true)"; echo "node: $(node --version 2>&1 || true)"; }
ISOLATION="采集主机隔离测试树（私有挂载命名空间里只读 tmpfs 遮住 /root/oauth-capture，树外只读字节码缓存）"

echo "=== DC：统一执行器一次运行（组合 vc4 = make check-egress-spec 与 make test 的全部内容）$(utc_now)"
RUN_START=$(utc_now); EG_RC=0
EG_ARGS=(--profile vc4 --out "$WORK/$SUBJECT/entry" --work "$EGW" --pycache "$PYCD")
BINDING_EXTRA=$(python3 -B -c 'import json, sys; print(json.dumps({"vc4": {"candidate": sys.argv[1], "executed": sys.argv[2], "receipt": sys.argv[3]}}))' "$C" "$DC" "$RECEIPT")
if [ "${VC4_RESUME:-on}" != off ]; then
  EG_ARGS+=(--resume-from auto --resume-series vc4 --resume-binding-extra "$BINDING_EXTRA")
  if [ -n "${VC4_RESUME_WAIVE_ENV:-}" ]; then EG_ARGS+=(--resume-waive-env "$VC4_RESUME_WAIVE_ENV" --resume-waive-env-names "${VC4_RESUME_WAIVE_ENV_NAMES:?环境豁免要点名变化的指纹条目（VC4_RESUME_WAIVE_ENV_NAMES）}"); fi
fi
# 第二层默认关：VC-4 的 make test 保持全集；显式 VC4_GATE_SCOPE=auto 才按客户端筛选（见文件头）。
if [ "${VC4_GATE_SCOPE:-full}" = auto ]; then
  echo "注意：VC4_GATE_SCOPE=auto——本次 full-regression 可能是 Codex 计划的子集（gate.json 带 client_selection），vc5-all.sh 默认拒绝"
  EG_ARGS+=(--gate-scope auto)
fi
bash "$DRV/entry-gates.sh" "${EG_ARGS[@]}" "$BUNDLE" "$BUNDLE_BRANCH" "$DC" "$NM_DIR" || EG_RC=$?
RUN_END=$(utc_now)
if [ "$EG_RC" != 0 ] && [ "$EG_RC" != 1 ]; then
  echo "入口门禁没有给出结论（rc=${EG_RC}），原因见上方输出；主体目录 $WORK/$SUBJECT/entry"; false
fi
# 入口门禁退出码 1 有两种来源：门禁项没过（门禁结论，照常交付），或者门禁项都过了、却在门禁之后发现测试树不干净（或运行清单自检没过）——后者是脚本自身
# 的失败，不是门禁结论：原脚本在 check-egress-spec 之后、make test 之前查树洁净度，脏就中止（退出 3、不交付、不写 READY、保留现场）；改走统一执行器后
# 这道守卫由入口门禁在门禁之后的树洁净度检查承担，退出码 1 但每个门禁项都通过的矛盾态就是它拦下的，这里按同样的口径中止，不能当“通过”交付。
if [ "$EG_RC" = 1 ] && python3 -B -c 'import json, sys; d = json.load(open(sys.argv[1])); sys.exit(0 if d.get("gates") and all(g.get("status") == "passed" for g in d["gates"]) else 1)' \
    "$WORK/$SUBJECT/entry/entry-gates.json" 2>/dev/null; then
  echo "入口门禁退出码 1，但每个门禁项都通过：门禁之后测试树不干净（或运行清单自检没过），不是门禁结论；不交付、不写 READY，现场保留在 $WORK/$SUBJECT/entry 与 $EGW"
  false
fi
echo "=== 门禁产物：改写成 local-gates 六件套与 impl-logs/check-egress-spec.log $(utc_now)"
RECORDS_OUT=$(python3 -B "$DRV/vc4_gate_records.py" build --entry-out "$WORK/$SUBJECT/entry" --gates-dir "$GATES" --impl-dir "$IMPL" --round "$ROUND" \
  --candidate "$C" --executed "$DC" --receipt "$RECEIPT" --isolation "$ISOLATION")
echo "$RECORDS_OUT"
SPEC_RC=$(sed -n 's/.*SPEC_RC=\([0-9]*\).*/\1/p' <<< "$RECORDS_OUT"); REG_RC=$(sed -n 's/.*REG_RC=\([0-9]*\).*/\1/p' <<< "$RECORDS_OUT")
[ -n "$SPEC_RC" ] && [ -n "$REG_RC" ] || { echo "门禁产物没写出退出码"; false; }
echo "check-egress-spec rc=${SPEC_RC} full-regression rc=${REG_RC} ${RUN_START} -> ${RUN_END}"
if [ -f "$WORK/$SUBJECT/entry/entry-gates.json" ]; then
  python3 -B -c 'import json, sys; d = json.load(open(sys.argv[1])); r = d.get("resume") or {}; i = d.get("inheritance") or {}; print("续跑沿用：%s，沿用 %s 个单元，执行 %s 个单元；来源 %s" % (r.get("status", "未启用"), i.get("inherited"), i.get("executed"), (r.get("source") or {}).get("run_id")))' "$WORK/$SUBJECT/entry/entry-gates.json" || true
fi

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
