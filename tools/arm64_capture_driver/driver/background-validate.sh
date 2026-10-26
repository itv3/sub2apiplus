#!/bin/bash
# 修复轮后台验证（E4-01，指南「修好接着跑」）的驱动入口：D、RUNROOT 取自 $ARM64_VC_ENV，其余交给 background_validation.py。
#
#   start <bundle 绝对路径> <分支> <40 位提交> [--profile full-gates] [--record-store <目录>] [--no-dryrun]
#       修复提交部署之后起后台验证：绑定数据根最新部署收据，nice 降优先级跑入口门禁 full-gates 组合的重新执行全集
#       （B-09 的来源必须同次真实执行），门禁前核对数据根部署的就是这个提交。同一提交＋同一部署已有在跑或
#       已有结论就不重复起；在跑的其它后台验证先停下（superseded）。测试树、运行目录与 Go 缓存独立放在
#       本轮 background-validation/work 下，准入清理不再触碰后台缓存。结论在 $RUNROOT/background-validation/。通过之后默认接着
#       空跑入口到 pre-A3（E4-02 日常化，entry-dryrun.sh；有采集在跑就让路），--no-dryrun 不接。
#       --declared-sample off|always|every:N（步骤 5 WP3b，缺省 off）：声明级承接命令单元的读集证据来源，原样交给后台起的入口门禁再到执行器（正式验证轮
#       显式带 always）；非法值立即退出 2，非 off 的取值进结论的输入合同，缺省时结论与命令行不变。通过之后默认接着的入口空跑不带它（要采样的空跑
#       另用 entry-dryrun.sh start … --declared-sample …，或本命令加 --no-dryrun）。本脚本把三个位置参数之后的选项原样交给 background_validation.py。
#       --unit-inherit-request <请求> --unit-inherit-source-result <旧后台结论> [--allow-unverified-ancestry]（步骤 6）：跨提交、跨部署承接旧后台轮次的成果——
#       旧结论须 passed／failed、进程组已退出且留有已发布的逐单元清单（被 stop 的运行不发布清单，其成果不能承接）；--work 须是旧结论的 WORK；旧提交须能
#       验证是新提交的祖先（确定不是则永远拒绝，只有「无法验证」才可加 --allow-unverified-ancestry，结论标 unverified）；没有任何单元被承接时结论
#       outcome=full_rerun。这两个选项只有本命令 start 支持，入口空跑 entry-dryrun.sh 没有。
#       --gate-scope client-plan --select-base <最近一次发版的 40 位提交> [--include-claude]（Codex 升级工具优化第 9 项）：只跑本客户端计划的采集工具测试（Codex 计划不跑
#       不受改动影响的 Claude 重模块），原样交给入口门禁。结论是子集：要配 --no-dryrun，不沿用来源结论、不签 B-09 全集收据；Codex 计划的结论给 Codex 自己的
#       准入用，Claude 一侧的 check-boundary／require-passed 与统一准入一律拒绝（防串用）。要全集结论先 stop 再不带本选项重新 start。
#       缺省（Codex）：本命令对 Codex 缺省带 --gate-scope auto——由入口门禁自己决定，基点取测试树最近的发版 tag（或另给 --select-base <40 位提交>），任何一步不行
#       （找不到发版 tag、归属表自己在改动里、选择器抛错……）回退全集，原因写进结论的 gate_scope_resolved 与运行日志。起跑时并不知道结论是子集还是全集：
#       实际是子集的，check-boundary／require-passed 的提示与运行日志写明“按客户端筛选（… 不是全集）【子集：… 跳过 N/M 个测试模块 …】”，只给 Codex 准入用，
#       通过后不自动接入口空跑（结论的 dryrun 记 skipped，要空跑另 entry-dryrun.sh start）；实际回退全集的是全集结论，两个客户端都认、照常接入口空跑。
#       --gate-scope full 显式要全集（本验证与同提交已有 auto 结论的输入合同不同，须先 stop 再重新 start）；Claude 轮次（CLIENT 非 codex）不带 auto；
#       与逐单元承接、B-09 全集收据、读集合同同用时 auto 在起跑就按全集处理（start 的输出里写明原因）。
#   reuse <bundle 绝对路径> <分支> <40 位提交> [--no-dryrun]
#       同树重部署后沿用已通过的测试结论（优化第 5 项）：来源是同一提交的通过结论（带沿用绑定）；在来源的工作目录里用 entry-gates.sh --bind-only
#       现算新部署上的绑定（含部署后核对，不跑测试），工具身份五摘要、提交、门禁集合、执行器、调度策略、输入清单、平台环境逐项相同且未过有效期（24 小时）
#       才沿用，写一份绑定新部署的通过结论；任一项变了就拒绝（退出码 3）并写明哪项，要求重验。start 缺省先试沿用（--reuse auto），--no-reuse 跳过。
#       沿用成功之后仍接部署后空跑（与 start 同一语义，缺省带 --then-dryrun，--no-dryrun 才关）：沿用只省掉测试，新部署上的入口空跑不省。
#   preflight <bundle 绝对路径> <分支> <40 位提交> [--only <检查项,…>]
#       只做起跑前置检查（优化第 4 项）：冻结收据、基线重封、重部署、准入绑定、主机 Go 版本五项逐项核对，退出码 0 通过、3 不通过（打印哪一项为什么）；不起后台验证。
#       主机 Go 版本（host-toolchain）：执行器固定 PATH 里的 go 不得低于被验证提交 backend/go.mod 的 go 指令，否则 GOTOOLCHAIN=local 的 make 单元退出 2、整轮白跑；
#       升级主机 Go 必须在没有后台验证运行时进行。
#       start 缺省先做同一套检查（--preflight on；Python 命令行缺省也是 on），不通过拒绝起跑；--no-preflight（即 --preflight off）显式跳过。
#   stop [--reason <原因>]   停下在跑的后台验证（修复轮跑定向回归之前）
#   check-boundary            批次边界：当前部署的结论 failed／aborted 退出 3（拒绝派发下一批），其余退出 0
#   require-passed            VC-5 验收前：当前部署的结论是 passed 才退出 0，否则退出 3
# 按客户端（B5a-10）：本轮客户端（参数 CLIENT）的部署收据决定 start 绑定哪一份、check-boundary／require-passed 取哪一份所绑定的
#   结论（客户端参数取客户端表，Codex 为空，命令行与改造前相同）。两客户端都纳管时全量门禁只跑一次、结论绑定两份收据：
#   另一客户端的轮次 start 时查到同一提交与部署已有在跑或已有结论就不再起，只登记自己的入口空跑，门禁通过后两客户端的
#   入口空跑都接上（见 background_validation.py）。
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
    preflight_args=(--preflight on)
    reuse_args=(--reuse auto)
    # 按客户端筛选（优化第 9 项）：Codex 缺省 --gate-scope auto，操作者自己给了 --gate-scope（含 =形式）就不再带；Claude 轮次不带，保持全集。
    scope_args=(); if [ "$CT_CLIENT" = codex ]; then scope_args=(--gate-scope auto); fi
    rest=()
    for arg in "$@"; do
      if [[ "$arg" == --gate-scope || "$arg" == --gate-scope=* ]]; then scope_args=(); fi
      if [ "$arg" = --no-dryrun ]; then extra=(); elif [ "$arg" = --no-preflight ]; then preflight_args=(--preflight off); elif [ "$arg" = --no-reuse ]; then reuse_args=(--reuse off); else rest+=("$arg"); fi
    done
    exec "${BV[@]}" start --runroot "$RUNROOT" --data-root "$D" --vc-env "$ARM64_VC_ENV" \
      --bundle "$bundle" --branch "$branch" --commit "$commit" ${extra[@]+"${extra[@]}"} ${preflight_args[@]+"${preflight_args[@]}"} ${reuse_args[@]+"${reuse_args[@]}"} ${scope_args[@]+"${scope_args[@]}"} ${rest[@]+"${rest[@]}"} \
      ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} ;;
  reuse)
    if [ "$#" -lt 3 ]; then echo "用法：bash background-validate.sh reuse <bundle 绝对路径> <分支> <40 位提交> [--no-dryrun]" >&2; exit 2; fi
    bundle=$1; branch=$2; commit=$3; shift 3
    # 沿用成功之后仍接部署后空跑（验收修正必修 6）：与 start 同一语义，缺省带 --then-dryrun，--no-dryrun 才关。
    extra=(--then-dryrun)
    rest=()
    for arg in "$@"; do
      if [ "$arg" = --no-dryrun ]; then extra=(); else rest+=("$arg"); fi
    done
    exec "${BV[@]}" reuse --runroot "$RUNROOT" --data-root "$D" --vc-env "$ARM64_VC_ENV" --bundle "$bundle" --branch "$branch" --commit "$commit" ${extra[@]+"${extra[@]}"} ${rest[@]+"${rest[@]}"} ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} ;;
  preflight)
    if [ "$#" -lt 3 ]; then echo "用法：bash background-validate.sh preflight <bundle 绝对路径> <分支> <40 位提交> [--only …]" >&2; exit 2; fi
    bundle=$1; branch=$2; commit=$3; shift 3
    exec "${BV[@]}" preflight --runroot "$RUNROOT" --data-root "$D" --bundle "$bundle" --branch "$branch" --commit "$commit" "$@" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} ;;
  stop) exec "${BV[@]}" stop --runroot "$RUNROOT" "$@" ;;
  check-boundary|require-passed) exec "${BV[@]}" "$command" --runroot "$RUNROOT" --data-root "$D" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} ;;
  status) exec "${BV[@]}" status --runroot "$RUNROOT" ;;
  *) echo "用法：bash background-validate.sh start|reuse|preflight|stop|check-boundary|require-passed|status …" >&2; exit 2 ;;
esac
