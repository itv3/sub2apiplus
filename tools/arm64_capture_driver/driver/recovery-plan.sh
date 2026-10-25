#!/bin/bash
# 恢复链一条命令（第 18 项）：失败后的"campaign-resume → 工具演进登记 → 对账 → 恢复预览 → 批准／授权 → 续跑"由现场状态推出
# 每一步做没做，已完成的跳过，未完成的按序调用既有受管命令。重复执行安全：中途被打断后再执行同一条命令，结果与不打断相同。
#
#   用法（采集主机 root；推荐先预览再执行）：
#     bash /root/arm64-capture-driver/driver/recovery-plan.sh <恢复计划参数文件>            # 只读计划预览（缺省，等同 --plan）
#     bash /root/arm64-capture-driver/driver/recovery-plan.sh <恢复计划参数文件> --json     # 同上，输出 JSON
#     setsid -f bash /root/arm64-capture-driver/driver/recovery-plan.sh <恢复计划参数文件> --apply \
#       > $RUNROOT/recovery-plan-<轮次>.out 2>&1 < /dev/null                                # 执行
#
#   参数模板：driver/recovery-plan.example.params（KEY=VALUE，经 parse_env.py 同一词法层安全解析，绝不 source）。
#   预览只读：不写任何文件、不改任何账本。执行只写 $RUNROOT/recovery-plan/<轮次>/（步骤记录 steps/、历史 history.ndjson、
#   计划 plans/、受管命令原始输出 raw/、批准意向 approval-intents/、续跑占位与启动记录 continue/）和受管命令自己的产物。
#
#   写账本的动作（campaign-resume、工具演进登记、批准恢复预览、授权、官方首批续跑）各消费一份人工提交的专项批准
#   （$RUNROOT/recovery-plan-approvals/<轮次>/<批准键>.json，意向在 approval-intents/），缺失时只写意向并停下，从不代签。
#   从不调用：accounting-resolve、environment-isolate、request-budget-extend、deadline-extend、record-root-cause-repair、任何 --force。
#
# 退出码：0 完成（续跑已派发／在跑／已结束）或预览成功；1 失败；2 参数或用法错误；3 本轮已有实例在跑；4 需要人工处理。
set -Eeuo pipefail
umask 077
DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH

usage() {
  echo "用法：bash $0 <恢复计划参数文件> [--plan|--apply] [--json]" >&2
}

PARAMS_ARG=""
APPLY=0
EXTRA=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --plan) shift ;;
    --apply) APPLY=1; shift ;;
    --json) EXTRA+=(--json); shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "未知选项：$1" >&2; usage; exit 2 ;;
    *)
      if [ -n "$PARAMS_ARG" ]; then echo "只接受一个参数文件" >&2; exit 2; fi
      PARAMS_ARG="$1"; shift ;;
  esac
done
if [ -z "$PARAMS_ARG" ]; then usage; exit 2; fi
if [ "$APPLY" = 1 ]; then
  if [ "$(id -u)" != 0 ]; then echo "--apply 必须以 root 执行（受管工具与续跑脚本都要求 root）" >&2; exit 2; fi
  exec python3 -B "$DRV/recovery_plan.py" "$PARAMS_ARG" --apply ${EXTRA[@]+"${EXTRA[@]}"}
fi
exec python3 -B "$DRV/recovery_plan.py" "$PARAMS_ARG" --plan ${EXTRA[@]+"${EXTRA[@]}"}
