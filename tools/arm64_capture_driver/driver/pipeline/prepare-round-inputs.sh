#!/bin/bash
# 新一轮的输入目录准备（采集主机侧，<驱动目录>/pipeline/prepare-round-inputs.sh，幂等：已存在就核对不覆盖）。
# 用法：bash prepare-round-inputs.sh <新参数文件(已填好，装入前先过 parse_env)> <回退依据收据(上一轮 VC-6 画像目录晋升收据)> <基线画像 JSON> <基线画像 sha256>
# 做的事：
#   1. 解析新参数文件，建 RUNROOT（0700），装入 env.sh（已存在则不覆盖，只报差异）；
#   2. 把上一轮 VC-6 画像目录晋升收据复制成 $RUNROOT/rollback-catalog-promotion-receipt.json（P0 回退依据），断言它的 target_version 等于本轮 BASELINE_VERSION；
#   3. 建输入目录 $D/control/$IN（0700），放 baseline-profile.json（基线版本的 Active 画像），核对给定摘要；
#   4. 用已安装驱动的 parse_env.py 只读解析装好的 env.sh，打印是否通过。
# 不写账本、不动 Campaign；不删除任何文件。
set -Eeuo pipefail
umask 077
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
NEWENV=${1:?"用法：prepare-round-inputs.sh <新参数文件> <回退依据收据> <基线画像json> <基线画像sha256>"}
PROMO=${2:?"缺回退依据收据"}
PROFILE_SRC=${3:?"缺基线画像 JSON"}
PROFILE_SHA=${4:?"缺基线画像 sha256"}
[[ ${PROFILE_SHA} =~ ^[0-9a-f]{64}$ ]] || { echo "基线画像 sha256 必须是 64 位小写十六进制"; exit 2; }
ARM64_VC_ENV=${NEWENV} pl_load_round_env || { echo "新参数文件被 parse_env 拒绝：${NEWENV}"; exit 1; }
RR=${RUNROOT}
IN_DIR=${D}/control/${IN}
install -d -m 700 "${RR}" "${IN_DIR}"
if [ ! -e "${RR}/env.sh" ]; then cp "${NEWENV}" "${RR}/env.sh"; chmod 600 "${RR}/env.sh"; echo "已装入 env.sh"; else echo "env.sh 已存在，不覆盖（与新参数文件的差异：$(diff -q "${RR}/env.sh" "${NEWENV}" >/dev/null && echo 无 || echo 有)）"; fi
if [ ! -e "${RR}/rollback-catalog-promotion-receipt.json" ]; then cp "${PROMO}" "${RR}/rollback-catalog-promotion-receipt.json"; chmod 600 "${RR}/rollback-catalog-promotion-receipt.json"; fi
cmp "${PROMO}" "${RR}/rollback-catalog-promotion-receipt.json"
python3 -B - "${RR}/rollback-catalog-promotion-receipt.json" "${BASELINE_VERSION}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["target_version"] == sys.argv[2], ("回退依据收据的目标版本与本轮基线版本不一致", d.get("target_version"), sys.argv[2])
print("回退依据：晋升收据 target=%s rollback=%s 通过" % (d["target_version"], d.get("rollback_version")))
PY
if [ ! -e "${IN_DIR}/baseline-profile.json" ]; then cp "${PROFILE_SRC}" "${IN_DIR}/baseline-profile.json"; chmod 600 "${IN_DIR}/baseline-profile.json"; fi
[ "$(sha256sum "${IN_DIR}/baseline-profile.json" | cut -d' ' -f1)" = "${PROFILE_SHA}" ] && echo "baseline-profile.json 摘要 ${PROFILE_SHA:0:12}… 通过" || { echo "baseline-profile.json 摘要不符"; exit 1; }
echo "== parse_env 只读解析"
python3 -B "${PIPE_DRIVER_DIR}/parse_env.py" "${RR}/env.sh" | grep -E "^export (TARGET_VERSION|NEW|IN|CAND|B|RUNROOT|HISTORICAL_SOURCE_ROOT|CODEX_BIN|CODEX_BIN_SHA256|TARGET_SOURCE|BASELINE_SOURCE|TARGET_PACKAGE|ACTIVE_PROFILE|PRE_A3_CERTIFICATION|CAMPAIGN_PREFIX)=" | cut -c1-200
echo PREPARE_INPUTS_OK
