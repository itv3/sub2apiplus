#!/bin/bash
# ARM64 抓包驱动链公共前导（被各脚本 source，不可直接执行）：
#   1. DRV = 脚本所在目录（安装目标 /root/arm64-capture-driver/driver 或仓库内 tools/arm64_capture_driver/driver）
#   2. 读取本轮参数文件 ${ARM64_VC_ENV}（只允许 KEY=VALUE 赋值与注释，其他行一律拒绝）
#   3. 校验必需变量、建立 ${RUNROOT}，导出 D／PYTHONPATH／PATH
# 用法：source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
if [ -z "${BASH_SOURCE[1]:-}" ]; then echo "lib.sh 只能被 source"; exit 2; fi
DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
: "${ARM64_VC_ENV:?ARM64_VC_ENV 未设置（指向本轮 env.sh，见 env.example.sh）}"
test -f "$ARM64_VC_ENV" || { echo "参数文件不存在：$ARM64_VC_ENV"; exit 2; }
if grep -nvE '^(#.*|[A-Z_][A-Z0-9_]*=.*|[[:space:]]*)$' "$ARM64_VC_ENV"; then
  echo "参数文件含非赋值行，拒绝加载：$ARM64_VC_ENV"; exit 2
fi
# shellcheck disable=SC1090
source "$ARM64_VC_ENV"
for v in ROUND STAMP D RUNROOT NEW IN UP CAND B PREV_CANDIDATE HISTORY_TEST_TREE C DC RECEIPT BUNDLE BUNDLE_BRANCH \
         OFFICIAL_CAMPAIGN OFFICIAL_STOP_LEDGER OFFICIAL_STOP_RECEIPT INPUT_RULE_MIGRATION INPUT_TARGET_SNAPSHOT \
         PROJECT_DEADLINE_UTC STAGE_BUDGETS MIN_FREE_GIB FRONTEND_DEVIATION_APPROVED_BY PROFILE_ID PROFILE_DIGEST \
         KILO_BIN KILO_VERSION KILO_SHA256 COMPOSE_DIR COMPOSE_BACKUP PRODUCTION_IMAGE; do
  [ -n "${!v:-}" ] || { echo "参数文件缺少 ${v}：$ARM64_VC_ENV"; exit 2; }
done
case "$C$DC" in *[!0-9a-f]*) echo "C／DC 必须是 40 位小写 sha1"; exit 2;; esac
[ "${#C}" = 40 ] && [ "${#DC}" = 40 ] || { echo "C／DC 必须是完整 40 位 sha1"; exit 2; }
[ -d "$RUNROOT" ] || mkdir -p "$RUNROOT"; [ "$(python3 -c "import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))" "$RUNROOT")" = 0o700 ] || chmod 700 "$RUNROOT"
export D PYTHONPATH=. PATH=/usr/local/go/bin:/opt/node-v20/bin:$PATH
NEWDIR=$D/evidence/campaigns/$NEW
W=$D/control/$IN
TOOLS=$D/tools/official_client_capture
L=$D/control/$UP-timing-ledger
G=$D/control/$NEW-candidate-gates
AS=$D/control/$NEW-assertions
# 下一批次序号：按 control/vc/batches 中已 COMMIT 的最大序号 +1
next_seq() { python3 -c "import glob,os,sys; xs=[int(os.path.basename(p).split('-')[0]) for p in glob.glob(sys.argv[1]+'/control/vc/batches/*.json')]; print(max(xs)+1 if xs else 1)" "$NEWDIR"; }
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
