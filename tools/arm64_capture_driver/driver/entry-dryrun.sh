#!/bin/bash
# 入口空跑（E4-02 日常化）的驱动入口：D、RUNROOT 取自 $ARM64_VC_ENV，其余交给 entry_dryrun.py。
#
#   start <bundle 绝对路径> <分支> <40 位提交> [--opening|--reexecute] [--to <步骤>]
#       在数据根 staging 下的演练根里（fixture_only 演练总账，零请求，不写生产总账与正式坐标）用入口编排器跑一遍入口，后台
#       执行、nice 降优先级。默认到 atomic-double（建账本之前那一段）；--to p0-receipt 连建账本之后一起（VC-0 收口会发正式
#       请求，演练根里不做）。--opening：升级开工的空跑，重新执行全集并带读集审计，默认到 p0-receipt；--reexecute：只重新
#       执行全集、不带审计（冷跑计时用）。有采集在跑（执行器
#       整机预约的申请方还活着）就让路、退出 4。同一提交＋同一部署（同一种空跑）已有在跑或已有结论就不重复跑。
#       每次部署后的空跑由后台验证通过后自动接上（background-validate.sh start 默认带 --then-dryrun，到 pre-A3 为止）。
#   stop [--reason <原因>]   停下在跑的空跑（空跑与前台入口门禁共用调度器；background-validate.sh stop 已连空跑一起停）
#   status                    打印全部结论（$RUNROOT/entry-dryrun/）
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
DR=(python3 -B "$DRV/entry_dryrun.py")
command="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
case "$command" in
  start)
    if [ "$#" -lt 3 ]; then echo "用法：bash entry-dryrun.sh start <bundle 绝对路径> <分支> <40 位提交> [--opening] [--to <步骤>]" >&2; exit 2; fi
    exec "${DR[@]}" start --runroot "$RUNROOT" --data-root "$D" --vc-env "$ARM64_VC_ENV" --bundle "$1" --branch "$2" --commit "$3" "${@:4}" ;;
  stop) exec "${DR[@]}" stop --runroot "$RUNROOT" "$@" ;;
  status) exec "${DR[@]}" status --runroot "$RUNROOT" ;;
  *) echo "用法：bash entry-dryrun.sh start|stop|status …" >&2; exit 2 ;;
esac
