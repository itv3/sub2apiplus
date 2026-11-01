#!/bin/bash
# 【可选组 --with-part2，现行流程已作废】收尾 b3-regen：第二部分被改过之后的“重算生成值 → 冻结承接收据”（重封链另走 tools/ci/reseal_chain.py）。
#
# 状态：现行流程里收尾不再改指南第二部分（evidence_index 已改读指南 4.9 小节），所以不需要本脚本；它只在 ``closeout.sh --with-part2`` 时由 b3-regen 调用，逻辑保留。
# 做什么：由登记集成阶段的 I1 脚本（i1.sh）改成的收尾版；本脚本自己不含那批 Python 工具，依赖“登记集成工具目录”（环境变量 I1_DIR，未入库），
#   其中必须有：env.sh、refreeze-trace-profile.py、guide-mentions.py、regen_closeout.py、gen_parity_batch.py、regen-claude-2-1-226.py、
#   ledger-diff.py、freeze-receipts.py、run-mods.sh、mods-digest.txt。第二章摘要连锁用本目录自己的 rebind_guide_ch2.py。
#     gen       “重算生成值”提交内容：驱动清单、候选追踪画像冻结摘要重冻、第二章摘要连锁（场景／期望清单＋检查器＋指南正文两处引用）、对照批次登记、
#               依赖声明与断言画像、归属表、版本泄漏与出站定型面台账核对
#     receipts  冻结承接收据：干跑→生成（要求工作树干净，after＝HEAD；after 之前的源码改动都要已提交）
#     check     快速核对：摘要敏感 Python 模块＋台账完整性＋驱动清单，打印仍红的项
#     all       依次 gen、receipts（均带提交）；之后接 reseal_chain.py
# 输入：[I1_REPO=<工作树>] [I1_BASE_OVERRIDE=<第二部分改写之前的提交>] [I1_RECEIPT_BEFORE=<收据 before>] [I1_LABEL=<分支称呼>]
#       TARGET_VERSION=<目标版本> TARGET_TAG=<目标标记> I1_DIR=<登记集成工具目录> [CO_AUTHOR='Name <mail>']
#       bash regen_after_part2.sh <gen|receipts|check|all> [--commit]
# 输出：在 I1_REPO 里按子命令提交（带 --commit 或 all）。每个子命令幂等：已经是最新就什么都不改、不产生空提交。日志在 ${I1_LOGS}。
# 何时用：仅 --with-part2 的 b3-regen。
set -u
KIT="$(cd "$(dirname "$0")" && pwd)"
: "${I1_DIR:?必须设置 I1_DIR（登记集成工具目录，见脚本头部说明）}"
: "${TARGET_VERSION:?必须设置 TARGET_VERSION}"
: "${TARGET_TAG:?必须设置 TARGET_TAG}"
HERE="${I1_DIR}"
source "${HERE}/env.sh"
# I1_BASE_OVERRIDE：env.sh 会把 I1_BASE 写成 main 基点，收尾时“第二章摘要的旧值”应取第二部分改写之前的头，由调用方显式传。
if [ -n "${I1_BASE_OVERRIDE:-}" ]; then export I1_BASE="${I1_BASE_OVERRIDE}"; fi
cd "${I1_REPO}" || exit 2
TRAILER=""; [ -n "${CO_AUTHOR:-}" ] && TRAILER="Co-Authored-By: ${CO_AUTHOR}"
DATE="${I1_DATE:-$(date +%Y%m%d)}"
LABEL="${I1_LABEL:-集成分支}"   # 提交说明里的分支称呼（收尾阶段设成“收尾分支”）

say() { echo "[i1] $*"; }
# tailrun <行数> <命令…>：输出落临时文件，只打印末尾 N 行，返回命令自己的退出码（不能写成 `命令 | tail`：管道会吞掉失败）
tailrun() {
  local n="$1" tmp rc; shift
  tmp="$(mktemp "${TMPDIR:-/tmp}/i1.XXXXXX")"
  "$@" > "${tmp}" 2>&1; rc=$?
  tail -n "${n}" "${tmp}"; rm -f "${tmp}"
  return ${rc}
}
die() { echo "[i1] 失败：$*" >&2; exit 1; }
clean() { [ -z "$(git status --porcelain)" ]; }
commit() { # commit "<说明正文>"
  if clean; then say "没有改动，不提交"; return 0; fi
  git add -A && git commit -q -m "$1${TRAILER:+

${TRAILER}}" || die "提交失败"
  say "已提交 $(git log --oneline -1)"
}

# 两个 Codex 对照模块是否都绿（约 45 秒）
parity_green() {
  python3 -B -m unittest tools.official_client_capture.tests.test_unit_inherit_codex_parity \
    tools.official_client_capture.tests.test_driver_client_table_codex_parity > "${I1_LOGS}/parity.log" 2>&1
}

# ---------------------------------------------------------------- 阶段一：重算生成值
cmd_gen() {
  say "== 重算生成值（基点 ${I1_BASE:0:12}，树 ${I1_REPO}）"
  # 0. 驱动清单：本脚本的其它步骤都不改驱动目录，所以先重建；对照模块里的安装演练用例要求 manifest 与目录一致，必须在它们之前
  tailrun 1 python3 tools/arm64_capture_driver/install.py build-manifest --root tools/arm64_capture_driver || die "驱动清单重建失败"
  for f in tools/arm64_capture_driver/driver/*.py; do
    b="$(basename "${f}")"
    if [ -f "tools/ci/${b}" ] && ! cmp -s "${f}" "tools/ci/${b}"; then die "驱动副本与 tools/ci 不一致：${b}"; fi
  done
  # 1. 指南第二章摘要连锁（18 份清单 source_spec＋两个检查器 FROZEN_PROFILE_SHA256）
  tailrun 6 python3 -B "${HERE}/refreeze-trace-profile.py" --apply ${I1_ALLOW_CHANGED:+--allow-changed "${I1_ALLOW_CHANGED}"} || die "候选追踪冻结画像摘要重冻失败（期望清单被改过且载荷审核不过，或找不到上次冻结的提交）"
  tailrun 3 python3 -B "${KIT}/rebind_guide_ch2.py" --base "${I1_BASE}" --apply --target-version "${TARGET_VERSION}" || die "第二章摘要连锁失败（见上方错误）"
  # 1b. 指南正文里引用“第二章摘要现值”的两处（第二部分之外）同步新值；旧摘要取基点指南的第二章摘要
  OLD_CH2="$(git show "${I1_BASE}:docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md" | python3 -B -c "import sys;from tools import codex_closeout as cc;print(cc.part2_sha(sys.stdin.read()))")"
  NEW_CH2="$(python3 -B -c "import sys;from tools import codex_closeout as cc;print(cc.part2_sha(open('docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md').read()))")"
  if [ "${OLD_CH2}" = "${NEW_CH2}" ]; then
    say "第二章摘要与基点相同（收尾通常不动第二部分），跳过指南正文引用同步"   # 收尾版新增：guide-mentions.py 要求旧摘要≠当前摘要
  else
    tailrun 6 python3 -B "${HERE}/guide-mentions.py" --old "${OLD_CH2}" --apply || die "指南第二章摘要引用同步失败"
  fi
  # 复核：用收尾工具自己的 source_updates 逻辑核连锁结果（测试里断言“指南 1＋清单 18＋检查器 2＝21”个更新，旧摘要取法不对时会红）；
  # 曾因旧摘要取法不对静默“没有待改项”，所以这里独立核一次。更早版本（0.145.0）的两份清单绑历史摘要，按设计不连锁。
  python3 -B -m unittest tools.official_client_capture.tests.test_codex_closeout > "${I1_LOGS}/closeout-test.log" 2>&1 || die "test_codex_closeout 红（第二章连锁更新数不符？）：见 ${I1_LOGS}/closeout-test.log"
  # 2. 对照批次登记（CLOSEOUT_PY／CLOSEOUT_SH，原位幂等）；仍红再补 T2 的批次生成器
  # 先判后改：对照模块已绿就不动登记（regen_closeout 会把早先批次的摘要刷成当前文件摘要，在 tools/ci 没有新改动时是无谓改写）；红了才重算，再红才补 T2 的批次生成器。
  if ! parity_green; then
    say "对照模块红，重算对照批次登记"
    tailrun 2 python3 -B "${HERE}/regen_closeout.py" --write || die "对照批次登记重算失败"
  fi
  if ! parity_green; then
    say "对照模块仍红，补跑 T2 的批次生成器（基点 ${I1_BASE:0:12}）后复测"
    tailrun 2 python3 -I -B "${HERE}/gen_parity_batch.py" "${I1_BASE}" || die "T2 批次生成器失败"
    parity_green || die "对照批次登记后 test_unit_inherit_codex_parity／test_driver_client_table_codex_parity 仍红，见 ${I1_LOGS}/parity.log"
  fi
  # 3. 出站定型面登记：后端零改动的版本不跑 register-surface.py（登记批次条目），后面 ledger-diff 核对复算＝封存即可；后端有改动时先在 I1 目录补登记
  # 4. 依赖声明漂移：有则应用，并重生成 Claude 断言画像与冻结摘要
  if ! PYTHONPATH=. python3 -B "${I1_REGEN_DEPS}" --check > "${I1_LOGS}/regen-deps.log" 2>&1; then
    say "依赖声明有漂移，应用并重生成 Claude 断言画像"
    PYTHONPATH=. python3 -B "${I1_REGEN_DEPS}" >> "${I1_LOGS}/regen-deps.log" 2>&1 || die "regen_deps 失败"
    python3 -B "${HERE}/regen-claude-2-1-226.py" || die "Claude 断言画像重生成失败"
  fi
  # 5. 归属表
  if ! python3 -B tools/ci/unit_ownership.py check > /dev/null 2>&1; then
    say "归属表不一致，重新物化"; tailrun 1 python3 -B tools/ci/unit_ownership.py materialize || die "归属表物化失败"
  fi
  # 7. 版本泄漏、台账完整性、归属
  python3 -B tools/check_version_leak.py > "${I1_LOGS}/version-leak.log" 2>&1 || die "版本泄漏检查红：见 ${I1_LOGS}/version-leak.log"
  python3 -B "${HERE}/ledger-diff.py" || die "出站定型面台账仍有差异，见上方"
  if ! python3 -B tools/check_ledger_completeness.py --upstream-merge-plan docs/egress/maintenance/upstream-v0.1.177-merge-plan.json > "${I1_LOGS}/ledger.log" 2>&1; then
    # 晋升之后（收尾阶段）当前 Active 版本要有终态收据，由收尾 prepare 生成；它之前这一条是预期红，其它原因仍是失败
    if grep -q '缺少终态收据' "${I1_LOGS}/ledger.log" && [ "$(grep -c '🔴' "${I1_LOGS}/ledger.log")" = "1" ]; then
      say "check_ledger_completeness 只报“缺少终态收据”（收尾 prepare 之前的预期红），overlay 复算已由 ledger-diff 核对一致"
    else
      die "check_ledger_completeness 红：见 ${I1_LOGS}/ledger.log"
    fi
  fi
  say "生成值阶段完成"
  [ "${1:-}" = "--commit" ] && commit "chore(codex-${TARGET_TAG}): ${LABEL}重算生成值（第二章摘要连锁、对照批次登记、出站定型面登记、驱动清单、归属表）

${TARGET_VERSION} 登记、指南第二部分换版及候选侧改动并入后的确定性重算，全部由登记集成工具目录（I1_DIR）里的脚本产出，载荷逐项审核：
清单与检查器每个文件只有一行摘要替换；对照批次只重算受登记文件的摘要与残差；出站定型面登记只核对复算与封存一致；
驱动 manifest 按目录整体重建。不含事实映射重绑、身份基线重封与冻结承接收据（各自单独提交）。"
  return 0
}

# ---------------------------------------------------------------- 事实映射重绑：由 tools/ci/reseal_chain.py 的 rebind 步骤负责（后端零改动时自动跳过）
cmd_rebind() { die "事实映射重绑、基线重封、manifest、追踪一律走 tools/ci/reseal_chain.py（指南 4.8.6），本脚本不再提供 rebind/seal"; }

# ---------------------------------------------------------------- （保留）身份基线重封：请用 reseal_chain.py，下面只作应急
cmd_seal() {
  say "== 身份守恒基线重封"
  clean || die "工作树不干净：封存要求受管文件与 HEAD 逐字一致，先提交前面的改动"
  python3 -B "${HERE}/seal-baseline.py" --version "${TARGET_VERSION}" || die "封存失败或守恒模块仍红"
  [ "${1:-}" = "--commit" ] && commit "chore(codex-${TARGET_TAG}): 封存 ${TARGET_VERSION} 身份守恒基线

source_commit=$(git rev-parse HEAD)；受管文件逐字与该提交一致、默认归层为 0；BASELINE_SHA256／BASELINE_CODEX_VERSION／BASELINE_PATH 随之更新，
REGISTERED_CONTROL_CHANGES 保持空。守恒模块 13 项全绿。"
  return 0
}

# ---------------------------------------------------------------- 阶段三：冻结承接收据
cmd_receipts() {
  say "== 冻结承接收据"
  clean || die "工作树不干净：收据 after 取 HEAD"
  python3 -B "${HERE}/freeze-receipts.py" dry || die "干跑失败"
  python3 -B "${HERE}/freeze-receipts.py" gen --date "${DATE}" || die "生成失败"
  [ "${1:-}" = "--commit" ] && commit "chore(egress): ${TARGET_VERSION} ${LABEL}的冻结承接收据（登记、指南第二部分换版、候选侧改动与第二章摘要连锁）

freeze-successor-generate 对 before=${I1_RECEIPT_BEFORE:-基点}、after=$(git rev-parse --short HEAD) 的整体计算结果（只含收据本身，源码另行提交）；
后端有改动时 official_egress_identity_authority.go 的 changeset3 收据需另行单列。"
  return 0
}

# ---------------------------------------------------------------- 快速核对
cmd_check() {
  say "== 快速核对（摘要敏感 Python 模块）"
  bash "${HERE}/run-mods.sh" "check-$(git rev-parse --short=8 HEAD)" "${HERE}/mods-digest.txt"
  grep -v 'rc=0' "${I1_LOGS}/check-$(git rev-parse --short=8 HEAD)/results.txt" || true
}

case "${1:-}" in
  gen) cmd_gen "${2:-}" ;;
  rebind) cmd_rebind "${2:-}" ;;
  seal) cmd_seal "${2:-}" ;;
  receipts) cmd_receipts "${2:-}" ;;
  check) cmd_check ;;
  all) cmd_gen --commit && cmd_receipts --commit ;;   # 之后接 tools/ci/reseal_chain.py（rebind→收据→reseal→收据→manifest→trace）
  *) sed -n 2,16p "$0"; exit 2 ;;
esac
