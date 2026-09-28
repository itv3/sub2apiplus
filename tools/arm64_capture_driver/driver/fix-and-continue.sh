#!/bin/bash
# 修好接着跑一条命令（第 35 项，老板 2026-09-28 批准）：把每轮"修复 → 部署 → 登记 → 对账 → 批准 → 重派"编排成一条命令。
#
# 此前每轮由人按 sed 复制改写 upload-rN（deploy／postdeploy／resume）与 repair-rN（实测／回归收据／根因修复登记）
# 十几个步骤逐步执行，人工间隙 15～30 分钟、容易漏改。本脚本随驱动清单受管，每轮只换一份轮次参数文件：
#
#   用法（采集主机 root；推荐后台运行、输出写日志）：
#     setsid -f bash /root/arm64-capture-driver/driver/fix-and-continue.sh <轮次参数文件> [--from <步骤>] \
#       > $RUNROOT/fix-and-continue-<轮次>.out 2>&1 < /dev/null
#     bash .../fix-and-continue.sh <轮次参数文件> --list        # 只列本轮各步骤记录
#   参数模板：driver/fix-and-continue.example.params（KEY=VALUE，经 parse_env.py 同一词法层安全解析，绝不 source）。
#
# 步骤（按序；每步幂等，--from <步骤> 续跑时前序步骤在本轮必须有 passed／skipped 记录）：
#   deploy            bundle 校验 → 取分支核对 HEAD → staging 干净检出 → 仓库文档 → 属主收口 → 后台受监督部署并等待；
#                     staging 已是同 HEAD 干净检出且最新部署收据 passed、摘要与 EXPECT 一致则跳过
#   postdeploy        部署收据与期望摘要（整树／五摘要／监督器）、数据根监督器文件与 wire 闭包 → 同步数据根部署脚本副本
#                     → 出口守护代码与状态核对（守护逻辑变了只停下，不重装守护）→ 驱动重装与复验 → 入口断言
#   item-tests        部署用 staging 树上实测（setsid 启动、SIGHUP 复位、字节码缓存放树外），日志 exit=0 且
#                     staging-clean=yes、各段 OK 才继续；本轮已通过则跳过
#   evolution         tool-evolution-status 无未登记漂移则跳过；否则预览（wire 闭包变化等与 EXPECT 不符即停）→ 按预览
#                     review_sha256 与批准人登记 → 复核无漂移
#   pre-extend        给了 EXTEND_DEADLINE 且当前阶段就是 EXTEND_PHASE、阶段截止早于它：对账前先延期（阶段截止在对账前
#                     已过时，计时账本是 deadline_paused，对账判预算暂停、拒绝批准；候选审核等阶段未开的状态跳过）
#   reconcile-runs    扫描监督器状态目录链尾（最后一个正常结束的父 run 之后）终态 failed／watchdog-aborted 且缺对账收据的
#                     父 run：run 期间无预约的 reconcile-supervisor-run，有预约的（监督器同一判据）reconcile-attempt，
#                     父 run 对账提示"属于 attempt 中断"时按提示改走 reconcile-attempt；目标 attempt 留给下一步。
#                     对账判"项目总账根因达上限"暂停且参数给了登记材料（与 reconcile-attempt 同一判据）时，在本步骤内按
#                     repair 步骤同一路径登记（root_cause_repair inline），再对该对象重新对账一次，仍暂停即停下；没给材料
#                     停下时续跑步骤就是本步骤。上次停在根因暂停的对象（收据虽已写）续跑时重新对账（第 59 项）
#   reconcile-attempt 目标 attempt 对账；账务暂停／环境污染／永久停线／请求预算／需审核一律停下，不越权
#   repair            给了 REPAIR_ROOT_CAUSES 才登记根因修复（同一修复提交已登记则跳过；回归收据缺失时可由草稿补本轮
#                     实测日志与部署收据写一次）；对账因根因达上限暂停而没给材料时停下（root_cause_repair step）
#   approve           重新对账取同一次运行的恢复预览 review_sha256 → --approve-recovery-sha256
#   authorize         --authorize-recovery-preview（批准步骤记录的预览路径）
#   extend            给了 EXTEND_DEADLINE 时授权后再做一次（候选审核重开阶段后才能阶段延期；已延期则跳过）
#   accepted          已批准预览仍被接受（授权未生效时不做，避免接受检查顺带写授权事件）
#   recover           后台启动 vc5-recover.sh（旧 vc5-recover.out／vc5-run-batch.out 改名留档；本轮已启动过则不重复派发）
#
# 相对设计稿（batch3-r1-r3-r5-design.md 第 35 项）的顺序偏离：
#   1. 根因修复登记放在对账之后、批准之前（根因要先由对账入账；登记后批准步骤的重新对账会验证是否解除暂停）；
#   2. 延期不放在"批准与授权之间"：延期 apply 会在 Campaign 计时账本追加 deadline_extended 事件，授权消费预览时要求账本
#      head 仍是预览冻结的 head（否则"恢复批准消费前 Campaign 账本 head 已推进，必须重新对账"），该位置必然让授权失败；
#      改为对账前 pre-extend ＋ 授权后 extend 两处，同一参数、各自幂等；
#   3. 第 59 项：reconcile-runs 也会遇"项目总账根因达上限"暂停，但它在 repair 之前，停下后 --from repair／reconcile-attempt
#      都过不了前序核对。不调步骤顺序（repair 仍须在 reconcile-attempt 入账之后），而把登记抽成共用函数 root_cause_repair：
#      repair 步骤与 reconcile-runs 步骤内登记走同一判定、同一命令、同一核对。
#
# 停下与续跑：任一步失败或需要人工时写 $RUNROOT/fix-and-continue/<轮次>/<步骤>.json 并打印原因、下一步与
#   "续跑：bash … --from <步骤>"；所有受管命令的 stdout／stderr／退出码原样落在同目录 raw/。
# 退出码：0 完成；1 失败；2 参数或用法错误；3 本轮已有实例在跑；4 需要人工处理；5 驱动已被本轮重装更新（用新驱动续跑）。
# 绝不做：accounting-resolve、environment-isolate、campaign-resume、request-budget-extend、任何 --force、重装出口守护。
set -Eeuo pipefail
umask 077
DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FCPY="$DRV/fix_and_continue.py"
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH

usage() {
  echo "用法：bash $0 <轮次参数文件> [--from <步骤>] [--list]" >&2
  echo "步骤：$(python3 "$FCPY" steps < /dev/null | tr '\n' ' ')" >&2
}

PARAMS_ARG=""
FROM=""
LIST=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --from)
      if [ "$#" -lt 2 ]; then usage; exit 2; fi
      FROM="$2"; shift 2 ;;
    --from=*) FROM="${1#--from=}"; shift ;;
    --list) LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "未知选项：$1" >&2; usage; exit 2 ;;
    *)
      if [ -n "$PARAMS_ARG" ]; then echo "只接受一个参数文件" >&2; exit 2; fi
      PARAMS_ARG="$1"; shift ;;
  esac
done
if [ -z "$PARAMS_ARG" ]; then usage; exit 2; fi
if [ "$(id -u)" != 0 ]; then echo "必须以 root 执行（部署、属主收口与受管工具都要求 root）" >&2; exit 2; fi

EXPORTS=$(python3 "$FCPY" load-params "$PARAMS_ARG" < /dev/null) || exit 2
eval "$EXPORTS"
unset EXPORTS
PARAMS="$PARAMS_PATH"
ST="$STAGING_TREE"
STEPS_LIST=$(python3 "$FCPY" steps < /dev/null)
TS="$(date -u +%Y%m%dt%H%M%Sz)-$$"
STEP=preflight

if [ "$LIST" = 1 ]; then python3 "$FCPY" list --params "$PARAMS" < /dev/null; exit 0; fi
if [ -z "$FROM" ]; then FROM=deploy; fi
VALID=0
for s in $STEPS_LIST; do
  if [ "$s" = "$FROM" ]; then VALID=1; fi
done
if [ "$VALID" != 1 ]; then echo "未知步骤：${FROM}（可选：$(echo $STEPS_LIST)）" >&2; exit 2; fi

mkdir -p "$OUT/raw"
chmod 700 "$OUT" "$OUT/raw"
# 同一轮次只允许一个实例：mkdir 原子锁；持有者已不在时回收陈旧锁。
LOCK="$OUT/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then
    echo "本轮已有 fix-and-continue 在运行（PID ${HOLDER}），拒绝并发" >&2
    exit 3
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK"
fi
printf '%s\n' "$$" > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

if [ "$FROM" != deploy ]; then
  python3 "$FCPY" check-from --params "$PARAMS" --step "$FROM" < /dev/null || exit 2
fi
SELF_SHA=$(python3 "$FCPY" self-digest < /dev/null)

on_error() {
  local rc=$? line="$1"
  trap - ERR
  python3 "$FCPY" stop --params "$PARAMS" --step "$STEP" --run-stamp "$TS" --status failed \
    --reason "fix-and-continue.sh 第 ${line} 行命令意外失败（rc=${rc}）" --next "查看上方输出与 $OUT/raw/" < /dev/null || true
  exit "$rc"
}
trap 'on_error $LINENO' ERR

# decide <判定名> [判定参数…]：eval 判定输出的赋值；0 继续、10 幂等跳过（DRC=10，记录已写），其余即停（记录已写）。
decide() {
  local name="$1" out
  shift
  DRC=0
  out=$(python3 "$FCPY" decide "$name" --params "$PARAMS" --step "$STEP" --run-stamp "$TS" "$@" < /dev/null) || DRC=$?
  if [ "$DRC" != 0 ] && [ "$DRC" != 10 ]; then exit "$DRC"; fi
  if [ -n "$out" ]; then eval "$out"; fi
  return 0
}
skipped() { [ "$DRC" = 10 ]; }
# fail_step <原因> <下一步> [续跑步骤]：写失败记录、打印下一步并退出。
fail_step() {
  local rc=0
  trap - ERR
  python3 "$FCPY" stop --params "$PARAMS" --step "$STEP" --run-stamp "$TS" --status failed --reason "$1" --next "$2" \
    ${3:+--resume-from "$3"} < /dev/null || rc=$?
  exit "$rc"
}
# managed <模块> <标签> <参数…>：在数据根执行受管工具 CLI，stdout／stderr／退出码落到 $RAW.{out,err,rc}。
managed() {
  local module="$1" label="$2" rc=0
  shift 2
  RAW="$OUT/raw/$TS-$STEP-$label"
  (cd "$D" && PYTHONPATH="$D" exec python3 -m "tools.official_client_capture.$module" "$@") > "$RAW.out" 2> "$RAW.err" < /dev/null || rc=$?
  printf '%s\n' "$rc" > "$RAW.rc"
  echo "  [$STEP] $module $1 → rc=${rc}（$RAW.out）"
}
# wait_marker <日志> <总秒数> <PID 文件>：等 ^exit= 标记；进程先退出或超时即失败。
wait_marker() {
  python3 "$DRV/wait_state.py" marker "$1" --regex '^exit=' --max-seconds "$2" --pid-file "$3" --startup-seconds 120 < /dev/null
}

step_deploy() {
  decide deploy-state
  if skipped; then return 0; fi
  if [ "$ACTION" = deploy ]; then
    git -C "$SRC" bundle verify "$BUNDLE" > "$OUT/raw/$TS-deploy-bundle-verify.log" 2>&1 < /dev/null \
      || fail_step "bundle 校验失败：$BUNDLE" "重新上传 bundle 并核对 sha256 清单后 --from deploy"
    local ref="refs/heads/deploy-$ROUND-$TS" got
    git -C "$SRC" fetch -q "$BUNDLE" "refs/heads/$BUNDLE_BRANCH:$ref" < /dev/null \
      || fail_step "从 bundle 取分支 $BUNDLE_BRANCH 失败" "核对 BUNDLE_BRANCH 与 bundle 内容"
    got=$(git -C "$SRC" rev-parse "$ref")
    if [ "$got" != "$HEAD_COMMIT" ]; then
      fail_step "bundle 分支 $BUNDLE_BRANCH 的 HEAD 是 ${got}，不是 HEAD_COMMIT" "核对本轮参数与 bundle"
    fi
    if [ "$REUSE_STAGING" != 1 ]; then
      if [ -e "$ST" ]; then mv "$ST" "$ST.superseded-$TS"; fi
      ( umask 022 && git clone -q "$SRC" "$ST" && git -C "$ST" checkout -q "$HEAD_COMMIT" ) < /dev/null > /dev/null 2>&1 \
        || fail_step "克隆或检出 staging 树失败：$ST" "核对 $SRC 后 --from deploy"
    fi
    if [ "$(git -C "$ST" rev-parse HEAD)" != "$HEAD_COMMIT" ] || [ -n "$(git -C "$ST" status --porcelain)" ]; then
      fail_step "staging 树不是 $HEAD_COMMIT 的干净检出" "人工核对 $ST"
    fi
    ( umask 022 && install -d -m 755 "$ST/docs/repository-docs" \
      && cp -p "$ST/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md" "$ST/docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md" "$ST/docs/repository-docs/" \
      && chmod 644 "$ST"/docs/repository-docs/*.md ) || fail_step "复制仓库文档到 staging 失败" "核对 staging 树 docs/"
    { chown -R root:root "$ST" && chmod -R go-w "$ST"; } || fail_step "staging 属主／权限收口失败" "人工核对 $ST"
    decide deploy-launch --log "$LOG" --pid-file "$PIDF"
    setsid -f bash -c 'echo "$$" > "$2"; cd "$0" && python3 tools/arm64_supervised_deploy.py --staging-root "$0" --production-root "$3/tools/official_client_capture" --production-doc-root "$3/docs" --control-root "$3/control" > "$1" 2>&1; echo "exit=$?" >> "$1"' \
      "$ST" "$LOG" "$PIDF" "$D" < /dev/null > /dev/null 2>&1
    echo "  [deploy] 受监督部署已在后台启动：$LOG"
  fi
  if [ "$ACTION" != verify ]; then
    wait_marker "$LOG" "$DEPLOY_MAX_SECONDS" "$PIDF" \
      || fail_step "等待受监督部署结束失败（超时或进程已退出）：$LOG" "部署仍在跑时稍后 --from deploy 会接着等；否则查看 $LOG"
  fi
  decide deploy-verify --log "$LOG"
}

step_postdeploy() {
  decide postdeploy-receipt
  decide sync-deploy-script
  local unit="$OUT/raw/$TS-postdeploy-guard-unit.txt" vrc=0 irc=0 arc=0
  systemctl cat "$GUARD_SERVICE" > "$unit" 2> "$unit.err" < /dev/null || true
  decide guard-check --file "$unit"
  if [ -f "$DRIVER_TARGET/install.py" ]; then
    python3 "$DRIVER_TARGET/install.py" verify --target "$DRIVER_TARGET" --data-root "$D" \
      > "$OUT/raw/$TS-postdeploy-driver-verify-before.out" 2>&1 < /dev/null || vrc=$?
  else
    vrc=127
  fi
  decide driver-state --verify-rc "$vrc"
  if [ "$ACTION" = install ]; then
    python3 "$ST/tools/arm64_capture_driver/install.py" install --source "$ST/tools/arm64_capture_driver" --target "$DRIVER_TARGET" \
      --data-root "$D" > "$OUT/raw/$TS-postdeploy-driver-install.out" 2>&1 < /dev/null || irc=$?
    if [ "$irc" = 0 ]; then
      python3 "$DRIVER_TARGET/install.py" verify --target "$DRIVER_TARGET" --data-root "$D" \
        > "$OUT/raw/$TS-postdeploy-driver-verify.out" 2>&1 < /dev/null || arc=$?
    fi
    decide driver-after --install-rc "$irc" --verify-rc "$arc"
  fi
  decide entry-assertions
  decide postdeploy-final --self-sha "$SELF_SHA"
}

step_item_tests() {
  decide tests-state
  if skipped; then return 0; fi
  if [ "$ACTION" = run ]; then
    setsid -f python3 "$FCPY" run-tests --params "$PARAMS" --log "$LOG" --pid-file "$PIDF" < /dev/null > /dev/null 2>&1
    echo "  [item-tests] 实测已在后台启动：$LOG"
  fi
  wait_marker "$LOG" "$ITEM_TESTS_MAX_SECONDS" "$PIDF" \
    || fail_step "等待实测结束失败（超时或进程已退出）：$LOG" "查看 $LOG 后 --from item-tests"
  decide tests-verdict --log "$LOG"
}

step_evolution() {
  managed codex_upgrade evolution-status tool-evolution-status --campaign-dir "$C"
  decide evolution-status --raw "$RAW"
  if skipped; then return 0; fi
  managed codex_upgrade evolution-preview tool-evolution --campaign-dir "$C" --fix-commit "$FIX_COMMIT" --reason "$EVOLUTION_REASON"
  decide evolution-preview --raw "$RAW"
  managed codex_upgrade evolution-apply tool-evolution --campaign-dir "$C" --fix-commit "$FIX_COMMIT" --reason "$EVOLUTION_REASON" \
    --approve-sha256 "$REVIEW_SHA256" --approved-by "$APPROVER"
  decide evolution-apply --raw "$RAW"
  managed codex_upgrade evolution-status-after tool-evolution-status --campaign-dir "$C"
  decide evolution-status --raw "$RAW" --mode final
}

# extend_round pre|post：阶段延期（预览 → 按预览 review_sha256 与批准人写入两本账 → 复核有效截止）。
extend_round() {
  local mode="$1"
  decide extend-state --mode "$mode"
  if skipped; then return 0; fi
  managed codex_upgrade extend-preview deadline-extend preview --campaign-dir "$C" --scope stage --phase "$EXTEND_PHASE" \
    --new-deadline-at-utc "$EXTEND_DEADLINE" --reason "$EXTEND_REASON"
  decide extend-preview --raw "$RAW" --mode "$mode"
  managed codex_upgrade extend-apply deadline-extend apply --campaign-dir "$C" --preview "$EXT_PREVIEW" \
    --approve-sha256 "$EXT_SHA256" --approved-by "$APPROVER"
  decide extend-apply --raw "$RAW" --mode "$mode"
}
step_pre_extend() { extend_round pre; }
step_extend() { extend_round post; }

# root_cause_repair <标签> <模式> [<对象类别> <对象>]：项目总账根因修复登记（第 59 项抽出，repair 步骤与 reconcile-runs 共用）。
#   同一判定（repair-plan：待登记根因、同一修复提交已登记即跳过、回归收据由草稿补本轮实测日志与部署收据写一次并校验）、
#   同一命令（record-root-cause-repair，参数逐字相同）、同一核对（repair-verdict：命令结果、登记后总账里必须有修复事件）。
#   模式 step：repair 步骤，结果写 repair 步骤记录（第 35 项原行为）；
#   模式 inline：reconcile-runs 对账判"项目总账根因达上限"暂停时在步骤内登记，结果并入本步骤记录，停下的续跑步骤是本步骤；
#     同一修复提交已登记时判定输出 REPAIR_ACTION=already，不再执行登记命令。
root_cause_repair() {
  local label="$1" mode="$2" object=() args=() cause
  if [ "$#" -ge 4 ]; then object=(--kind "$3" --object "$4"); fi
  REPAIR_ACTION=""
  decide repair-plan --mode "$mode" ${object[@]+"${object[@]}"}
  if skipped || [ "$REPAIR_ACTION" = already ]; then return 0; fi
  args=(record-root-cause-repair --ledger-dir "$LEDGER_DIR")
  for cause in $TODO_RCS; do args+=(--root-cause-id "$cause"); done
  args+=(--kind code --binding "fix_commit_sha=$REPAIR_FIX_COMMIT" --binding "regression_receipt_sha256=$REG_SHA256"
         --binding "deployment_receipt_sha256=$DEPLOY_RECEIPT_SHA256" --note "$REPAIR_NOTE")
  managed codex_upgrade_project_ledger "$label" "${args[@]}"
  decide repair-verdict --raw "$RAW" --subject "$TODO_RCS" --mode "$mode" ${object[@]+"${object[@]}"}
}

# reconcile_object <标签> <对象类别> <对象> <受管对账命令与参数…>：reconcile-runs 里对账一个对象（父 run 或 attempt）。
#   判定 ACTION=repair（暂停只因项目总账根因达上限且参数给了登记材料，与 reconcile-attempt 旁路同一判据）时，按
#   root_cause_repair inline 在本步骤内登记，再对该对象重新对账一次（--mode after-repair：仍暂停即停下，不再登记）。
reconcile_object() {
  local label="$1" kind="$2" subject="$3"
  shift 3
  managed codex_upgrade "$label" "$@"
  decide run-verdict --raw "$RAW" --kind "$kind" --subject "$subject"
  if [ "$ACTION" != repair ]; then return 0; fi
  root_cause_repair "$label-repair" inline "$kind" "$subject"
  managed codex_upgrade "$label-after-repair" "$@"
  decide run-verdict --raw "$RAW" --kind "$kind" --subject "$subject" --mode after-repair
}

# reconcile_one_attempt <序号> <attempt> <恢复段或 ->：链尾父 run 窗口内非目标 attempt 的对账。
reconcile_one_attempt() {
  local n="$1" attempt="$2" revision="$3" subject extra=()
  if [ "$attempt" = "$ATTEMPT" ] && [ "$revision" = - ]; then
    echo "  [reconcile-runs] 目标 attempt $ATTEMPT 留给 reconcile-attempt 步骤"
    return 0
  fi
  subject="$attempt"
  if [ "$revision" != - ]; then
    extra=(--recovery-revision "$revision")
    subject="$attempt:$revision"
  fi
  reconcile_object "attempt-$n-$attempt" attempt "$subject" \
    reconcile-attempt --campaign-dir "$C" --attempt-id "$attempt" ${extra[@]+"${extra[@]}"}
}

step_reconcile_runs() {
  decide scan-runs
  if skipped; then return 0; fi
  local rows=() row kind run a b item n=0 redirected=() excludes=()
  while IFS= read -r row; do
    if [ -n "$row" ]; then rows+=("$row"); fi
  done < "$PENDING_FILE"
  for row in ${rows[@]+"${rows[@]}"}; do
    IFS=$'\t' read -r kind run a b <<< "$row"
    n=$((n + 1))
    if [ "$kind" = supervisor-run ]; then
      reconcile_object "run-$n" supervisor-run "$run" reconcile-supervisor-run --run-dir "$a" --campaign-dir "$C"
      if [ "$ACTION" = redirect ]; then
        echo "  [reconcile-runs] $run 期间已有预约，按对账器提示改走 reconcile-attempt：$REDIRECT"
        redirected+=("$run")
        for item in $REDIRECT; do
          reconcile_one_attempt "$n" "${item%%:*}" "${item#*:}"
        done
      fi
    else
      reconcile_one_attempt "$n" "$a" "$b"
    fi
  done
  for run in ${redirected[@]+"${redirected[@]}"}; do excludes+=(--exclude-run "$run"); done
  decide scan-runs --mode final ${excludes[@]+"${excludes[@]}"}
}

step_reconcile_attempt() {
  managed codex_upgrade reconcile reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT"
  decide attempt-verdict --raw "$RAW" --mode reconcile
}

step_repair() {
  root_cause_repair repair step
}

step_approve() {
  # 批准摘要只取同一次运行刚生成的恢复预览输出（预览幂等：内容不变时返回同一份），从不复用旧文件里的摘要。
  managed codex_upgrade approve-preview reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT"
  decide attempt-verdict --raw "$RAW" --mode approve-preview
  managed codex_upgrade approve reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT" --approve-recovery-sha256 "$REVIEW_SHA256"
  decide approve-verdict --raw "$RAW" --subject "$REVIEW_SHA256" --preview "$PREVIEW_PATH"
}

step_authorize() {
  decide need-preview
  managed codex_upgrade authorize reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT" --authorize-recovery-preview "$PREVIEW"
  decide authorize-verdict --raw "$RAW" --preview "$PREVIEW" --subject "$REVIEW_SHA256"
}

step_accepted() {
  decide need-preview
  decide accepted --preview "$PREVIEW"
}

step_recover() {
  local f
  decide need-preview
  decide recover-state --preview "$PREVIEW"
  if skipped; then return 0; fi
  for f in vc5-recover.out vc5-run-batch.out; do
    if [ -e "$RUNROOT/$f" ]; then mv "$RUNROOT/$f" "$RUNROOT/$f.pre-$ROUND-$TS"; fi
  done
  ARM64_VC_ENV="$VC_ENV" VC_STATE_DIR="$VC_STATE_DIR" setsid -f bash "$RECOVER_SCRIPT" "$PREVIEW" > "$RECOVER_LOG" 2>&1 < /dev/null
  decide recover-started --preview "$PREVIEW" --log "$RECOVER_LOG"
  echo "VC5_RECOVER_STARTED $RECOVER_LOG"
}

echo "==== 修好接着跑：轮次 ${ROUND}，Campaign ${CAMPAIGN}，attempt ${ATTEMPT}，从 $FROM 开始（${TS}）"
decide preflight --resume-from "$FROM"
STARTED=0
for s in $STEPS_LIST; do
  if [ "$STARTED" = 0 ]; then
    if [ "$s" != "$FROM" ]; then continue; fi
    STARTED=1
  fi
  STEP="$s"
  rm -f "$OUT/.partial-$s.json"
  echo "==== [$(date -u +%H:%M:%SZ)] 步骤 $s"
  "step_$(printf '%s' "$s" | tr '-' '_')"
done
STEP=done
echo "FIX_AND_CONTINUE_DONE round=$ROUND out=$OUT"
echo "下一步：$RUNROOT/vc5-run-batch.out 出现 RUN_BATCH_DONE 后执行 ARM64_VC_ENV=$VC_ENV VC_STATE_DIR=$VC_STATE_DIR setsid -f bash $DRIVER_TARGET/driver/vc5-all.sh > $RUNROOT/vc5-all.out 2>&1 < /dev/null（旧 vc5-all.out 先改名留档）"
