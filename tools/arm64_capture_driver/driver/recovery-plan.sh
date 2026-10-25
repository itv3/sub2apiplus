#!/bin/bash
# 恢复链一条命令（第 18 项）：失败后的"campaign-resume → 工具演进登记 → 对账 → 恢复预览 → 批准／授权 → 续跑"由现场状态推出
# 每一步做没做，已完成的跳过，未完成的按序调用既有受管命令。重复执行安全：中途被打断后再执行同一条命令，结果与不打断相同。
#
#   用法（采集主机 root；推荐先预览再执行）：
#     bash /root/arm64-capture-driver/driver/recovery-plan.sh <恢复计划参数文件>            # 只读计划预览（缺省，等同 --plan）
#     bash /root/arm64-capture-driver/driver/recovery-plan.sh <恢复计划参数文件> --json     # 同上，输出 JSON
#     bash .../recovery-plan.sh <参数文件> --follow-latest   # 目标之后已有后继且最新后继明确失败时，本次改指它（预览与执行都可加）
#     setsid -f bash /root/arm64-capture-driver/driver/recovery-plan.sh <恢复计划参数文件> --apply \
#       > $RUNROOT/recovery-plan-<轮次>.out 2>&1 < /dev/null                                # 执行
#
#   参数模板：driver/recovery-plan.example.params（KEY=VALUE，经 parse_env.py 同一词法层安全解析，绝不 source）。
#   预览只读：不写 Campaign、两本账与运行根（受管只读接口可能刷新数据根之外的身份记忆化缓存，见 recovery_plan.py 文档）。执行只写 $RUNROOT/recovery-plan/<轮次>/（步骤记录 steps/、历史 history.ndjson、
#   计划 plans/、受管命令原始输出 raw/、批准意向 approval-intents/、续跑占位与启动记录 continue/）和受管命令自己的产物。
#
#   对账（reconcile-runs、reconcile）会写项目总账（入账，可能判永久停线），不需要人工批准，与 fix-and-continue 一致；
#   处理完暂停（账务、环境、预算、截止）或补好批准后，必须用同一参数文件、同一轮次重跑（批准、续跑占位、对账暂停对象都按轮次存放）。
#   写账本的动作（campaign-resume、工具演进登记、批准恢复预览、授权、官方首批续跑）各消费一份人工提交的专项批准
#   （$RUNROOT/recovery-plan-approvals/<轮次>/<批准键>.json，意向在 approval-intents/），缺失时只写意向并停下，从不代签。
#   验收打断注入：--inject-crash-after <步骤>（resume|evolution|reconcile-runs|reconcile|approve|authorize|continue-inflight|continue-spawned）
#   让执行在该步动作做完、记录写入之前以 137 退出（等价 kill -9）。只供验收，只对 --apply 有效；本脚本默认清掉
#   RECOVERY_PLAN_CRASH_AFTER 与 RECOVERY_PLAN_ACCEPTANCE，只有这个显式选项才会为本次执行设上（recovery_plan.py 要两个变量同时设才生效）。
#   从不调用：accounting-resolve、environment-isolate、request-budget-extend、deadline-extend、record-root-cause-repair、任何 --force。
#
# 退出码：0 完成（续跑已派发／在跑／已结束）或预览成功；1 失败；2 参数或用法错误；3 本轮已有实例在跑；4 需要人工处理。
set -Eeuo pipefail
umask 077
DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH
# 默认清掉验收打断注入的两个变量：环境里残留的值不能让生产执行被打断。
unset RECOVERY_PLAN_CRASH_AFTER RECOVERY_PLAN_ACCEPTANCE

usage() {
  echo "用法：bash $0 <恢复计划参数文件> [--plan|--apply] [--json] [--follow-latest] [--inject-crash-after <步骤>（只供验收，只对 --apply）]" >&2
}

PARAMS_ARG=""
APPLY=0
CRASH_AFTER=""
EXTRA=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --plan) shift ;;
    --apply) APPLY=1; shift ;;
    --json) EXTRA+=(--json); shift ;;
    --follow-latest) EXTRA+=(--follow-latest); shift ;;
    --inject-crash-after)
      if [ "$#" -lt 2 ]; then usage; exit 2; fi
      CRASH_AFTER="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "未知选项：$1" >&2; usage; exit 2 ;;
    *)
      if [ -n "$PARAMS_ARG" ]; then echo "只接受一个参数文件" >&2; exit 2; fi
      PARAMS_ARG="$1"; shift ;;
  esac
done
if [ -z "$PARAMS_ARG" ]; then usage; exit 2; fi
if [ -n "$CRASH_AFTER" ] && [ "$APPLY" != 1 ]; then echo "--inject-crash-after 只对 --apply 有效" >&2; exit 2; fi
if [ "$APPLY" = 1 ]; then
  if [ "$(id -u)" != 0 ]; then echo "--apply 必须以 root 执行（受管工具与续跑脚本都要求 root）" >&2; exit 2; fi
  if [ -n "$CRASH_AFTER" ]; then
    export RECOVERY_PLAN_ACCEPTANCE=1 RECOVERY_PLAN_CRASH_AFTER="$CRASH_AFTER"
    echo "验收打断注入已开启：在 ${CRASH_AFTER} 之后强制退出（137）。仅供验收。" >&2
  fi
  exec python3 -B "$DRV/recovery_plan.py" "$PARAMS_ARG" --apply ${EXTRA[@]+"${EXTRA[@]}"}
fi
exec python3 -B "$DRV/recovery_plan.py" "$PARAMS_ARG" --plan ${EXTRA[@]+"${EXTRA[@]}"}
