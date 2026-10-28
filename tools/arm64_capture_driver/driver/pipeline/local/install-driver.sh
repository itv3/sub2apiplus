#!/bin/bash
# 只换采集主机上的驱动（不做受监督部署）：入口编排器等驱动文件修复后，把集成分支指定提交的 tools/arm64_capture_driver 装到采集主机驱动目录。
# 用法：bash install-driver.sh <本机分支名>
# 前提：采集主机上没有入口、门禁、后台验证、流水线在跑（脚本会先检查，有就拒绝）。入口或 VC 步骤运行时不得换驱动。
# 注意：驱动清单（manifest.json）必须与目录内容一致，装之前会 --check；安装收据绑定当前受管工具部署收据，换驱动不需要重新部署工具。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../pipeline-lib.sh"
pl_local_init
REPO=${PIPE_LOCAL_REPO}
BR=${1:?"用法：install-driver.sh <本机分支名>"}
C=$(git -C "${REPO}" rev-parse --verify "refs/heads/${BR}^{commit}")
C12=${C:0:12}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/install-driver.XXXXXX")
DRIVER_ROOT=$(dirname "${REMOTE_DRIVER}")
STAGE_DIR=${REMOTE_PREP}/driver-install
busy=$(ssh -o ConnectTimeout=10 "${PIPE_SSH_HOST}" "pgrep -af 'unit_executor|entry-gates|background_validation|entry_orchestrator|entry-dryrun|vc-pipeline.sh|evolve-and-resume.sh' | grep -v pgrep || true")
if [ -n "${busy}" ]; then echo "采集主机上仍有入口／门禁／后台验证／流水线进程，拒绝换驱动：" >&2; echo "${busy}" >&2; exit 1; fi
git -C "${REPO}" archive --format=tar "${C}" tools/arm64_capture_driver | gzip -n > "${WORK}/driver-${C12}.tgz"
( cd "${WORK}" && shasum -a 256 "driver-${C12}.tgz" > "driver-${C12}.tgz.sha256" )
ssh -o ConnectTimeout=10 "${PIPE_SSH_HOST}" "install -d -m 700 ${STAGE_DIR}"
scp -q "${WORK}/driver-${C12}.tgz" "${WORK}/driver-${C12}.tgz.sha256" "${PIPE_SSH_HOST}:${STAGE_DIR}/"
DATA_ROOT=$(pl_remote_var D)
ssh -o ConnectTimeout=10 "${PIPE_SSH_HOST}" "C12=${C12} W=${STAGE_DIR} DRIVER_ROOT=${DRIVER_ROOT} DATA_ROOT=${DATA_ROOT} bash -s" <<'REMOTE'
set -Eeuo pipefail
( cd "${W}" && sha256sum -c "driver-${C12}.tgz.sha256" )
test ! -e "${W}/${C12}" || { echo "暂存目录已存在，先人工处理：${W}/${C12}"; exit 3; }
mkdir "${W}/${C12}" && tar -xzf "${W}/driver-${C12}.tgz" -C "${W}/${C12}"
S="${W}/${C12}/tools/arm64_capture_driver"
python3 -B "${S}/install.py" build-manifest --root "${S}" --check
python3 -B "${S}/install.py" install --source "${S}" --target "${DRIVER_ROOT}" --data-root "${DATA_ROOT}"
python3 -B "${DRIVER_ROOT}/install.py" verify --target "${DRIVER_ROOT}" --data-root "${DATA_ROOT}"
python3 -B "${DRIVER_ROOT}/install.py" verify --target "${DRIVER_ROOT}" --data-root "${DATA_ROOT}" --client claude
cmp "${DRIVER_ROOT}/driver/entry_orchestrator.py" "${S}/driver/entry_orchestrator.py" && echo "驱动已换成 ${C12}"
REMOTE
echo "完成：驱动 ${C12}。下一步先 entry.sh --plan 确认入口门禁判“沿用”，再用 start-entry.sh 起跑。"
