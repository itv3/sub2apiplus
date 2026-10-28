#!/bin/bash
# 本机完整 make test-capture-tools，并生成“本地收据”（Codex 升级工具优化第 4 项）。
#
# 为什么要有：要进入入口门禁或后台验证的修复提交，先在本机把完整的 make test-capture-tools 跑一遍再部署起跑（本机约 48 分钟、ARM64 约 23 分钟，不占门禁）。
# 上一轮入口门禁第三轮就是修复提交只跑了定向测试，注释里写了版本号，test_driver_client_table 红，白跑 1.8 小时并多部署一次。
# 起跑前置检查（entry-preflight.sh 的 local-capture-receipt、后台验证 background_preflight.py 的 local-capture-receipt）核对这里生成的收据：
# 完整提交号一致、命令就是 make test-capture-tools、退出码 0、工作树干净、耗时合理、封口对得上。
#
# 做什么：在被测仓库（缺省当前目录所在的仓库）的 HEAD 上跑 make test-capture-tools，不论成败都写收据——红的也写，如实记录退出码与日志末尾，
#   被拦下时能看到是哪个提交红了。跑之前和跑之后工作树都必须干净（有未提交改动或未跟踪文件，测试跑的就不是这个提交本身；收据记 tree_clean=false，起跑检查会拒绝）。
# 用法：bash local-capture-tools-receipt.sh --out-dir <收据目录> [--repo <仓库根>]
#   收据目录也可用环境变量 LOCAL_RECEIPT_DIR；产物 <收据目录>/capture-tools-<完整提交号>.json 与同名 .log（完整输出）。
#   收据要上传到 ARM64 本轮的 $RUNROOT/local-receipts/ 才会被起跑检查看到（local-upload.sh 同类做法）。
#   本机重负载约定：这是 make 门禁级的重负载，一次只跑一路；本机若有排队用的重负载锁脚本，请经它调用本脚本。
# 退出码：与 make 一致（通过 0）；用法错误 2。
set -u
OUT_DIR=${LOCAL_RECEIPT_DIR:-}
REPO=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out-dir) OUT_DIR=${2:-}; shift 2 ;;
    --repo) REPO=${2:-}; shift 2 ;;
    *) echo "用法：bash local-capture-tools-receipt.sh --out-dir <收据目录> [--repo <仓库根>]" >&2; exit 2 ;;
  esac
done
[ -n "$OUT_DIR" ] || { echo "缺少收据目录：--out-dir 或环境变量 LOCAL_RECEIPT_DIR" >&2; exit 2; }
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECKS="$HERE/../opening_checks.py"
[ -f "$CHECKS" ] || { echo "找不到同目录树里的 opening_checks.py：$CHECKS" >&2; exit 2; }
if [ -n "$REPO" ]; then cd "$REPO" || exit 2; fi
ROOT=$(git rev-parse --show-toplevel) || { echo "不在 git 仓库里" >&2; exit 2; }
cd "$ROOT" || exit 2
COMMIT=$(git rev-parse HEAD)
mkdir -p "$OUT_DIR" && chmod 700 "$OUT_DIR"
LOG="$OUT_DIR/capture-tools-${COMMIT}.log"
clean() { [ -z "$(git status --porcelain --untracked-files=all)" ] && echo true || echo false; }
CLEAN_BEFORE=$(clean)
if [ "$CLEAN_BEFORE" != true ]; then echo "注意：工作树不干净，收据会记 tree_clean=false，起跑前置检查会拒绝它" >&2; fi
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "本机完整 make test-capture-tools：提交 ${COMMIT} 开始 ${STARTED}，完整输出写入 ${LOG}"
RC=0
make test-capture-tools > "$LOG" 2>&1 || RC=$?
FINISHED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CLEAN_AFTER=$(clean)
TREE_CLEAN=false; if [ "$CLEAN_BEFORE" = true ] && [ "$CLEAN_AFTER" = true ]; then TREE_CLEAN=true; fi
# 跑的过程中 HEAD 被换走了：测试跑的不是开始时记下的那个提交，收据作废（不写成通过）。
if [ "$(git rev-parse HEAD)" != "$COMMIT" ]; then echo "运行期间 HEAD 变了，收据作废" >&2; exit 2; fi
python3 -B "$CHECKS" capture-receipt-write --dir "$OUT_DIR" --commit "$COMMIT" --exit-code "$RC" --started "$STARTED" --finished "$FINISHED" \
  --host "$(hostname -s)" --tree-clean "$TREE_CLEAN" --log "$LOG" || exit 2
if [ "$RC" -eq 0 ]; then echo "make test-capture-tools 通过（${STARTED} → ${FINISHED}）"; else echo "make test-capture-tools 没通过（退出码 ${RC}）：收据已如实记录，修好后对新提交重跑" >&2; fi
exit "$RC"
