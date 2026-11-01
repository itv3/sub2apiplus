#!/bin/bash
# 收尾 b3-check：晋升后形态的 Python 定向回归（b3 之后、prepare 之前跑；也用于在模拟晋升树上预演）。
#
# 做什么：逐模块跑 python3 -B -m unittest，记录 rc 与末行；全部结束写 ALLDONE。模块清单取“读取运行时 Catalog 或绑定 Active/Previous 版本”的那批
#   （早先一次收尾的 3 个晋升后红：boundary 的 Active 断言＋两个守卫登记，都在这批里）。顺序跑，本机 16 GiB 不要并行。
# 输入：bash post_promo_mods.sh <仓库根> <日志目录>
#   环境变量 CODEX_SPEC_SOURCE_ROOT（本版 Codex 源码树）和 CAPTURE_TYPESCRIPT_MODULE（TypeScript 解析器）必须已设置——这两个路径随机器和版本而变，
#   脚本不替你猜；closeout.sh 会按参数导出它们。
# 输出：<日志目录>/results.txt（每行“模块 rc=… 末行摘要”，末行 ALLDONE）、每个模块的 .log。
# 何时用：closeout.sh 的 b3-check。
set -u
REPO=${1:?仓库根}; LOG=${2:?日志目录}
export PYTHONDONTWRITEBYTECODE=1
: "${CAPTURE_TYPESCRIPT_MODULE:?必须设置 CAPTURE_TYPESCRIPT_MODULE（TypeScript 解析器路径）}"
: "${CODEX_SPEC_SOURCE_ROOT:?必须设置 CODEX_SPEC_SOURCE_ROOT（本版 Codex 源码树）}"
export CAPTURE_TYPESCRIPT_MODULE CODEX_SPEC_SOURCE_ROOT
mkdir -p "${LOG}"; cd "${REPO}" || exit 2
: > "${LOG}/results.txt"
MODS="${POST_PROMO_MODS:-test_claude_upgrade_boundary_codex test_ci_declared_inputs_records test_codex_identity_conservation test_candidate_test_trace \
test_driver_client_table test_driver_client_table_codex_parity test_unit_inherit_codex_parity test_ci_unit_ownership test_ci_unit_records \
test_codex_closeout test_codex_upgrade_gate_receipt test_ci_read_audit test_vc5_admission test_main_track_lite_expectations \
test_codex_upgrade_canonical test_arm64_capture_driver test_ci_background_preflight test_codex_0151_worktree_successor}"
for m in ${MODS}; do
  f="tools/official_client_capture/tests/${m}.py"; [ -f "${f}" ] || { echo "${m} rc=SKIP(无此模块)" >> "${LOG}/results.txt"; continue; }
  python3 -B -m unittest "tools.official_client_capture.tests.${m}" > "${LOG}/${m}.log" 2>&1; rc=$?
  echo "${m} rc=${rc} $(tail -n 3 "${LOG}/${m}.log" | tr '\n' ' ' | cut -c1-160)" >> "${LOG}/results.txt"
done
for c in "tools/check_version_leak.py"; do   # check_runtime_catalog_projection.py 需要 --dump（Go 导出），不在这里跑
  [ -f "${REPO}/${c}" ] && { python3 -B "${c}" > "${LOG}/$(basename "${c}").log" 2>&1; echo "${c} rc=$? $(tail -n 2 "${LOG}/$(basename "${c}").log" | tr '\n' ' ' | cut -c1-160)" >> "${LOG}/results.txt"; }
done
echo ALLDONE >> "${LOG}/results.txt"
