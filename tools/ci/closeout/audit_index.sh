#!/bin/bash
# 收尾 b5：在 ARM64 上生成并复核审计索引（codex_audit_index.py generate --no-replay，再 check --evidence-root）。
#
# 做什么：在 VC-6 全部结束（含私有归档、清理决定、deliver-2）且证据根不再被写之后跑；索引逐文件登记证据根摘要，check 会逐文件复核无漂移。
# 输入（在 ARM64 上以 root 运行；closeout.sh 通过 ssh 调用）：
#   bash audit_index.sh --tools <含 codex_audit_index.py 的目录> --outdir <输出目录> --campaign <Campaign 全名> \
#       --target-version <目标版本> --baseline-version <基线版本> --prefix <文件名前缀> --runround <轮次目录> \
#       [--data <证据根，缺省 /root/docker/capture-cli/data>] [--archive <私有归档 tar.gz 绝对路径>] [--evidence-host ARM64]
#   私有归档路径缺省顺序：①轮次目录 vc6/vc6-private-archive.out 里最后一个 .tar.gz；②/root/private-archives 里本版本本 Campaign 最新的一个
#   （codex-<目标版本>-<Campaign>-*.tar.gz）；都取不到就退出码 3，必须手工传 --archive，不要省略（索引要登记归档坐标）。
# 输出：<输出目录>/<前缀>_AUDIT_INDEX.json；末行 AUDIT_INDEX_DONE <路径> <sha256>。
# 何时用：closeout.sh 的 b5-audit-index（ARM64 写步骤，需 CLOSEOUT_ARM64_WRITE=1）。
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1
TOOLS=""; OUTDIR=""; CAMPAIGN=""; TARGET=""; BASELINE=""; PREFIX=""; RUNROUND=""; D=/root/docker/capture-cli/data; ARCHIVE=""; HOST_LABEL=ARM64
while [ $# -gt 0 ]; do
  case "$1" in
    --tools) TOOLS=$2; shift 2 ;;
    --outdir) OUTDIR=$2; shift 2 ;;
    --campaign) CAMPAIGN=$2; shift 2 ;;
    --target-version) TARGET=$2; shift 2 ;;
    --baseline-version) BASELINE=$2; shift 2 ;;
    --prefix) PREFIX=$2; shift 2 ;;
    --runround) RUNROUND=$2; shift 2 ;;
    --data) D=$2; shift 2 ;;
    --archive) ARCHIVE=$2; shift 2 ;;
    --evidence-host) HOST_LABEL=$2; shift 2 ;;
    -h|--help) sed -n 2,16p "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
for need in TOOLS OUTDIR CAMPAIGN TARGET BASELINE PREFIX RUNROUND; do
  [ -n "${!need}" ] || { echo "缺少参数 ${need}（见 --help）" >&2; exit 2; }
done
if [ -z "${ARCHIVE}" ]; then ARCHIVE=$(grep -Eo '/root/private-archives/[^ ]+\.tar\.gz' "${RUNROUND}/vc6/vc6-private-archive.out" 2>/dev/null | tail -1 || true); fi
# 部分轮次的目录里没有 vc6-private-archive.out：退回 /root/private-archives 里本版本本 Campaign 最新的一个归档
if [ -z "${ARCHIVE}" ]; then ARCHIVE=$(ls -t /root/private-archives/codex-${TARGET}-${CAMPAIGN}-*.tar.gz 2>/dev/null | head -1 || true); fi
[ -f "${ARCHIVE}" ] || { echo "找不到私有归档：${ARCHIVE}"; exit 3; }
OUT=${OUTDIR}/${PREFIX}_AUDIT_INDEX.json
python3 -B "${TOOLS}/codex_audit_index.py" generate --evidence-root "${D}" --target-version "${TARGET}" --baseline-version "${BASELINE}" \
  --campaign-id "${CAMPAIGN}" --evidence-host "${HOST_LABEL}" --tool-root "${D}/tools/official_client_capture" \
  --archive "${ARCHIVE}" --no-replay --output "${OUT}"
python3 -B "${TOOLS}/codex_audit_index.py" check --index "${OUT}" --evidence-root "${D}"
echo "AUDIT_INDEX_DONE ${OUT} $(sha256sum "${OUT}" | cut -c1-64)"
