#!/bin/bash
# Codex 升级收尾（closeout）本机步骤总控：VC-6 PASS 之后，从“并入候选链”一路到“收尾全集门禁绿、候选备齐、等老板批准推 main”。
#
# 做什么：把收尾的 20 来个步骤固化成一条入口，版本、Campaign 名、文件名前缀、分支名、ARM64 路径全部从参数或总账现取，不再复制上一轮脚本改字面量。
# 输入：命令行选项（目标／基线／退休版本、Campaign 全名等）或总账 STATUS.md 里的 closeout.<名> 字段行；ARM64 步骤需要 ssh 别名可用。
# 输出：各步骤产物落在 --out-dir 下的 facts/ 与 logs/；提交落在收尾工作树（--wt）的收尾分支上；ARM64 写只落收尾目录。
# 何时用：VC-6 PASS 之后（b10-pre 可在 VC-6 之前提前做）；每一步成功后才做下一步，失败先停、读日志，不用重试试错。
#
# 用法：bash tools/ci/closeout/closeout.sh [选项] <步骤> [步骤参数]      （在任意目录运行；工作树取 --wt）
#       bash tools/ci/closeout/closeout.sh steps                         打印步骤表（必需参数、是否写 ARM64、是否属可选组）
#       bash tools/ci/closeout/closeout.sh --help                        本帮助 + 全部选项说明
# 选项（全部可省，缺的参数按“命令行 > 环境变量 CLOSEOUT_<大写名> > 总账字段 closeout.<名> > 派生默认值”取，详见 closeout_params.py --help）：
#   --version 目标版本   --baseline-version 基线版本   --retire-version 退休版本   --campaign Campaign 全名
#   --ledger 总账 STATUS.md（里面写 “closeout.campaign = …” 之类字段行即可，不用逐个传参）
#   --with-part2   启用可选组（b2-part2-lead、b3-regen、第二部分最小改写；现行流程已作废，缺省关闭）
#   --co-author 'Name <mail>'   提交说明末尾的 Co-Authored-By，缺省不加
# 约定：
#   * 带“【ARM64 写】”的步骤要设 CLOSEOUT_ARM64_WRITE=1 才会真的执行（不设就只打印将要做什么）；ARM64 只读步骤随时可跑（ssh -n）。
#   * 推 main／发版不在本脚本里：需要老板明确指令，另走。批准件（write_approvals.py）只在老板明确指令后才写。
#   * 每一步先核对前置、失败即停；本机重负载一律走重负载锁（prerun.sh 内部已做）。缺参数时报“缺哪个参数、怎么补”，不会带着猜测值继续。
#   * 超过 10 分钟的本机命令用 detach.py（Python 新会话写法）脱离会话，不用 nohup。
# 步骤总表（顺序即依赖顺序；每步的前置断言保留）：
#   b0-check            只读：VC-6 PASS、PC／RPC、Campaign 与候选目录实名、官方 attempt 唯一（没给 --campaign 时只做发现并提示）
#   b1-worktree         从 RPC（或候选别名分支头）建收尾分支与工作树；集成分支有新提交就预演后并入；EXTRA_BRANCHES 里的修复分支逐个预演并入
#   b2-part2-lead       【可选，--with-part2；已作废】指南第二部分开头段最小改写。仅当 evidence_index 回退到读第二部分时才用；做完必须接 b3-regen
#   b3b-vc6-receipt     VC-6 两个 Catalog 提交（PC／RPC）的冻结承接收据（release-catalog.json、release-graph.json 两条转移）；候选链已带就跳过
#   b3a-postpromo-fixes 晋升后断言修正（boundary 测试的 Active 版本），定向回归，提交
#   b3-check            只读：重封链干跑、归属表、驱动 manifest、晋升后形态定向回归
#   b3-regen            【可选，--with-part2 且 CLOSEOUT_REGEN=1；已作废】第二章摘要连锁＋冻结承接收据＋重封链（约 40 分钟）
#   b4-stage-receipts   ARM64 只读：取回四份阶段收据，逐字节核 sha256
#   b5-audit-index      【ARM64 写】在收尾目录生成审计索引，取回并核摘要
#   b6-commit-stage     提交阶段收据＋审计索引
#   b7-terminal-facts   ARM64 只读取证据小文件 → 终态事实 → 终态候选校验
#   b8a-active-receipt  生成带新 4.9 值表（Active 终态收据坐标）的指南副本 ${FACTS}/guide-active-receipt.md，并用 evidence_index 同一校验核对；不提交
#   b8-evidence-index   临时放终态候选与 4.9 副本重建 docs/EVIDENCE_INDEX.md 并 --check，提交，立刻还原临时文件
#   b9-render-guide     渲染最终指南（第一、三、四部分的 Active 绑定位置），输出 ${FACTS}/final-guide.md；步骤参数原样传给 render_guide.py，
#                       其中 --behavior-summary '<一句行为概述>|@文件' 必填；第四部分经验另走 finalize_part4.py
#   b10-pre             【ARM64 写】可在 VC-6 结束前做：建收尾目录、seed／mirror.git／repo 克隆（只需候选目录与 GitHub main 提示号）
#   b10-sync            【ARM64 写】bundle 上 ARM64、克隆 ff 到本机头，送最终指南、终态事实、配置生成器等
#   b10b-prepare        【ARM64 写】draft → 配置 → prepare --dry-run → prepare（产出收尾候选提交）；换工作根用 WORK_NAME=work-2
#   b12-fetch-candidate 取回候选提交到本机工作树 <工作树>-cand，核对提交号与 bundle 摘要
#   b13-prerun          CI 等价预跑（候选提交上，脱离会话；PRERUN_SKIP／PRERUN_ONLY 可调）
#   b14-gates-start     【ARM64 写】起收尾全集门禁（先 --preflight-only，再 setsid -f），核对 SigIgn
#   b15-gates-status    ARM64 只读：门禁状态快照
#   b16-prepush         候选工作树上跑推送前预检（只读；步骤参数传给 prepush_check.py，如 --fast）
# 其它辅助脚本（同目录）：gen_terminal_facts.py、render_guide.py、finalize_part4.py、update_active_receipt.py、make_closeout_config.py、
#   deployment_check.py、write_approvals.py、gate_status.py、apply_postpromo_fixes.py、post_promo_mods.sh、prerun.sh、detach.py；
#   可选组：part2_minimal.py、rebind_guide_ch2.py、official_counts.py、regen_after_part2.sh。
# 例（参数仅示意；<日期> 是 Campaign 名里的 YYYYMMDD）：
#   bash tools/ci/closeout/closeout.sh --version 0.170.0 --baseline-version 0.169.0 --retire-version 0.168.0 \
#        --campaign c01700-formal-vc1-r1-<日期>t000000z --ledger <总账 STATUS.md> b0-check
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
help() {
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  echo
  echo "全部参数的来源与派生规则、总账字段写法：python3 -B ${HERE}/closeout_params.py --help"
}
case "${1:-}" in
  ""|-h|--help|help) help; exit 0 ;;
  steps) exec python3 -B "${HERE}/closeout_params.py" steps ;;
esac

# ---- 参数解析：交给 closeout_params.py（缺必需参数时它写明缺哪个并退出 2）
PARAMS_OUT=$(python3 -B "${HERE}/closeout_params.py" emit "$@") || exit $?
eval "${PARAMS_OUT}"
export PYTHONDONTWRITEBYTECODE=1
: "${CAPTURE_TYPESCRIPT_MODULE:=${TYPESCRIPT_MODULE:-}}"; export CAPTURE_TYPESCRIPT_MODULE
: "${CODEX_SPEC_SOURCE_ROOT:=${SPEC_SOURCE_ROOT:-}}"; export CODEX_SPEC_SOURCE_ROOT
mkdir -p "${FACTS}" "${LOGS}"

say() { echo "[closeout] $*"; }
die() { echo "[closeout] 失败：$*" >&2; exit 1; }
arm64_write() { # 守卫：ARM64 写步骤
  [ "${CLOSEOUT_ARM64_WRITE:-0}" = "1" ] || { say "【ARM64 写】步骤，未设 CLOSEOUT_ARM64_WRITE=1，只打印不执行：$*"; return 1; }
  return 0
}
part2_enabled() { # 可选组守卫：没开 --with-part2 就跳过（退出码 0，提示原因）
  [ "${WITH_PART2:-}" = "1" ] && return 0
  say "$1 属于可选组（--with-part2），缺省关闭：evidence_index 已改读指南 4.9 小节，收尾不再改冻结的第二部分；跳过。"
  return 1
}
inwt() { [ -d "${WT}" ] || die "收尾工作树 ${WT} 不存在：先 b1-worktree"; cd "${WT}" || exit 2; }
clean() { [ -z "$(git status --porcelain)" ]; }
commit() { # commit "<说明>"
  clean && { say "没有改动，不提交"; return 0; }
  git add -A && git commit -q -m "$1${CO_AUTHOR:+

Co-Authored-By: ${CO_AUTHOR}}" || die "提交失败"
  say "已提交 $(git log --oneline -1)"
}
ssh_ro() { ssh -n -o BatchMode=yes -o ConnectTimeout=15 "${SSH_HOST}" "$@"; }   # 只读 ssh（调用方保证命令只读）
ssh_w() { ssh -n "${SSH_HOST}" "$@"; }                                          # 写 ssh（只在 arm64_write 通过之后使用）
sha256_of() { if command -v shasum > /dev/null 2>&1; then shasum -a 256 "$1" | cut -c1-64; else sha256sum "$1" | cut -c1-64; fi; }
need_campaign() { [ -n "${CAMPAIGN:-}" ] || die "缺少 campaign（--campaign）"; }
load_official() { # 取官方 attempt 与官方 Job 数（只读 ssh），设 OFFICIAL_ATTEMPT／OFFICIAL_JOBS；不唯一或取不到返回 1
  local dir="${DATA}/evidence/campaigns/${CAMPAIGN}/official/attempts"
  OFFICIAL_ATTEMPT=$(ssh_ro "ls ${dir}/") || return 1
  [ -n "${OFFICIAL_ATTEMPT}" ] && [ "$(printf '%s\n' "${OFFICIAL_ATTEMPT}" | wc -l | tr -d ' ')" = "1" ] || { echo "官方 attempt 不唯一或为空：[${OFFICIAL_ATTEMPT}]" >&2; return 1; }
  OFFICIAL_JOBS=$(ssh_ro "python3 -c \"import json;print(len(json.load(open('${dir}/${OFFICIAL_ATTEMPT}/attempt.json'))['results']))\"") || return 1
}
GUIDE=docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md
TERMINAL_FACTS="${FACTS}/terminal-facts-${TARGET_TAG}.json"
# 临时场景：放入终态收据候选、接上 local-analysis、（可选）把指南换成给定副本，跑命令，无论成败都还原（工作树必须干净）
with_temp_receipt() { # with_temp_receipt <指南副本，空串＝不换> <命令…>
  local guide_copy="$1" rc receipt="docs/egress/maintenance/${PREFIX}_TERMINAL_STATE_RECEIPT.json"; shift
  [ ! -e "${receipt}" ] || die "终态收据 ${receipt} 已在工作树里：本函数只在收据入库之前临时放入，跑完会删除它，不能覆盖已有文件"
  [ ! -e "${WT}/local-analysis" ] || die "${WT}/local-analysis 已存在：临时链接要自己建自己删，先查是谁放的"
  cp "${FACTS}/terminal-state-receipt.candidate.json" "${receipt}"
  ln -s "${MAIN}/local-analysis" "${WT}/local-analysis"
  [ -z "${guide_copy}" ] || cp "${guide_copy}" "${GUIDE}"
  "$@"; rc=$?
  rm -f "${WT}/local-analysis" "${receipt}"
  [ -z "${guide_copy}" ] || git checkout -q -- "${GUIDE}"
  return ${rc}
}

step="${CL_STEP}"
case "${step}" in
b0-check)
  say "VC 流水线状态末几行："
  ssh_ro "tail -n 6 ${PREP}/pipeline/vc-status.txt" || die "读不到流水线状态"
  PC=$(ssh_ro "cat ${RR}/vc6/vc6-pc.txt"); RPC=$(ssh_ro "cat ${RR}/vc6/vc6-retire-pc.txt")
  say "PC=${PC} RPC=${RPC}"
  git -C "${MAIN}" cat-file -e "${PC}^{commit}" && git -C "${MAIN}" cat-file -e "${RPC}^{commit}" || die "PC／RPC 还没交接到本机（vc6-handoff-watch 没跑完？）"
  git -C "${MAIN}" merge-base --is-ancestor "${PC}" "${RPC}" || die "PC 不是 RPC 的祖先"
  git -C "${MAIN}" merge-base --is-ancestor "${RPC}" "${CAND_BRANCH}" && say "RPC 在 ${CAND_BRANCH} 历史里（头 $(git -C "${MAIN}" rev-parse --short "${CAND_BRANCH}")）" || say "提示：RPC 不在 ${CAND_BRANCH} 历史里；b1 直接以 RPC 为起点建收尾分支，不依赖该分支名"
  C_DIRS=$(ssh_ro "ls ${DATA}/evidence/campaigns | grep '^${CTAG}-formal'")
  if [ -z "${CAMPAIGN:-}" ]; then
    [ "$(printf '%s\n' "${C_DIRS}" | wc -l | tr -d ' ')" = "1" ] && [ -n "${C_DIRS}" ] || die "数据根里 ${CTAG}-formal* 的 Campaign 目录不唯一：[${C_DIRS}]；请用 --campaign 指定"
    say "发现 Campaign=${C_DIRS}（没传 --campaign，只做发现）：后续步骤请用 --campaign ${C_DIRS} 或在总账写 closeout.campaign = ${C_DIRS}"
    exit 0
  fi
  [ "${C_DIRS}" = "${CAMPAIGN}" ] || die "数据根里的 Campaign 目录是 [${C_DIRS}]，不是参数给的 ${CAMPAIGN}：核对 --campaign"
  ssh_ro "test -d ${DATA}/candidates/${CANDIDATE} && echo 候选目录在" || die "候选目录 ${DATA}/candidates/${CANDIDATE} 不存在：核对 --candidate"
  ATT=$(ssh_ro "ls ${DATA}/evidence/campaigns/${CAMPAIGN}/official/attempts/"); [ "$(printf '%s\n' "${ATT}" | wc -l | tr -d ' ')" = "1" ] || die "官方 attempt 不唯一：${ATT}"
  say "官方 attempt=${ATT}"
  ssh_ro "ls ${RR}/vc6/ | head -20"
  ssh_ro "ls /root/private-archives | grep '${TARGET_VERSION}' | tail -2"
  ssh_ro "test -s ${DATA}/control/${INPUTS}/rule-migration-approved.json && echo 规则迁移清单在" || say "!! 规则迁移清单路径不对：${DATA}/control/${INPUTS:-<缺 inputs>}/rule-migration-approved.json"
  ;;

b1-worktree)
  RPC=$(ssh_ro "cat ${RR}/vc6/vc6-retire-pc.txt")
  START_REF="${RPC}"
  if git -C "${MAIN}" merge-base --is-ancestor "${RPC}" "${CAND_BRANCH}"; then START_REF="${CAND_BRANCH}"; fi   # 先例：DC→RPC 收据提交追加在 RPC 之后，收尾分支从该头建
  say "收尾分支起点：${START_REF}（RPC=${RPC:0:10}）"
  if [ ! -d "${WT}" ]; then
    git -C "${MAIN}" worktree add -b "${CBRANCH}" "${WT}" "${START_REF}" || die "建工作树失败"
  fi
  inwt; clean || die "工作树不干净"
  git merge-base --is-ancestor "${RPC}" HEAD || die "工作树头不是 RPC 的后代"
  # 集成分支前进了（指南改动等）就并入；先预演
  if ! git merge-base --is-ancestor "${INT_BRANCH}" HEAD; then
    say "集成分支 $(git rev-parse --short "${INT_BRANCH}") 不在收尾分支历史里，预演并入"
    git merge-tree --write-tree --name-only HEAD "${INT_BRANCH}" > "${LOGS}/merge-int-preview.txt" 2>&1 || { head -20 "${LOGS}/merge-int-preview.txt"; die "并入集成分支有冲突：生成值类取任一侧再重算；其余停下报告"; }
    git merge --no-ff "${INT_BRANCH}" -m "merge: 并入 ${INT_BRANCH}（${TARGET_VERSION} 登记集成分支在候选链之后的新提交）${CO_AUTHOR:+

Co-Authored-By: ${CO_AUTHOR}}" | tail -2
  else
    say "集成分支已在历史里（无新提交）"
  fi
  # 其它修复分支（有就空格分隔放进 EXTRA_BRANCHES）
  for b in ${EXTRA_BRANCHES:-}; do
    git merge-base --is-ancestor "${b}" HEAD && { say "${b} 已并入"; continue; }
    git merge-tree --write-tree --name-only HEAD "${b}" > "${LOGS}/merge-${b//\//_}-preview.txt" 2>&1 || { head -20 "${LOGS}/merge-${b//\//_}-preview.txt"; die "并入 ${b} 有冲突"; }
    git merge --no-ff "${b}" -m "merge: 并入 ${b}${CO_AUTHOR:+

Co-Authored-By: ${CO_AUTHOR}}" | tail -2
  done
  python3 -B -c "import json;print('Catalog source =', json.load(open('backend/internal/officialegress/catalogdata/runtime/release-catalog.json'))['source'])"
  say "头 $(git rev-parse HEAD)；与集成头的 tools 差异（期望只有测试）："; git diff --stat "${INT_BRANCH}" HEAD -- tools ':!tools/official_client_capture/tests' | tail -3
  ;;

b2-part2-lead)
  # 【可选组，已作废】步骤参数原样传给 part2_minimal.py（上一轮 Campaign、Job 数、模型名、规则总数等本轮特有事实，缺哪个它报哪个）
  part2_enabled b2-part2-lead || exit 0
  need_campaign
  inwt; clean || die "工作树不干净"
  git rev-parse HEAD > "${FACTS}/part2-base.txt"          # 改之前的头：b3-regen 的收据 before 与第二章旧摘要都取它
  load_official || die "取官方 attempt 失败"
  python3 -B "${HERE}/part2_minimal.py" --target-version "${TARGET_VERSION}" --baseline-version "${BASELINE_VERSION}" --retire-version "${RETIRE_VERSION}" \
    --campaign "${CAMPAIGN}" --attempt "${OFFICIAL_ATTEMPT}" --jobs "${OFFICIAL_JOBS}" ${CL_STEP_ARGS[@]+"${CL_STEP_ARGS[@]}"} || die "第二部分最小改写失败"
  commit "docs(codex): 指南第二部分开头段补写 ${TARGET_VERSION} 官方样本 Campaign 与当前 Active（收尾最小改写）

证据索引重建（tools/evidence_index.py）要求第二部分写明 Active 终态收据的 Campaign（回退场景）；只改开头段几句，不动规则正文。第二章摘要随之变化，
后续由 b3-regen 做摘要连锁、冻结承接收据与重封链。"
  say "下一步必须 b3-regen（--with-part2 且 CLOSEOUT_REGEN=1），再回到 b3-check"
  ;;

b3-check)
  inwt
  # 四类检查彼此独立：一项没通过，其余照跑（让操作者一次看全），最后统一以非零退出。
  # 以前每一项的失败都被吞掉：重封链干跑只 grep“无变化”类字样（前置检查没通过、整条没跑成时一行都不打印，看上去像没事），
  # manifest 校验经管道 tail 丢了退出码，整步永远退出 0。11-01 对着真实环境冒烟时（工作树是游离 HEAD）发现。
  B3_BAD=0
  say "== 重封链干跑（只读，看每一步是否‘无变化’）"
  python3 -B tools/ci/reseal_chain.py --repo . --log-dir "${LOGS}/reseal-dry" --version "${TARGET_VERSION}" --label closeout --reason "收尾判定" --ack-no-campaign --dry-run > "${LOGS}/reseal-dry.txt" 2>&1; RESEAL_RC=$?
  grep -E '无变化|无需|不需要|将执行|仍然成立' "${LOGS}/reseal-dry.txt" | cut -c1-160
  if [ "${RESEAL_RC}" != "0" ]; then
    say "!! 重封链干跑没跑成（退出码 ${RESEAL_RC}）：上面没有“无变化”不等于无变化。日志 ${LOGS}/reseal-dry.txt 末尾："
    tail -n 12 "${LOGS}/reseal-dry.txt" | cut -c1-200
    B3_BAD=1
  fi
  say "== 归属表／驱动 manifest"
  python3 -B tools/ci/unit_ownership.py check || { say "!! 归属表检查没通过"; B3_BAD=1; }
  python3 -B tools/arm64_capture_driver/install.py build-manifest --root tools/arm64_capture_driver --check > "${LOGS}/manifest-check.txt" 2>&1; MANIFEST_RC=$?
  tail -n 1 "${LOGS}/manifest-check.txt"
  [ "${MANIFEST_RC}" = "0" ] || { say "!! 驱动 manifest 校验没通过（退出码 ${MANIFEST_RC}）：先 build-manifest 重建并查为什么不一致"; B3_BAD=1; }
  say "== 晋升后形态定向回归（顺序跑，约 6～10 分钟，结果见 ${LOGS}/post-promo/results.txt）"
  bash "${HERE}/post_promo_mods.sh" "${WT}" "${LOGS}/post-promo" || { say "!! post_promo_mods.sh 异常退出（环境变量没设？）"; B3_BAD=1; }
  cut -c1-200 "${LOGS}/post-promo/results.txt" || B3_BAD=1
  if grep -Eq ' rc=[1-9]' "${LOGS}/post-promo/results.txt" || ! grep -q '^ALLDONE$' "${LOGS}/post-promo/results.txt"; then
    say "!! 晋升后形态回归有红，或没有跑到 ALLDONE。红的项："
    grep -E ' rc=[1-9]' "${LOGS}/post-promo/results.txt" | cut -c1-200
    say "提示：固定顺序是 b3a 在 b3-check 之前；test_claude_upgrade_boundary_codex 红说明 b3a 还没做（Active 版本断言），其余红一律先查再往下走"
    B3_BAD=1
  fi
  [ "${B3_BAD}" = "0" ] || die "b3-check 有没通过的项（上面带 !! 的行）"
  say "b3-check 全部通过"
  ;;

b3-regen)
  # 【可选组，已作废】第二部分被改过才需要；依赖登记集成工具目录 --i1-dir（未入库）
  part2_enabled b3-regen || exit 0
  [ "${CLOSEOUT_REGEN:-0}" = "1" ] || { say "未设 CLOSEOUT_REGEN=1，只打印：先 b3-check；第二部分改过才重算（生成值提交、冻结承接收据、重封链各自单独提交）"; exit 0; }
  inwt; clean || die "工作树不干净：先提交前面的改动"
  BASE_INT=$(cat "${FACTS}/part2-base.txt" 2>/dev/null || git rev-parse "${INT_BRANCH}")   # b2-part2-lead 改之前的头；没做 b2 就取集成头
  say "== 重算生成值＋冻结承接收据（regen_after_part2.sh all；旧第二章摘要与收据 before 取 ${BASE_INT:0:10}）"
  I1_REPO="${WT}" I1_LABEL="收尾分支" I1_BASE_OVERRIDE="${BASE_INT}" I1_RECEIPT_BEFORE="${I1_RECEIPT_BEFORE:-${BASE_INT}}" I1_RECEIPT_TAG_PREFIX="${CLOSEOUT_TAG}-part2" \
    TARGET_VERSION="${TARGET_VERSION}" TARGET_TAG="${TARGET_TAG}" CO_AUTHOR="${CO_AUTHOR:-}" bash "${HERE}/regen_after_part2.sh" all || die "regen_after_part2.sh all 失败"
  say "== 重封链（后台，约 30 分钟，含追踪）：日志 ${LOGS}/chain"
  python3 -B "${HERE}/detach.py" "${LOGS}/chain.log" "cd ${WT} && python3 -B tools/ci/reseal_chain.py --repo . --log-dir ${LOGS}/chain --version ${TARGET_VERSION} --label closeout-part2 --reason '收尾第二部分最小改写后的重封（第二章摘要变化连锁）' ${CO_AUTHOR:+--co-author '${CO_AUTHOR}'} --ack-no-campaign"
  say "已脱离会话起跑；看 ${LOGS}/chain.log 末行 EXIT="
  ;;

b3a-postpromo-fixes)
  inwt; clean || die "工作树不干净"
  python3 -B "${HERE}/apply_postpromo_fixes.py" --target "${TARGET_VERSION}" --prev "${BASELINE_VERSION}" --retired "${RETIRE_VERSION}" || die "断言修正失败"
  python3 -B -m unittest tools.official_client_capture.tests.test_claude_upgrade_boundary_codex 2>&1 | tail -3
  commit "test(boundary): 晋升后 Runtime Catalog 的 Active 断言随 ${TARGET_VERSION} 终态更新

VC-6 晋升后 Active 为 ${TARGET_VERSION}（Previous ${BASELINE_VERSION}）；test_real_codex_runtime_catalog_resolves_active 仍断言上一版为 Active。
与历次收尾同类（预跑暴露、同一文件）；该文件不在冻结台账内，不需要承接收据。"
  ;;

b3b-vc6-receipt)
  inwt
  FZ=tools.official_client_capture.tests.test_ci_background_preflight.FreezeReceiptsTests.test_the_real_repository_head_has_no_uncovered_frozen_paths
  if python3 -B -m unittest "${FZ}" > "${LOGS}/freeze-cover.log" 2>&1; then say "冻结覆盖已满足（候选链或集成分支已带 DC→RPC 收据），跳过"; exit 0; fi
  clean || die "工作树不干净"
  PC=$(ssh_ro "cat ${RR}/vc6/vc6-pc.txt"); RPC=$(ssh_ro "cat ${RR}/vc6/vc6-retire-pc.txt"); DC=$(git rev-parse "${PC}^")
  say "DC=${DC:0:10} PC=${PC:0:10} RPC=${RPC:0:10}"
  TAG="${VC6_RECEIPT_TAG}"
  REASON="VC-6 晋升与 ${RETIRE_VERSION} 退休两个 Catalog 提交的 Runtime Catalog 指针与 release graph 契约文件承接；新增的内容寻址 release graph 与裁剪后的快照目录不在冻结台账内"
  python3 -B -m tools.upstream_merge freeze-successor-generate --before "${DC}" --after "${RPC}" --tag "${TAG}" --reason "${REASON}" --dry-run 2>&1 | tail -1 > "${LOGS}/vc6-receipt-dry.json"
  python3 -B - "${LOGS}/vc6-receipt-dry.json" <<'PYIN' || die "干跑不符合预期（要 passed_local_evidence_successor、转移 2 条、人工动作空）"
import json, sys
d = json.loads(open(sys.argv[1]).read())
ok = d["result"] == "passed_local_evidence_successor" and not d["required_manual_actions"] and len(d["transitions"]) == 2
print("干跑:", d["result"], "转移", len(d["transitions"]), "人工动作", len(d["required_manual_actions"]), "台账外未登记", d["unregistered_path_count"])
sys.exit(0 if ok else 1)
PYIN
  OUT="docs/egress/maintenance/upstream-${TAG}-freeze-successor.json"
  python3 -B -m tools.upstream_merge freeze-successor-generate --before "${DC}" --after "${RPC}" --tag "${TAG}" --reason "${REASON}" --output "${WT}/${OUT}" 2>&1 | tail -1 | cut -c1-200
  test -s "${OUT}" || die "收据没生成"
  commit "chore(egress): 登记 ${TARGET_VERSION} VC-6 晋升与退休（${RETIRE_VERSION}）两个 Catalog 提交的冻结承接收据

before＝候选链头 ${DC:0:9}，after＝退休提交 ${RPC:0:9}，承接 release-catalog.json 与 releasecontract/testdata/release-graph.json 两条转移。
freeze-successor-generate：result=passed_local_evidence_successor，required_manual_actions 为空；新增的内容寻址 release graph 与裁剪后的快照目录不在冻结台账内。"
  python3 -B -m unittest "${FZ}" 2>&1 | tail -2
  ;;

b4-stage-receipts)
  inwt
  ARM64_HOST="${SSH_HOST}" bash "${HERE}/collect_stage_receipts.sh" "${WT}" "${RR}" "${PREFIX}" || die "取回阶段收据失败"
  ;;

b5-audit-index)
  inwt; need_campaign
  arm64_write "ARM64 建 ${CLOSE}、scp 工具、生成审计索引" || exit 0
  ssh_w "install -d -m 700 ${CLOSE} ${CLOSE}/audit"
  scp "${WT}/tools/codex_audit_index.py" "${HERE}/audit_index.sh" "${SSH_HOST}:${CLOSE}/"
  ssh_w "bash ${CLOSE}/audit_index.sh --tools ${CLOSE} --outdir ${CLOSE}/audit --campaign ${CAMPAIGN} --target-version ${TARGET_VERSION} --baseline-version ${BASELINE_VERSION} --prefix ${PREFIX} --runround ${RR} --data ${DATA} --evidence-host ${SSH_HOST}" || die "审计索引生成失败"
  scp "${SSH_HOST}:${CLOSE}/audit/${PREFIX}_AUDIT_INDEX.json" "${WT}/docs/egress/maintenance/"
  test "$(sha256_of "${WT}/docs/egress/maintenance/${PREFIX}_AUDIT_INDEX.json")" = "$(ssh_w "sha256sum ${CLOSE}/audit/${PREFIX}_AUDIT_INDEX.json | cut -c1-64")" && say AUDIT_SHA_OK || die "审计索引摘要不一致"
  python3 -B "${WT}/tools/codex_audit_index.py" check --index "${WT}/docs/egress/maintenance/${PREFIX}_AUDIT_INDEX.json" || die "本机自摘要复核失败"
  ;;

b6-commit-stage)
  inwt
  ls docs/egress/maintenance/${PREFIX}_*.json > /dev/null || die "没有阶段收据"
  git add "docs/egress/maintenance/${PREFIX}_"*.json
  git commit -q -m "docs(codex): ${BASELINE_VERSION}→${TARGET_VERSION} 审计索引与四份阶段收据入库（Framework §5.7）

四份阶段收据是 ARM64 证据根的逐字节副本：Catalog 晋升、post-promotion 门禁、四阶段生产激活、${RETIRE_VERSION} 运行画像退休 RemovalReceipt。审计索引由受管工具
tools/codex_audit_index.py generate 以 --no-replay 在 ARM64 生成，check --evidence-root 逐文件复核无漂移。终态收据由收尾工具 codex_closeout.py prepare 按终态事实生成，不在本提交。${CO_AUTHOR:+

Co-Authored-By: ${CO_AUTHOR}}" && say "已提交 $(git log --oneline -1)"
  ;;

b7-terminal-facts)
  inwt; need_campaign
  ARM64_HOST="${SSH_HOST}" DATA_ROOT="${DATA}" bash "${HERE}/fetch_evidence_subset.sh" "${FACTS}/evidence-subset" "${CAMPAIGN}" "${CANDIDATE}" "${RR}" || die "取证据子集失败"
  python3 -B "${HERE}/gen_terminal_facts.py" --repo "${WT}" --evidence-root "${FACTS}/evidence-subset/data" --evidence-root-label "${DATA}" \
    --campaign "${CAMPAIGN}" --candidate "${CANDIDATE}" --baseline-version "${BASELINE_VERSION}" --target-version "${TARGET_VERSION}" --retire-version "${RETIRE_VERSION}" \
    --run-vc6 "${FACTS}/evidence-subset/run/vc6" --output "${TERMINAL_FACTS}" || die "终态事实生成失败"
  python3 -B - <<PY || die "终态候选校验失败"
import json, sys
from pathlib import Path
sys.path.insert(0, ".")
from tools import codex_closeout as cc
facts = json.load(open("${TERMINAL_FACTS}"))
rel = "docs/egress/maintenance/${PREFIX}_TERMINAL_STATE_RECEIPT.json"
text = cc.terminal_candidate(Path(".").resolve(), facts, rel)
Path("${FACTS}/terminal-state-receipt.candidate.json").write_text(text)
print("终态候选通过校验器，字节数", len(text))
PY
  say "通读 ${TERMINAL_FACTS} 的 *_note 与 completed_at_utc"
  ;;

b8a-active-receipt)
  # 指南 4.9「当前 Active 终态收据坐标」值表换成新 Active 的值（evidence_index 的校验位置）。不提交：副本供 b8 临时借用、b9 作渲染输入，
  # 最终随收尾 prepare 的候选提交（指南是冻结路径，由 prepare 自带的冻结承接收据覆盖）一起入库。
  inwt; need_campaign; clean || die "工作树不干净"
  [ -s "${TERMINAL_FACTS}" ] && [ -s "${FACTS}/terminal-state-receipt.candidate.json" ] || die "先 b7"
  load_official || die "取官方 attempt 失败"
  rm -f "${FACTS}/guide-active-receipt.md"
  python3 -B "${HERE}/update_active_receipt.py" --guide "${GUIDE}" --facts "${TERMINAL_FACTS}" --official-attempt "${OFFICIAL_ATTEMPT}" \
    --output "${FACTS}/guide-active-receipt.md" || die "4.9 小节更新失败（指南里没有约定格式的 4.9 小节？）"
  say "== 用 evidence_index 同一校验核对（临时放入终态候选，跑完还原）"
  with_temp_receipt "${FACTS}/guide-active-receipt.md" python3 -B "${HERE}/update_active_receipt.py" --guide "${GUIDE}" --facts "${TERMINAL_FACTS}" \
    --official-attempt "${OFFICIAL_ATTEMPT}" --output "${FACTS}/.verify-4.9.md" --verify-repo "${WT}"; rc=$?
  rm -f "${FACTS}/.verify-4.9.md"
  [ ${rc} = 0 ] || die "evidence_index 校验未通过（退出码 ${rc}；3＝evidence_index 还是旧版，校验位置不在 4.9，需要 --with-part2 回退做法）"
  say "已写 ${FACTS}/guide-active-receipt.md；b8 与 b9 会用它"
  ;;

b8-evidence-index)
  inwt; clean || die "工作树不干净"
  GUIDE_COPY=""
  if [ -s "${FACTS}/guide-active-receipt.md" ]; then GUIDE_COPY="${FACTS}/guide-active-receipt.md"
  elif [ "${WITH_PART2:-}" != "1" ]; then die "先 b8a-active-receipt（证据索引要读指南 4.9 小节）"; fi
  with_temp_receipt "${GUIDE_COPY}" bash -c 'python3 -B tools/evidence_index.py && python3 -B tools/evidence_index.py --check'; rc=$?
  [ ${rc} = 0 ] || die "证据索引重建失败"
  git status --short
  git add docs/EVIDENCE_INDEX.md && git commit -q -m "docs(codex): 证据索引按 Active ${TARGET_VERSION} 终态重建

证据来源切到 ${TARGET_VERSION} 终态收据与私有归档（终态收据由收尾工具按同一终态事实生成，字节相同；本提交重建时临时放入、之后删除）。${CO_AUTHOR:+

Co-Authored-By: ${CO_AUTHOR}}" && say "已提交 $(git log --oneline -1)"
  ;;

b9-render-guide)
  inwt; need_campaign
  [ -s "${TERMINAL_FACTS}" ] || die "先做 b7"
  GUIDE_IN="${FACTS}/guide-active-receipt.md"
  if [ ! -s "${GUIDE_IN}" ]; then
    [ "${WITH_PART2:-}" = "1" ] || die "先 b8a-active-receipt（渲染输入要带新的 4.9 值表）"
    GUIDE_IN="${GUIDE}"
  fi
  load_official || die "取官方 attempt 失败"
  ssh_ro "cat ${DATA}/evidence/campaigns/${CAMPAIGN}/campaign.json" > "${FACTS}/campaign.json"
  ssh_ro "cat ${DATA}/control/${INPUTS}/rule-migration-approved.json" > "${FACTS}/rule-migration-approved.json"
  rm -f "${FACTS}/final-guide.md"
  P2MODE=untouched; [ "${WITH_PART2:-}" = "1" ] && P2MODE=lead-only
  python3 -B "${HERE}/render_guide.py" --repo "${WT}" --guide "${GUIDE_IN}" --facts "${TERMINAL_FACTS}" \
    --campaign-json "${FACTS}/campaign.json" --official-attempt "${OFFICIAL_ATTEMPT}" --official-jobs "${OFFICIAL_JOBS}" --rule-migration "${FACTS}/rule-migration-approved.json" \
    --part2-mode "${P2MODE}" ${CL_STEP_ARGS[@]+"${CL_STEP_ARGS[@]}"} --output "${FACTS}/final-guide.md" || die "渲染失败（--behavior-summary 必填；见 render_guide.py --help）"
  say "通读：diff -u ${GUIDE} ${FACTS}/final-guide.md；"
  say "再用 finalize_part4.py 并入第四部分‘本轮经验’与时间线（调度者写句子；不得动第二部分，脚本自检）"
  ;;

b10-pre)
  # 不要求工作树干净、不要求最终指南：只准备 ARM64 侧的目录与镜像，可以在 VC-5 等待期或 VC-6 期间提前做。
  need_campaign
  TIP=$(git -C "${MAIN}" ls-remote origin refs/heads/main | cut -f1); [ -n "${TIP}" ] || die "读不到 GitHub main"
  say "GitHub main=${TIP}（只读 ls-remote）"; echo "${TIP}" > "${FACTS}/github-main-tip.txt"
  arm64_write "建 ${CLOSE}、seed（从 ${DATA}/candidates/${CANDIDATE}/source 克隆）、mirror.git（推 main=${TIP}）、repo" || exit 0
  ssh_w "set -Eeuo pipefail; install -d -m 700 ${CLOSE}; cd ${CLOSE}
    [ ! -e seed ] && git clone -q --no-hardlinks ${DATA}/candidates/${CANDIDATE}/source seed
    [ ! -e mirror.git ] && git init -q --bare mirror.git && git -C seed push -q ${CLOSE}/mirror.git ${TIP}:refs/heads/main
    [ ! -e repo ] && git clone -q mirror.git repo
    git -C repo rev-parse origin/main"   # 镜像 HEAD 指向不存在的 master，克隆后本地分支未出生；b10-sync 的 bundle ff 才会出生
  say "预期：repo 的 origin/main = GitHub main ${TIP:0:12}；seed 推 main 若报对象缺失，说明候选 source 克隆里没有该提交——改从本机 bundle 先送 origin/main 全量"
  ;;

b10-sync)
  inwt; need_campaign; clean || die "工作树不干净"
  git fetch -q origin main && TIP=$(git ls-remote origin refs/heads/main | cut -f1) && [ "${TIP}" = "$(git rev-parse origin/main)" ] || die "GitHub main 与本地 origin/main 不一致（main 前进了：先并入再重做 b8 之后各步）"
  say "GitHub main=${TIP}"
  echo "${TIP}" > "${FACTS}/github-main-tip.txt"
  BUNDLE="${FACTS}/closeout-${TARGET_TAG}.bundle"
  arm64_write "bundle ${BUNDLE} 上 ARM64 ${CLOSE}，建 seed／mirror.git／repo" || exit 0
  GUIDE_SRC="${FACTS}/final-guide-p4.md"; [ -s "${GUIDE_SRC}" ] || GUIDE_SRC="${FACTS}/final-guide.md"     # 带第四部分经验与时间线的最终指南优先
  [ -s "${GUIDE_SRC}" ] || die "先 b9（并跑完 finalize_part4.py）"
  say "上传的最终指南：${GUIDE_SRC}"
  rm -f "${BUNDLE}"
  git bundle create "${BUNDLE}" origin/main..HEAD && git bundle verify "${BUNDLE}" | tail -1
  ssh_w "install -d -m 700 ${CLOSE}"
  scp "${GUIDE_SRC}" "${SSH_HOST}:${CLOSE}/final-guide.md"
  scp "${BUNDLE}" "${TERMINAL_FACTS}" "${HERE}/make_closeout_config.py" "${HERE}/deployment_check.py" "${HERE}/write_approvals.py" "${HERE}/gate_status.py" "${SSH_HOST}:${CLOSE}/"
  AUTHOR_NAME=$(git config user.name); AUTHOR_EMAIL=$(git config user.email)
  ssh_w "set -Eeuo pipefail; cd ${CLOSE}; chmod 600 final-guide.md terminal-facts-${TARGET_TAG}.json; chmod 700 deployment_check.py write_approvals.py gate_status.py
    [ ! -e seed ] && git clone -q --no-hardlinks ${DATA}/candidates/${CANDIDATE}/source seed
    [ ! -e mirror.git ] && git init -q --bare mirror.git && git -C seed push -q ${CLOSE}/mirror.git ${TIP}:refs/heads/main
    [ ! -e repo ] && git clone -q mirror.git repo
    git -C repo config user.name '${AUTHOR_NAME}'; git -C repo config user.email '${AUTHOR_EMAIL}'
    git -C repo fetch -q ${CLOSE}/closeout-${TARGET_TAG}.bundle HEAD:refs/heads/merged && git -C repo merge -q --ff-only merged
    git -C repo log --oneline -1; git -C repo status --short | wc -l"
  test "$(ssh_w "git -C ${CLOSE}/repo rev-parse HEAD")" = "$(git rev-parse HEAD)" && say REPO_HEAD_OK || die "ARM64 克隆头与本机不一致"
  ;;

b10b-prepare)
  need_campaign
  TIP=$(cat "${FACTS}/github-main-tip.txt" 2>/dev/null) || die "先 b10-sync"
  arm64_write "draft → make_closeout_config → prepare --dry-run → prepare（${CLOSE}）" || exit 0
  WORK=${WORK_NAME:-work-1}
  ssh_w "cd ${CLOSE}/repo && python3 -B tools/codex_closeout.py draft --campaign ${DATA}/evidence/campaigns/${CAMPAIGN} --candidate ${CANDIDATE} \
    --guide ${CLOSE}/repo/${GUIDE} --baseline-profile ${CLOSE}/repo/tools/official_client_capture/candidate_rule_expectations_${BASELINE_VERSION//./_}.json --output ${CLOSE}/draft" | tail -c 700
  say "期望 draft：status=review_required；affected_rule_ids 里列出的是‘期望判据相对基线不同’的规则，不一定是迁移分类变化（draft 只生成审核草稿，prepare 不依赖它）"
  ssh_w "cd ${CLOSE} && python3 -B make_closeout_config.py --closeout-dir ${CLOSE} --target-version ${TARGET_VERSION} --baseline-version ${BASELINE_VERSION} \
    --runround ${RR} --campaign ${CAMPAIGN} --candidate ${CANDIDATE} --fact-name ${FACT_NAME} --data ${DATA} --gate-driver ${GATE_DRIVER} \
    --node-modules ${ARM64_NODE_MODULES} --tag ${CLOSEOUT_TAG} --expected-tip ${TIP} --work-name ${WORK} --output ${CLOSE}/closeout-config.json"
  ssh_w "cd ${CLOSE}/repo && python3 -B tools/codex_closeout.py prepare --config ${CLOSE}/closeout-config.json --dry-run" | tail -c 900
  say "期望 dry-run：status=dry_run，changed_paths 只有指南与终态收据两项（第二部分不动，第二章摘要连锁应为空），manual_gates 三项；不符就停"
  ssh_w "cd ${CLOSE}/repo && python3 -B tools/codex_closeout.py prepare --config ${CLOSE}/closeout-config.json > ${CLOSE}/prepare.out 2> ${CLOSE}/prepare.err; echo rc=\$?; tail -c 600 ${CLOSE}/prepare.err"
  ssh_w "python3 -B -c \"import json;d=json.load(open('${CLOSE}/${WORK}/plan.json'));print('candidate_commit', d['candidate_commit'])\""
  ;;

b12-fetch-candidate)
  inwt
  WORK=${WORK_NAME:-work-1}
  CAND_SHA=$(ssh_ro "python3 -B -c \"import json;print(json.load(open('${CLOSE}/${WORK}/plan.json'))['candidate_commit'])\"")
  [[ "${CAND_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "取不到候选提交号"
  CAND_BUNDLE="${FACTS}/candidate-${TARGET_TAG}.bundle"
  scp "${SSH_HOST}:${CLOSE}/${WORK}/candidate.bundle" "${CAND_BUNDLE}" || die "取候选包失败"
  test "$(sha256_of "${CAND_BUNDLE}")" = "$(ssh_ro "sha256sum ${CLOSE}/${WORK}/candidate.bundle | cut -c1-64")" || die "候选包摘要不一致"
  git fetch -q "${CAND_BUNDLE}" "+refs/heads/*:refs/closeout-cand-tmp/*" 2>&1 | tail -2   # 先取到非分支命名空间，免得留下多余分支
  git branch -f "${CBRANCH}-cand" "${CAND_SHA}" || die "候选提交没取回来（bundle 里没有该提交？）"
  git for-each-ref --format='%(refname)' refs/closeout-cand-tmp | while read -r r; do git update-ref -d "${r}"; done
  [ -d "${WT}-cand" ] || git worktree add "${WT}-cand" "${CBRANCH}-cand"
  test "$(git -C "${WT}-cand" rev-parse HEAD)" = "${CAND_SHA}" && say "候选提交 ${CAND_SHA} 已在 ${WT}-cand" || die "候选工作树头不符"
  ;;

b13-prerun)
  TREE="${CL_STEP_ARGS[0]:-${WT}-cand}"
  [ -d "${TREE}" ] || die "候选工作树 ${TREE} 不存在：先 b12"
  rm -rf "${LOGS}/prerun"
  python3 -B "${HERE}/detach.py" "${LOGS}/prerun.log" "PRERUN_REPO=${TREE} PRERUN_MAIN=${MAIN} PRERUN_SKIP='${PRERUN_SKIP:-}' PRERUN_BASE='${PRERUN_BASE:-}' bash ${HERE}/prerun.sh ${LOGS}/prerun"
  say "已脱离会话；看 ${LOGS}/prerun/SUMMARY.txt 与末行 ALLDONE（约 45～60 分钟，含 Go 全包与 lint：后端有 Catalog 数据与测试改动，不能省）"
  ;;

b14-gates-start)
  WORK=${WORK_NAME:-work-1}
  arm64_write "preflight-only → setsid -f codex_closeout.py gates --plan ${CLOSE}/${WORK}/plan.json" || exit 0
  ssh_w 'pgrep -af "vc-pipeline|background-validate|entry-gates|entry.sh|capture-cli|unit_executor" | grep -v pgrep | head; df -h / | tail -1; docker ps --format "{{.Names}}" | head -20'
  say "上面应无重负载进程；根盘 ≤69%、可用 ≥40G。有进程或盘不够就停（housekeeping 先清）"
  ssh_w "cd ${CLOSE}/repo && python3 -B tools/codex_closeout.py gates --plan ${CLOSE}/${WORK}/plan.json --preflight-only" | tail -c 500 || die "预检未过"
  ssh_w "setsid -f bash -c 'cd ${CLOSE}/repo && python3 -B tools/codex_closeout.py gates --plan ${CLOSE}/${WORK}/plan.json > ${CLOSE}/gates-run.out 2> ${CLOSE}/gates-run.err; echo exit=\$? >> ${CLOSE}/gates-run.out' < /dev/null > /dev/null 2>&1"
  ssh_w "sleep 3; pid=\$(pgrep -f '[c]odex_closeout.py gates' | head -1); echo pid=\${pid}; grep -E '^Sig(Ign|Blk)' /proc/\${pid}/status"
  say "SigIgn 十六进制值最后一位必须是偶数（最低位＝SIGHUP 不被忽略）；是奇数就是 nohup 继承，立刻停掉重起"
  ;;

b15-gates-status)
  WORK=${WORK_NAME:-work-1}
  ssh_ro "python3 -B ${CLOSE}/gate_status.py ${CLOSE} ${CLOSE}/${WORK}"
  ;;

b16-prepush)
  TREE="${CL_STEP_ARGS[0]:-}"; [ -n "${TREE}" ] || TREE="${WT}-cand"      # 第一个参数空＝默认候选树
  [ "${#CL_STEP_ARGS[@]}" -gt 0 ] && CL_STEP_ARGS=("${CL_STEP_ARGS[@]:1}")
  cd "${TREE}" || die "候选工作树不存在"
  python3 -B tools/ci/prepush_check.py ${CL_STEP_ARGS[@]+"${CL_STEP_ARGS[@]}"}     # 可加 --fast（不联网）；本机收据由 b13 的 capture 步骤生成
  ;;

*) die "未知步骤 ${step}（closeout.sh steps 查看步骤表）" ;;
esac
