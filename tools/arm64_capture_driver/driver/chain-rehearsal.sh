#!/bin/bash
# 升级开工前的整链零请求空跑（任务第 13 项）的驱动入口：把 tests/chain_rehearsal.py 包成一条命令，汇总落在
# $RUNROOT/chain-rehearsal/<UTC>/。全程零真实请求：被测的是工具自己的回归用例、故障注入链与录制回放链，不碰生产数据根，
# 不写总账、不发采集批次。
#
#   list
#       列出探针（上一版升级的历史缺陷、恢复分支故障注入）、完整链步骤与本机具备的能力（Linux root／unshare、录制 Campaign、
#       docker＋go、驱动源码、git 历史）。缺能力的探针不会假装通过，一律标“未执行”并写明缺什么。
#   round [--probe <id>]… [--phase VC-n]…
#       一轮查全：每个历史缺陷各用独立夹具单独执行，汇总里一次列全；依赖被阻断的步骤标“未执行”。
#   replay-history [--probe <id>]… [--phase VC-n]…
#       自检：把每个历史缺陷的修复补丁反向打进受管树副本，要求对应探针在同一轮里各自报出；基线必须先通过。
#   full-chain
#       完整链：VC-1 取证 → VC-2～VC-5 连续链 → 评估 → 构建修订 → 段复用 → VC-6 canonical；遇首个失败即停，后面标“未执行”。
#   rehearse [--chain-anyway]
#       开工空跑：先 round，汇总后再另跑一遍 full-chain 确认整体能通（round 发现缺陷默认不跑完整链，先修再重跑）。
#   preview --candidate-evidence <上一版候选 attempt 目录> --labels <新版证据标签声明> --profile <新版候选期望清单> [选项…]
#       登记阶段离线预演（OPT-13）：新版标签与判据对上一版真实候选证据回放 seal 预检的选择器可达性与 assert-rules 全部检查，
#       一次报全；零写入原证据、零请求，登记期间随时可跑。选项见 python3 -m tools.official_client_capture.tests.registration_preview preview --help。
#
# 空跑里“需要外部输入”的探针（没给就如实标“未执行”，不会假装通过）：
#   REHEARSAL_CODEX_BIN=<新版客户端二进制>     tui-callsite-flags-vs-real-binary（真二进制在无网络命名空间里逐个启动 TUI 调用点命令行，需要 Linux root＋unshare）
#   REHEARSAL_PRIOR_CANDIDATE_EVIDENCE=<上一版候选 attempt 目录>   prior-candidate-seal-assert-replay（可加 …_ROOTS／…_OFFICIAL_EVIDENCE／…_OFFICIAL_ROOTS）
#
# 选项：--tree <仓库根> 被测树，默认数据根 $D（真实链在这里跑）；VC-2 离线判据门、阶段解析、生成器重放登记这类要读驱动源码或
#       git 历史的探针需要完整检出，可改指向本轮测试树 $B/test-tree。--work <目录> 工作目录；--keep 保留。
# 退出码：0 全部符合预期；1 发现缺陷（当前树有失败，或历史重放漏报）；2 探针自身出错或用法错误；3 没有缺陷但有未执行项（不能当通过）。
# 与后台验证、采集、入口空跑互斥（真实链各约 10 分钟，且占用 unshare／docker）：先 background-validate.sh status 确认空闲。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
command="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
module="tools.official_client_capture.tests.chain_rehearsal"
tree="$D"
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --tree) tree="${2:?--tree 需要一个目录}"; shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
case "$command" in
  list)
    cd "$tree" && PYTHONPATH="$tree" exec python3 -B -m "$module" list ;;
  preview)
    cd "$tree" && PYTHONPATH="$tree" exec python3 -B -m tools.official_client_capture.tests.registration_preview preview ${args[@]+"${args[@]}"} ;;
  round|replay-history|full-chain|rehearse)
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    out="$RUNROOT/chain-rehearsal/$stamp"
    mkdir -p "$out"; chmod 700 "$RUNROOT/chain-rehearsal" "$out"
    echo "汇总目录：$out"
    cd "$tree" && PYTHONPATH="$tree" nice -n 10 python3 -B -m "$module" "$command" --tree "$tree" --out "$out" ${args[@]+"${args[@]}"} 2>&1 | tee "$out/run.log"
    exit "${PIPESTATUS[0]}" ;;
  *) echo "用法：bash chain-rehearsal.sh list|round|replay-history|full-chain|rehearse|preview [--tree <仓库根>] [选项…]" >&2; exit 2 ;;
esac
