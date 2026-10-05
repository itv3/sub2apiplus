#!/bin/bash
# 修复轮后台验证（E4-01，指南「修好接着跑」）的驱动入口：D、RUNROOT 取自 $ARM64_VC_ENV，其余交给 background_validation.py。
#
#   start <bundle 绝对路径> <分支> <40 位提交> [--profile full-gates] [--record-store <目录>] [--no-dryrun]
#       修复提交部署之后起后台验证：绑定数据根最新部署收据，nice 降优先级跑入口门禁 full-gates 组合的重新执行全集
#       （B-09 的来源必须同次真实执行），门禁前核对数据根部署的就是这个提交。同一提交＋同一部署已有在跑或
#       已有结论就不重复起；在跑的其它后台验证先停下（superseded）。测试树、运行目录与 Go 缓存独立放在
#       本轮 background-validation/work 下，准入清理不再触碰后台缓存。结论在 $RUNROOT/background-validation/。通过之后默认接着
#       空跑入口到 pre-A3（E4-02 日常化，entry-dryrun.sh；有采集在跑就让路），--no-dryrun 不接。
#   stop [--reason <原因>]   停下在跑的后台验证（修复轮跑定向回归之前）
#   check-boundary            批次边界：当前部署的结论 failed／aborted 退出 3（拒绝派发下一批），其余退出 0
#   require-passed            VC-5 验收前：当前部署的结论是 passed 才退出 0，否则退出 3
#   status                    打印全部结论
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
BV=(python3 -B "$DRV/background_validation.py")
command="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
case "$command" in
  start)
    if [ "$#" -lt 3 ]; then echo "用法：bash background-validate.sh start <bundle 绝对路径> <分支> <40 位提交> [--profile …] [--record-store …] [--no-dryrun]" >&2; exit 2; fi
    bundle=$1; branch=$2; commit=$3; shift 3
    extra=(--then-dryrun)
    rest=()
    for arg in "$@"; do if [ "$arg" = --no-dryrun ]; then extra=(); else rest+=("$arg"); fi; done
    exec "${BV[@]}" start --runroot "$RUNROOT" --data-root "$D" --vc-env "$ARM64_VC_ENV" \
      --bundle "$bundle" --branch "$branch" --commit "$commit" ${extra[@]+"${extra[@]}"} ${rest[@]+"${rest[@]}"} ;;
  stop) exec "${BV[@]}" stop --runroot "$RUNROOT" "$@" ;;
  check-boundary|require-passed) exec "${BV[@]}" "$command" --runroot "$RUNROOT" --data-root "$D" ;;
  status) exec "${BV[@]}" status --runroot "$RUNROOT" ;;
  *) echo "用法：bash background-validate.sh start|stop|check-boundary|require-passed|status …" >&2; exit 2 ;;
esac
