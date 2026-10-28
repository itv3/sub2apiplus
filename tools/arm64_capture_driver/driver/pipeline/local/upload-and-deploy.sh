#!/bin/bash
# 本机侧“打包 → 算期望摘要 → 上传 → 在采集主机上校验或受监督部署”配套脚本（zsh/bash 均可，本机运行）。
#
# 用法：
#   bash upload-and-deploy.sh <提交或分支> --verify-only   # 只校验、不动数据根（演练用）
#   bash upload-and-deploy.sh <提交或分支>                 # 真部署（集成分支定稿后用；需先确认没有在跑的验证或流水线）
# 例：bash upload-and-deploy.sh $(git -C <仓库> rev-parse <集成分支>)
#
# 做什么：
#   1. 把提交解析成 40 位完整号（rev-parse 现取）；
#   2. git archive 打包 docs tools（gzip），算 sha256；
#   3. 把包展开到本机临时目录，用该提交自带的 arm64_supervised_deploy.py 现算 tool_digest（独立于采集主机的第二个来源）；
#   4. 上传包与 .sha256 到采集主机的 <PREP>/stage/，并把该提交自带的 deploy-commit.sh 与 pipeline-lib.sh 传到 <PREP>/bootstrap/
#      （部署会重装驱动，所以不能用驱动里正在被替换的那份；也兼容“采集主机上还是上一版驱动、没有新脚本”的第一次部署）；
#   5. ssh 执行 bootstrap 里的 deploy-commit.sh（日志落采集主机的 <PREP>/logs/，同时回显到本机）。
# 本脚本传给采集主机的 deploy-commit.sh 取自本机仓库的“当前工作树”文件，请在要部署的提交已检出时运行，或确认两者一致。
# 不做：不 push、不碰生产库、不删文件（本机临时目录由系统清理，采集主机上旧暂存树只改名）。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../pipeline-lib.sh"
pl_local_init
REPO=${PIPE_LOCAL_REPO}
REF=${1:?"用法：upload-and-deploy.sh <提交或分支> [--verify-only]"}
MODE=${2:-}
case "${MODE}" in ""|--verify-only) ;; *) echo "第二个参数只能是 --verify-only"; exit 2 ;; esac
C=$(git -C "${REPO}" rev-parse --verify "${REF}^{commit}")
[[ ${C} =~ ^[0-9a-f]{40}$ ]]
echo "提交 ${C}：$(git -C "${REPO}" log -1 --format=%s "${C}")"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ap-deploy-${C:0:12}.XXXXXX")
TGZ=${WORK}/stage-${C:0:12}.tgz
git -C "${REPO}" archive --format=tar "${C}" docs tools | gzip -n > "${TGZ}"
( cd "${WORK}" && shasum -a 256 "stage-${C:0:12}.tgz" > "stage-${C:0:12}.tgz.sha256" )
echo "包 $(du -h "${TGZ}" | cut -f1)，$(cut -d' ' -f1 "${TGZ}.sha256")"
mkdir "${WORK}/x" && tar -xzf "${TGZ}" -C "${WORK}/x"
DIGEST=$(PYTHONDONTWRITEBYTECODE=1 python3 -B - "${WORK}/x" <<'PY'
import importlib.util, sys
from pathlib import Path
sys.dont_write_bytecode = True
stage = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("dep_local", stage / "tools" / "arm64_supervised_deploy.py")
m = importlib.util.module_from_spec(spec); sys.modules["dep_local"] = m; spec.loader.exec_module(m)
print(m.tool_digest(stage / "tools" / "official_client_capture")[0])
PY
)
[[ ${DIGEST} =~ ^[0-9a-f]{64}$ ]]
echo "本机算出 tool_digest=${DIGEST}"
ssh "${PIPE_SSH_HOST}" "install -d -m 700 ${REMOTE_PREP} ${REMOTE_PREP}/stage ${REMOTE_PREP}/logs"
scp -q "${TGZ}" "${TGZ}.sha256" "${PIPE_SSH_HOST}:${REMOTE_PREP}/stage/"
pl_push_bootstrap "${PIPE_HOME}/deploy-commit.sh"
ssh "${PIPE_SSH_HOST}" "chmod 600 ${REMOTE_PREP}/stage/stage-${C:0:12}.tgz* && chmod 700 ${REMOTE_PREP}/bootstrap/deploy-commit.sh"
echo "== 在采集主机上执行（日志同步写 ${REMOTE_PREP}/logs/）"
pl_ssh "ARM64_VC_ENV=${ARM64_VC_ENV} PIPE_DRIVER_DIR=${REMOTE_DRIVER} PIPE_PREP=${REMOTE_PREP} ${STAGE_NAME:+STAGE_NAME=${STAGE_NAME}} bash ${REMOTE_PREP}/bootstrap/deploy-commit.sh ${C} ${DIGEST} ${MODE}"
