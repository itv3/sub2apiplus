#!/bin/bash
# VC-5 库表前置的临时改动、恢复与核对（show | apply | restore | verify）；逻辑与口径见 vc5_prereq.py 的模块说明。
# 用法：bash vc5-prereq.sh apply --account <采集账号 id> --group <采集分组 id> --key <要临时关掉的另一把 Key id> --backup-dir <绝对路径>
#
# --key 是采集分组里「要临时关掉的另一把 Key」（VC-5 前置要求分组内只许采集 Key 一把活跃，所以把分组里别的活跃 Key 临时停用），不是采集 Key；
#   采集 Key 必须一直保持 active。0.166 曾误把采集 Key 当 --key 传，apply 把它关了（发现后立刻 restore 补救）。
# 防误传：apply 必须知道采集 Key 是哪一把，--key 等于它就直接拒绝（退出 2，库不动、不写备份）。采集 Key 的编号取自本轮参数文件的 API_KEY_ID
#   （ARM64_VC_ENV，环境变量或 pipeline.env 里给；本脚本自动解析并带上 --capture-key），也可以自己写 --capture-key N 覆盖；两处都取不到时 apply 同样拒绝。
#   restore／verify／show 不需要采集 Key（恢复被误关的采集 Key 时 --key 恰恰就是它）。
set -Eeuo pipefail; umask 077
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CAPTURE_KEY_ARGS=()
case " $* " in
  *" --capture-key "*|*" --capture-key="*) ;;   # 自己给了就以它为准
  *)
    # 在子 shell 里解析本轮参数文件（和流水线其它脚本同一解析器 parse_env.py），取 API_KEY_ID；解析失败或没有就什么都不传，由 apply 自己拒绝。
    CAPTURE_KEY=$( { source "${HERE}/pipeline-lib.sh" && pl_load_round_env && printf '%s' "${API_KEY_ID:-}"; } 2>/dev/null || true )
    if [[ ${CAPTURE_KEY} =~ ^[1-9][0-9]{0,17}$ ]]; then CAPTURE_KEY_ARGS=(--capture-key "${CAPTURE_KEY}"); fi ;;
esac
exec python3 -B "${HERE}/vc5_prereq.py" "$@" ${CAPTURE_KEY_ARGS[@]+"${CAPTURE_KEY_ARGS[@]}"}
