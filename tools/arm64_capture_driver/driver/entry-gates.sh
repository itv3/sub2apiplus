#!/bin/bash
# 入口门禁（E2-04）：一次运行跑完入口要的全部门禁——采集工具测试、check-egress-spec 的各个子检查、后端三组测试、三组
# lint、前端检查、部署脚本测试与 pre-A3 场景——全部交给统一调度执行器（驱动随附的 unit_executor.py run-gates），在整机
# 额度内并行，一项失败其余照跑，全部跑完再按门禁项汇总。P0 证据、VC-0 预跑记录、部署前全量门禁记录与 pre-A3 认证都取自
# 这一次运行（entry_gates.py export 导出；pre-A3 认证由认证模块 issue 核对场景全集与网络计数后签发）。
#
# 组合（--profile）：
#   entry（默认）：全部门禁项＋pre-A3 场景。要求数据根已部署本提交（最新部署收据的整树摘要等于测试树的受管树）；本轮
#     pre-A3 认证已有、或有工具身份与策略未变的可复用认证时沿用它，pre-A3 场景不纳入本次运行；
#   full-gates：不含 pre-A3（部署前全量门禁，arm64-full-gates.sh 调用）；
#   preflight：只含 make test 的组成（VC-0 预跑，vc0-gate-target.sh 调用）。
# 隔离：测试树里的单元在私有挂载命名空间里遮住 /root/oauth-capture（与 lib.sh 的 isolated_run 同一做法），树外只读字节码
#   缓存；pre-A3 场景在数据根的生产布局里运行（受管树经生产别名访问的分支也要覆盖到），用生产字节码共享层与身份记忆化。
# Linux 上不执行 macOS 专用的部署脚本测试（写进门禁记录的 not_executed；CI 在 macos-15 上照常执行）。
#
# 产物（主体目录 --out，默认 $RUNROOT/entry-gates/entry-gates-<UTC 时间戳>）：entry-gates.json（总摘要）、logs/<门禁项>.gate.json
#   与 logs/full-regression.gate.json（make test 的组成全部通过与否）、p0/{check-egress-spec,test-capture-tools}.json（P0 证据）、
#   preflight.json（VC-0 预跑记录）、full-gates-summary.json（全量门禁记录，full-gates／entry）、pre-a3-executor-summary.json、
#   executor/（执行器记录与逐单元日志）、executor.log。不写候选门禁目录、候选目录、时间账本与 Campaign，零模型请求。
#
# 用法（采集主机 root；make test 里有挂断检测用例，必须 setsid -f 启动，不能 nohup）：
#   ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash entry-gates.sh [--profile entry|full-gates|preflight] [--out <主体目录>] \
#     [--work <测试树与缓存所在目录>] [--pycache <字节码缓存目录>] <bundle> <分支> <40 位提交> [<前端依赖目录>] \
#     > $RUNROOT/entry-gates.out 2>&1 < /dev/null
# 退出码：0 全部门禁通过（entry 组合还要 pre-A3 认证签发或沿用成功）；1 有门禁未通过（门禁结论，测试树保留供排查）；
#   2 用法错误；3 准备或执行失败（bundle、测试树、前端依赖、字节码缓存、部署不一致、并发锁、执行器出错），没有门禁结论。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight] [--out <目录>] [--work <目录>] [--pycache <目录>] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }
PROFILE=entry; OUT=""; WORK="$RUNROOT/entry-gates"; PYC=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --out) OUT="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --work) WORK="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --pycache) PYC="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --) shift; break ;;
    -*) usage; exit 2 ;;
    *) break ;;
  esac
done
if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then usage; exit 2; fi
PBUNDLE="$1"; PBRANCH="$2"; PCOMMIT="$3"; NM_DIR="${4:-$HISTORY_TEST_TREE/frontend}"
case "$PROFILE" in entry|full-gates|preflight) ;; *) echo "未知的门禁组合：$PROFILE" >&2; exit 2 ;; esac
if [[ "$PBUNDLE" != /* ]] || [ -L "$PBUNDLE" ] || [ ! -f "$PBUNDLE" ]; then echo "bundle 必须是已存在的普通文件（绝对路径）：$PBUNDLE" >&2; exit 2; fi
if ! [[ "$PBRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then echo "分支名只允许字母、数字与 ._/-：$PBRANCH" >&2; exit 2; fi
if ! [[ "$PCOMMIT" =~ ^[0-9a-f]{40}$ ]]; then echo "提交必须是完整 40 位小写 sha1：$PCOMMIT" >&2; exit 2; fi
for dir in "$NM_DIR" "$WORK" ${OUT:+"$OUT"} ${PYC:+"$PYC"}; do
  if [[ "$dir" != /* ]]; then echo "目录必须是绝对路径：$dir" >&2; exit 2; fi
done
TREE="$WORK/test-tree"; PYC="${PYC:-$WORK/pycache}"
on_error() {
  local rc=$? line="$1"
  trap - ERR
  echo "ENTRY_GATES_ABORTED：准备或执行失败（entry-gates.sh 第 ${line} 行，rc=${rc}），没有门禁结论；原因见上方输出，主体目录 ${OUT:-（未建立）}"
  exit 3
}
trap 'on_error $LINENO' ERR
mkdir -p "$WORK"; chmod 700 "$WORK"
# 同一工作目录只允许一次入口门禁（共用测试树与缓存）：mkdir 原子锁，持有者已不在时回收陈旧锁。
LOCK="$WORK/.entry-gates.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then echo "ENTRY_GATES_ABORTED：已有入口门禁在运行（PID ${HOLDER}），拒绝并发"; exit 3; fi
  rm -rf "$LOCK"; mkdir "$LOCK"
fi
printf '%s\n' "$$" > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT
if [ -z "$OUT" ]; then
  OUT="$RUNROOT/entry-gates/entry-gates-$(date -u +%Y%m%dt%H%M%Sz)"
  if [ -e "$OUT" ]; then OUT="$OUT-$$"; fi
fi
SUBJECT=$(basename "$OUT")
# 主体目录可以由调用方预先建好（VC-0 预跑先在里面写 gate_before 环境收据），但不能已有门禁记录（不覆盖上一次的结论）。
mkdir -p "$(dirname "$OUT")"; mkdir -p -m 0700 "$OUT"; chmod 700 "$OUT"
if [ -e "$OUT/logs" ] || [ -e "$OUT/entry-gates.json" ]; then echo "ENTRY_GATES_ABORTED：主体目录里已有门禁记录：${OUT}"; exit 3; fi
mkdir -m 0700 "$OUT/logs"
echo "=== 入口门禁 ${SUBJECT}（组合 ${PROFILE}）$(utc_now)"
echo "bundle=${PBUNDLE} 分支=${PBRANCH} 提交=${PCOMMIT} 前端依赖=${NM_DIR}"
echo "=== 测试树（clone_test_tree，umask 022）$(utc_now)"
umask 022
clone_test_tree "$TREE" "$PBUNDLE" "$PBRANCH" "$PCOMMIT"
if [ ! -d "$NM_DIR/node_modules" ] || ! cmp -s "$NM_DIR/pnpm-lock.yaml" "$TREE/frontend/pnpm-lock.yaml"; then
  echo "前端依赖不可用：${NM_DIR}/node_modules 不存在，或 ${NM_DIR}/pnpm-lock.yaml 与本树 frontend/pnpm-lock.yaml 不同"
  echo "本轮 lockfile 有变化时，先按 frontend.sh 同一方式（node:20 容器内 pnpm install --frozen-lockfile）在独立目录装好依赖，再把该目录作为最后一个参数传入"
  echo "ENTRY_GATES_ABORTED：前端依赖不可用，没有门禁结论；主体目录 ${OUT}"
  exit 3
fi
cp -a "$NM_DIR/node_modules" "$TREE/frontend/node_modules"
umask 077
TREE_HEAD=$(git -C "$TREE" rev-parse HEAD)
# 命令替换里一律 `|| true`：set -E 会把 ERR 陷阱带进命令替换，head 截断触发的 SIGPIPE 不能误判为准备失败。
tree_status() { git -C "$TREE" status --porcelain --untracked-files=all 2>&1 | head -n "${1:-1000000}" || true; }
echo "test-tree HEAD=${TREE_HEAD} status=[$(tree_status)]"
echo "=== 树外字节码缓存 ${PYC} $(utc_now)"
env -u PYTHONPATH python3 "$DRV/bytecode_cache.py" "$PYC" "$TREE/tools" | tail -n 1 | cut -c1-300

PRE_A3_MODE=none; PRE_A3_ARGS=(); PA3_ROOT=""
if [ "$PROFILE" = entry ]; then
  echo "=== pre-A3 认证：部署绑定与沿用判断 $(utc_now)"
  DEPLOY=$(python3 -B - "$DRV/../install.py" "$D" <<'PYDEPLOY'
import importlib.util, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('installed_driver', sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
print(module.latest_deploy_receipt(Path(sys.argv[2])/'control')[0])
PYDEPLOY
)
  # 入口门禁验的是测试树，pre-A3 跑的是数据根的受管树：两者必须是同一提交（部署收据的整树摘要等于测试树受管树）。
  TREE_FILES=$(cd "$TREE" && env -u CODEX_UPGRADE_IDENTITY_MEMO PYTHONPATH=. PYTHONPYCACHEPREFIX="$PYC" python3 -c \
    "from tools.official_client_capture import codex_upgrade_policy_certification as pc; print(pc.current_identity()['tool_files_sha256'])")
  DEPLOY_FILES=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['tool_files_sha256'])" "$DEPLOY")
  if [ "$TREE_FILES" != "$DEPLOY_FILES" ]; then
    echo "数据根部署的受管树（${DEPLOY_FILES}）不是本提交（测试树受管树 ${TREE_FILES}）：先受监督部署本提交，再跑入口门禁"
    echo "ENTRY_GATES_ABORTED：部署与测试树不一致，没有门禁结论；主体目录 ${OUT}"
    exit 3
  fi
  [ -f "$POLICY_COMPAT_RECEIPT" ] || python3 -m tools.official_client_capture.codex_upgrade_policy_certification compatibility --previous-policy "${PREVIOUS_POLICY:?缺少前序策略文件}" --output "$POLICY_COMPAT_RECEIPT" | cut -c1-160
  [ -f "$POLICY_ACTIVATION" ] || python3 -m tools.official_client_capture.codex_upgrade_policy_certification activation --deployment-receipt "$DEPLOY" --compatibility-receipt "$POLICY_COMPAT_RECEIPT" --output "$POLICY_ACTIVATION" | cut -c1-160
  if [ -f "$PRE_A3_CERTIFICATION" ]; then
    # 已有的本轮认证先按 stage1 同一口径复核（无效即在跑门禁之前停下，不白跑一遍）。
    python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification find-reusable --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" >/dev/null
    PRE_A3_MODE=present; echo "PRE_A3_PRESENT $PRE_A3_CERTIFICATION（不纳入本次运行）"
  elif REUSE=$(python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification find-reusable --search-root "$(dirname "$PRE_A3_CERTIFICATION")" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION"); then
    cp -p "$REUSE" "$PRE_A3_CERTIFICATION"; chmod 600 "$PRE_A3_CERTIFICATION"
    PRE_A3_MODE=reused; echo "PRE_A3_REUSED $REUSE（不纳入本次运行）"
  else
    PRE_A3_MODE=run
    UNITS="$OUT/pre-a3-units.json"
    PA3_ROOT=$(python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification plan --staging-root "$D/staging/pre-a3-certification-$STAMP" --output "$UNITS" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["staging_root"])')
    PRE_A3_ARGS=(--pre-a3-units "$UNITS" --pre-a3-env "PYTHONPATH=." --pre-a3-env "CODEX_UPGRADE_IDENTITY_MEMO=$CODEX_UPGRADE_IDENTITY_MEMO")
    if [ -d "$PYC_MANAGED" ]; then PRE_A3_ARGS+=(--pre-a3-env "PYTHONPYCACHEPREFIX=$PYC_MANAGED"); fi
    echo "pre-A3 场景纳入本次运行：认证根 ${PA3_ROOT}"
  fi
fi
PLAN_PROFILE="$PROFILE"
if [ "$PROFILE" = entry ] && [ "$PRE_A3_MODE" != run ]; then PLAN_PROFILE=full-gates; fi
LAUNCHER='["unshare","-m","--propagation","private","bash","-c","mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec \"$@\"","entry-gates"]'
ISOLATION="测试树单元：unshare -m --propagation private，只读 tmpfs 遮住 /root/oauth-capture（生产别名），树外只读字节码缓存；pre-A3 场景：数据根生产布局"
TS="$TREE/frontend/node_modules/typescript/lib/typescript.js"
echo "=== 门禁清单（${PLAN_PROFILE}）$(utc_now)"
python3 -B "$DRV/entry_gates.py" plan --tree "$TREE" --profile "$PLAN_PROFILE" --launcher-json "$LAUNCHER" --typescript-module "$TS" \
  ${PRE_A3_ARGS[@]+"${PRE_A3_ARGS[@]}"} --output "$OUT/gates-manifest.json" | cut -c1-400
echo "=== 一次运行：统一调度执行器 run-gates（记录 ${OUT}/executor）$(utc_now)"
EXEC_RC=0
( cd "$TREE" && env -u CODEX_UPGRADE_IDENTITY_MEMO PYTHONPATH=. PYTHONPYCACHEPREFIX="$PYC" CODEX_0_149_1_SOURCE_ROOT="$HISTORICAL_SOURCE_ROOT" \
    CAPTURE_TYPESCRIPT_MODULE="$TS" python3 "$DRV/unit_executor.py" run-gates --manifest "$OUT/gates-manifest.json" --out-dir "$OUT/executor" ) \
  > "$OUT/executor.log" 2>&1 < /dev/null || EXEC_RC=$?
tail -n 25 "$OUT/executor.log" | cut -c1-240
if [ "$EXEC_RC" -gt 1 ] || [ ! -f "$OUT/executor/summary.json" ]; then
  echo "ENTRY_GATES_ABORTED：执行器出错（rc=${EXEC_RC}），没有门禁结论；日志 ${OUT}/executor.log"
  exit 3
fi
echo "=== 导出门禁记录、P0 证据与预跑／全量门禁记录 $(utc_now)"
python3 -B "$DRV/entry_gates.py" export --manifest "$OUT/gates-manifest.json" --summary "$OUT/executor/summary.json" --out "$OUT" \
  --subject "$SUBJECT" --round "$ROUND" --target-version "${TARGET_VERSION:-}" --tree "$TREE" --isolation "$ISOLATION" \
  --host "$(hostname -s)" --architecture "$(python3 -c 'import platform; print(platform.system().lower() + "/" + platform.machine())')" \
  --bytecode-cache "$PYC" --source "bundle=$PBUNDLE" --source "branch=$PBRANCH" --source "commit=$PCOMMIT" --source "tree_head=$TREE_HEAD" \
  --source "history_test_tree=$HISTORY_TEST_TREE" --source "node_modules_source=$NM_DIR/node_modules" --source "pre_a3=$PRE_A3_MODE" | cut -c1-600
GATES_STATUS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['status'])" "$OUT/entry-gates.json")
PRE_A3_OK=true
if [ "$PRE_A3_MODE" = run ]; then
  echo "=== pre-A3 认证签发（核对场景全集与网络计数）$(utc_now)"
  if ! python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification issue --staging-root "$PA3_ROOT" --executor-summary "$OUT/pre-a3-executor-summary.json" \
      --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" --output "$PRE_A3_CERTIFICATION" | cut -c1-300; then
    PRE_A3_OK=false
  fi
fi
if [ "$PRE_A3_MODE" != none ] && [ "$PRE_A3_OK" = true ]; then
  # 与 stage1 建账本前同一口径复核本轮坐标；跨部署复用登记复用收据（同一组绑定不重复写）。
  python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification find-reusable --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" >/dev/null
  python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification record-reuse --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" --receipt-root "$(dirname "$PRE_A3_CERTIFICATION")" | cut -c1-300
  echo "PRE_A3_DONE $PRE_A3_CERTIFICATION（${PRE_A3_MODE}）"
fi
AFTER_STATUS=$(tree_status 5)
trap - ERR
if [ "$GATES_STATUS" = passed ] && [ "$PRE_A3_OK" = true ] && [ -z "$AFTER_STATUS" ]; then
  rm -rf "$TREE" "$PYC"
  echo "入口门禁通过：总摘要 ${OUT}/entry-gates.json；测试树与字节码缓存已删除"
  echo "ENTRY_GATES_DONE rc=0 subject=${SUBJECT} out=${OUT}"
  exit 0
fi
echo "入口门禁未通过：$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(' '.join(g['gate_id'] for g in d['gates'] if g['status'] != 'passed') or '（门禁项都通过）')" "$OUT/entry-gates.json")"
if [ "$PRE_A3_OK" != true ]; then echo "pre-A3 认证没有签发：没通过的认证只写旁路文件 $(dirname "$PRE_A3_CERTIFICATION")/*.failed-*.json，修好后同一 STAMP 直接重跑"; fi
if [ -n "$AFTER_STATUS" ]; then echo "门禁后测试树不干净：[${AFTER_STATUS}]"; fi
grep -E "^(单元未通过|诊断重跑|测试组 .*全集核对失败)" "$OUT/executor.log" | head -n 20 | cut -c1-240 || true
echo "  总摘要 ${OUT}/entry-gates.json；执行器日志 ${OUT}/executor.log；测试树保留在 ${TREE}（排查用，下次运行会重建）"
echo "ENTRY_GATES_FAILED subject=${SUBJECT} out=${OUT}"
exit 1
