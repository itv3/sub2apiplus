#!/bin/bash
# 入口空跑（E4-02 日常化）的驱动入口：D、RUNROOT 取自 $ARM64_VC_ENV，其余交给 entry_dryrun.py。
#
#   start <bundle 绝对路径> <分支> <40 位提交> [--opening|--reexecute|--unit-inherit-request <请求>] [--unit-inherit-full-gates-request <配套请求>] [--audit-policy <策略>] [--to <步骤>]
#       在数据根 staging 下的演练根里（fixture_only 演练总账，零请求，不写生产总账与正式坐标）用入口编排器跑一遍入口，后台
#       执行、nice 降优先级。默认到 atomic-double（建账本之前那一段）；--to p0-receipt 连建账本之后一起（VC-0 收口会发正式
#       请求，演练根里不做）。--opening：升级开工的空跑，重新执行全集并带读集审计，默认到 p0-receipt；--reexecute：只重新
#       执行全集、不带审计（冷跑计时用）。有采集在跑（执行器
#       整机预约的申请方还活着）就让路、退出 4。同一提交＋同一部署（同一种空跑）已有在跑或已有结论就不重复跑。
#       每次部署后的空跑由后台验证通过后自动接上（background-validate.sh start 默认带 --then-dryrun，到 pre-A3 为止）。
#       按客户端（B5a-10）：本轮客户端取参数文件的 CLIENT，空跑绑定它最新的部署收据，结论文件名带客户端前缀（Codex 为空），
#       同一提交＋同一部署两客户端各一份；Claude 的结论另带没通过各步的分类（登记过的待接入、pre-A3 场景占位与真实失败）。
#       --unit-inherit-request（B5a-08 合同）：把逐单元承接请求交给入口编排器，只执行依赖变化或缺少有效记录的单元。
#       可从首轮同时固定带 --unit-inherit-full-gates-request：pre-A3 沿用后选这份独立批准的实际组合请求，不改来源授权。
#       精确读集登记另带 --audit-policy：只随承接请求或显式 --opening 使用，摘要绑定结论，取锁后及收口时复核；
#       后台验证明确带策略时不会自动接缺少当前批准的空跑，应显式带请求与原策略执行本入口。
#       开跑前先做日常维护（见 housekeeping），根盘越过停线就不跑（结论 failed，没通过的一步记 disk）。
#   housekeeping [--dry-run] [--retention-days <天>] [--keep-dryruns <次>] [--go-cache auto|off|<目录>]
#       单独做一遍日常维护：清 Go 编译缓存里 6 小时以上没用过的条目、清理单元执行记录库（保留期默认 30 天，仍被 v2 认证与
#       P0 v2 证据引用的运行连同记录与日志都留着）、只留最近 5 次空跑的演练根与产物，再查根盘余量（与 guard.sh 同一条停线，
#       操作员阈值取参数文件 MIN_FREE_GIB）。--dry-run 只算不删。打印报告；退出码 0 根盘在停线以内，5 越过停线。
#   stop [--reason <原因>]   停下在跑的空跑（空跑与前台入口门禁共用调度器；background-validate.sh stop 已连空跑一起停）
#   status                    打印全部结论（$RUNROOT/entry-dryrun/）
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
DR=(python3 -B "$DRV/entry_dryrun.py")
command="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
case "$command" in
  start)
    if [ "$#" -lt 3 ]; then echo "用法：bash entry-dryrun.sh start <bundle 绝对路径> <分支> <40 位提交> [--opening|--reexecute|--unit-inherit-request <请求>] [--unit-inherit-full-gates-request <配套请求>] [--audit-policy <策略>] [--to <步骤>]" >&2; exit 2; fi
    exec "${DR[@]}" start --runroot "$RUNROOT" --data-root "$D" --vc-env "$ARM64_VC_ENV" --bundle "$1" --branch "$2" --commit "$3" "${@:4}" ;;
  housekeeping) exec python3 -B "$DRV/entry_housekeeping.py" run --data-root "$D" --runroot "$RUNROOT" --min-free-gib "${MIN_FREE_GIB:-40}" "$@" ;;
  stop) exec "${DR[@]}" stop --runroot "$RUNROOT" "$@" ;;
  status) exec "${DR[@]}" status --runroot "$RUNROOT" ;;
  *) echo "用法：bash entry-dryrun.sh start|housekeeping|stop|status …" >&2; exit 2 ;;
esac
