#!/bin/bash
# 收尾 b4：把 ARM64 上 VC-6 留下的四份阶段收据逐字节取回本机，放进仓库 docs/egress/maintenance/（Framework §5.7 终态收据登记的四份）。
#
# 做什么：只读 ARM64（ssh cat），本机写文件；写之前核对 ARM64 与本机的 sha256 相等；目标文件已存在则拒绝覆盖。
# 输入：
#   bash collect_stage_receipts.sh <仓库工作树> <ARM64 轮次目录> <文件名前缀，如 CODEX_CLI_<基线标记>_TO_<目标标记>>
#   环境变量 ARM64_HOST（ssh 主机别名，缺省 ARM64）。三个位置参数都必填，没有缺省值。
# 对应关系（已用历史终态收据复核过四份摘要）：
#   CATALOG_PROMOTION_RECEIPT        <- <vc6-root>/catalog-promotion-receipt.json
#   POST_PROMOTION_GATE_RECEIPT      <- <vc6-root>/post-promotion-gates.receipt.json
#   PRODUCTION_ACTIVATION_RECEIPT    <- <vc6-root>/activation-receipt.json
#   RUNTIME_PROFILE_REMOVAL_RECEIPT  <- <vc6-retire-root>/removal-receipt.json
#   <vc6-root>、<vc6-retire-root> 来自轮次目录 vc6/vc6-root.txt、vc6-retire-root.txt。
# 输出：四个 <前缀>_<名称>.json，逐行打印“摘要  字节数  相对路径”，末行 STAGE_RECEIPTS_DONE；任何不一致退出码 3。
# 何时用：VC-6 PASS 之后（closeout.sh 的 b4-stage-receipts）。
set -Eeuo pipefail
REPO=${1:?仓库工作树}
RUNROUND=${2:?ARM64 轮次目录}
PREFIX=${3:?文件名前缀}
HOST=${ARM64_HOST:-ARM64}
OUT=${REPO}/docs/egress/maintenance
sha256_of() { if command -v shasum > /dev/null 2>&1; then shasum -a 256 "$1" | cut -c1-64; else sha256sum "$1" | cut -c1-64; fi; }
VC6_ROOT=$(ssh -n "${HOST}" "cat ${RUNROUND}/vc6/vc6-root.txt")
RETIRE_ROOT=$(ssh -n "${HOST}" "cat ${RUNROUND}/vc6/vc6-retire-root.txt")
copy() { # $1 远端路径 $2 本机文件名
  local dst="${OUT}/${PREFIX}_$2.json"
  [ ! -e "${dst}" ] || { echo "已存在，拒绝覆盖：${dst}"; exit 3; }
  ssh -n "${HOST}" "cat $1" > "${dst}"
  local l r
  l=$(sha256_of "${dst}"); r=$(ssh -n "${HOST}" "sha256sum $1 | cut -c1-64")
  [ "${l}" = "${r}" ] || { echo "摘要不一致：$1"; rm -f "${dst}"; exit 3; }
  echo "${l}  $(wc -c < "${dst}" | tr -d ' ')  ${dst#${REPO}/}"
}
copy "${VC6_ROOT}/catalog-promotion-receipt.json" CATALOG_PROMOTION_RECEIPT
copy "${VC6_ROOT}/post-promotion-gates.receipt.json" POST_PROMOTION_GATE_RECEIPT
copy "${VC6_ROOT}/activation-receipt.json" PRODUCTION_ACTIVATION_RECEIPT
copy "${RETIRE_ROOT}/removal-receipt.json" RUNTIME_PROFILE_REMOVAL_RECEIPT
echo "STAGE_RECEIPTS_DONE"
