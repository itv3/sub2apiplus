#!/bin/bash
# 流水线脚本集公共前导（被 source，不可直接执行）。
#
# 为什么有这个文件：上一版把轮次、版本、路径全部写死在每个脚本里（工作目录、轮次目录、分支、账号号、bundle 名……），
# 换一版就要全文替换一遍，漏一处就是事故。本文件把“每轮会变的东西”收成两层：
#   1. 本轮参数文件 ARM64_VC_ENV（驱动 parse_env.py 安全解析，RUNROOT／NEW／CAND／TARGET_VERSION／BUNDLE_BRANCH 等都在里面）；
#   2. 流水线自己的可选覆盖文件 pipeline.env（模板 pipeline.env.example，只允许下面白名单里的键、值只允许安全字符）。
# 脚本里不再出现任何版本号、Campaign 名、提交号、摘要、账号号、轮次目录。
#
# 约定（source 之后可用）：
#   PIPE_HOME        本目录（流水线脚本安装位置，随驱动一起安装、受驱动清单保护，运行中不要重装驱动）
#   PIPE_DRIVER_DIR  驱动目录（缺省 ${PIPE_HOME} 的上一级）
#   PREP             本轮流水线工作目录（状态文件、日志、ORDER、交接标记；缺省 /root/pipeline-prep/<RUNROOT 目录名>）
#   RR               本轮参数文件里的 RUNROOT（轮次目录）
#   pl_load_round_env  经 parse_env.py 解析 ARM64_VC_ENV 并导出全部键；pl_defaults 补 PREP／RR／PDIR
#   pl_retry         对“可证明幂等”的步骤做有限重试（见函数说明）
#   pl_sigign_ok     检查没有继承“忽略 SIGHUP”（nohup 起的进程会让后台验证里的挂断检测用例必红）

if [ -z "${BASH_SOURCE[1]:-}" ]; then echo "pipeline-lib.sh 只能被 source"; exit 2; fi
PIPE_HOME=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1

# pipeline.env 允许的键（与 pipeline.env.example 一一对应；未知键一律拒绝，防止拼写错误悄悄失效）
PL_ALLOWED_KEYS=" ARM64_VC_ENV PIPE_PREP PIPE_DRIVER_DIR PIPE_COMPOSE_ENV PIPE_ADMIN_GATEWAY CAPTURE_ACCOUNT_ID CAPTURE_ACCOUNT_NAME CAPTURE_GROUP_ID CAPTURE_USER_EMAIL CAPTURE_KEY_NAME PIPE_RETRY_MAX PIPE_RETRY_SLEEP PIPE_RETRY_VC4_GATES PIPE_RETRY_VC4_ALL PIPE_NEED_VC1 PIPE_NEED_VC2 PIPE_NEED_VC3 PIPE_NEED_VC4 PIPE_NEED_BGV PIPE_NEED_VC5 PIPE_NEED_VC6 PIPE_DISK_WARN_PCT_MARGIN PIPE_DISK_WARN_GIB_MARGIN PIPE_SSH_HOST PIPE_LOCAL_REPO PIPE_REMOTE_RUNROOT PIPE_REMOTE_PREP PIPE_REMOTE_DATA_ROOT PIPE_DATA_ROOT PIPE_OFFICIAL_SOURCE PIPE_LOCAL_BRANCH PIPE_LOCAL_WORKTREE PIPE_REMOTE_DRIVER_DIR PIPE_TOKEN_MIN_MINUTES PIPE_CAPTURE_CONTAINER PIPE_CAPTURE_AUTH "

# 读取 pipeline.env：不执行文件，只逐行解析 KEY=VALUE；已经在环境里的键不覆盖（命令行临时覆盖优先）。
pl_load_config() {
  local file=${PIPELINE_ENV:-} line key value
  # 没指定 PIPELINE_ENV 时，依次找本机用户目录与采集主机默认位置；都没有就只用环境变量和默认值
  if [ -z "${file}" ]; then
    for line in "${HOME:-/nonexistent}/.arm64-pipeline.env" /root/pipeline-prep/pipeline.env; do [ -f "${line}" ] && { file=${line}; PIPELINE_ENV=${line}; break; }; done
  fi
  [ -n "${file}" ] || return 0
  [ -f "${file}" ] || { echo "PIPELINE_ENV 指向的文件不存在：${file}" >&2; return 2; }
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in ''|'#'*) continue ;; esac
    case "${line}" in *=*) ;; *) echo "pipeline.env 行格式不对（要 KEY=VALUE）：${line}" >&2; return 2 ;; esac
    key=${line%%=*}; value=${line#*=}
    case "${PL_ALLOWED_KEYS}" in *" ${key} "*) ;; *) echo "pipeline.env 含未知键：${key}" >&2; return 2 ;; esac
    [[ "${value}" =~ ^[A-Za-z0-9_./:@,+=-]*$ ]] || { echo "pipeline.env 的 ${key} 含不允许的字符（只许字母数字和 _./:@,+=-）" >&2; return 2; }
    if [ -z "${!key+x}" ]; then printf -v "${key}" '%s' "${value}"; export "${key}"; fi
  done < "${file}"
  return 0
}
pl_load_config || exit 2
[ -z "${PIPELINE_ENV:-}" ] || export PIPELINE_ENV
export PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(dirname "${PIPE_HOME}")}

# 解析本轮参数文件并导出全部键（与驱动 lib.sh 同一解析器：精确键集合、任何命令形态拒绝）。
pl_load_round_env() {
  local ex
  [ -n "${ARM64_VC_ENV:-}" ] || { echo "缺 ARM64_VC_ENV（本轮参数文件路径；环境变量或 pipeline.env 里给）" >&2; return 1; }
  ex=$(python3 -B "${PIPE_DRIVER_DIR}/parse_env.py" "${ARM64_VC_ENV}") || return 1
  eval "${ex}"
}
# 补流水线层的派生坐标。必须在 pl_load_round_env 之后调用。
pl_defaults() {
  RR=${RUNROOT}
  PREP=${PIPE_PREP:-/root/pipeline-prep/$(basename "${RUNROOT}")}
  PDIR=${PREP}/pipeline
  DRV=${PIPE_DRIVER_DIR}
  export RR PREP PDIR DRV
}

pl_sigign_ok() { local v; v=$(awk '/^SigIgn:/{print $2}' /proc/$$/status 2>/dev/null); [ -z "${v}" ] || [ $(( 0x${v} & 1 )) -eq 0 ]; }

# 入口门禁 Go 编译缓存的容量前置检查（start-entry.sh 的 C10 用）。
#   pl_check_go_cache_capacity <缓存目录> <最小 GiB>   打印一行结论；返回 0＝通过或不适用，1＝容量不足
# 为什么：入口门禁的 GOCACHE 是数据根上一级 entry-gates-work/go-build-cache。ARM64 上这个目录曾是手工挂的 4 GiB tmpfs，
# 平时缓存是暖的够用；腾盘清掉 Go 编译缓存后冷启动全量重建超过 4 GiB，写满后 6 项门禁报 no space left on device，白跑近 2 小时。
# 规则：目录（不存在则取最近的已存在祖先）落在 tmpfs／ramfs 上时总容量必须 ≥ 最小 GiB（缺省 10）；落在磁盘上的由根盘检查（C5）约束。
pl_check_go_cache_capacity() {
  local dir=$1 min=${2:-10} probe fstype size_m
  probe=${dir}
  while [ ! -e "${probe}" ] && [ "${probe}" != / ] && [ "${probe}" != . ]; do probe=$(dirname "${probe}"); done
  fstype=$(stat -f -c %T "${probe}" 2>/dev/null) || fstype=未知
  case "${fstype}" in
    tmpfs|ramfs) ;;
    *) echo "入口门禁 Go 编译缓存 ${dir} 在磁盘上（${fstype}），容量由根盘检查约束"; return 0 ;;
  esac
  size_m=$(df -P -BM "${probe}" 2>/dev/null | awk 'NR==2{gsub("M","",$2); print $2}')
  case "${size_m}" in ''|*[!0-9]*) echo "入口门禁 Go 编译缓存 ${dir} 在内存盘上，但读不出容量"; return 1 ;; esac
  if [ "${size_m}" -lt $(( min * 1024 )) ]; then
    echo "入口门禁 Go 编译缓存 ${dir} 是 ${fstype}，总容量 $(( size_m / 1024 )) GiB < ${min} GiB：冷启动（腾盘清过 Go 缓存之后）全量重建会写满，门禁报 no space left on device。先扩容（mount -o remount,size=${min}G ${probe}，需确认可用内存够），或卸载后改走磁盘"
    return 1
  fi
  echo "入口门禁 Go 编译缓存 ${dir} 是 ${fstype}，总容量 $(( size_m / 1024 )) GiB ≥ ${min} GiB（腾盘清掉 Go 缓存后冷启动仍装得下）"
}
# 本轮输入目录前置检查（start-entry.sh 的 C11 用）：prepare-round-inputs.sh 必须已经跑过。
#   pl_check_round_inputs <RUNROOT> <数据根 D> <输入目录名 IN> [回退依据收据路径]   打印结论；返回 0＝齐全，1＝缺
# 为什么：漏跑 prepare-round-inputs.sh 时入口便宜检查两项直接红（required-parameters 缺 P0 回退依据、plan-audit 缺基线画像），
# 入口白占一次起跑；这两个文件正是它的产物，起跑前先查。
pl_check_round_inputs() {
  local rr=$1 data=$2 in_dir=$3 rollback=${4:-$1/rollback-catalog-promotion-receipt.json} missing="" file
  for file in "${rollback}" "${data}/control/${in_dir}/baseline-profile.json"; do
    [ -s "${file}" ] || missing="${missing} ${file}"
  done
  if [ -n "${missing}" ]; then
    echo "缺本轮输入文件：${missing# }。先在采集主机跑 prepare-round-inputs.sh（用法见其头注释：新参数文件、上一轮 VC-6 画像目录晋升收据、基线画像 JSON 与摘要）"
    return 1
  fi
  echo "本轮输入已就绪（P0 回退依据与基线画像都在，即 prepare-round-inputs.sh 已跑）"
}

# 有限重试，只用于“可证明幂等”的步骤（只读检查，或脚本文档明确写了幂等、重入安全）：
#   pl_retry <标签> <允许重试的退出码列表，逗号分隔，空串＝任何非零> <命令…>
# 最多再试 ${PIPE_RETRY_MAX:-2} 次，每次前等 ${PIPE_RETRY_SLEEP:-60} 秒；每次失败用调用方定义的 log 写一行 RETRY。
# 退出码不在允许列表里（确定性失败，例如身份对不上）立刻返回，不重试；重试用尽返回最后一次的退出码。
# 绝不用于：会占用批次序号、发真实请求、写批准件或账本、改账号以外状态且无幂等保证的步骤（见 README 的“重试清单”）。
pl_retry() {
  # 局部变量一律加 _pl_ 前缀：被重试的函数可能也用 n、rc 这类名字，bash 是动态作用域，重名会互相踩
  local _pl_label=$1 _pl_codes=$2 _pl_n=0 _pl_rc _pl_max=${PIPE_RETRY_MAX:-2} _pl_nap=${PIPE_RETRY_SLEEP:-60}
  shift 2
  while true; do
    "$@"; _pl_rc=$?
    [ "${_pl_rc}" -eq 0 ] && return 0
    if [ -n "${_pl_codes}" ] && ! [[ ",${_pl_codes}," == *",${_pl_rc},"* ]]; then
      pl_say "NOTE ${_pl_label} 失败 rc=${_pl_rc}（不在可重试退出码 ${_pl_codes} 内，确定性失败，不重试）"; return "${_pl_rc}"
    fi
    _pl_n=$((_pl_n + 1))
    if [ "${_pl_n}" -gt "${_pl_max}" ]; then pl_say "NOTE ${_pl_label} 失败 rc=${_pl_rc}，已重试 ${_pl_max} 次仍失败"; return "${_pl_rc}"; fi
    pl_say "RETRY ${_pl_label} 第 ${_pl_n} 次失败 rc=${_pl_rc}，${_pl_nap} 秒后重试（可证明幂等的步骤；上限 ${_pl_max} 次）"
    sleep "${_pl_nap}"
  done
}
# 写日志：调用方定义了 log 就用它（会写状态文件），否则只打印。
pl_say() { if declare -F log >/dev/null 2>&1; then log "$*"; else echo "$(date -u +%FT%TZ) $*"; fi; }

# ------------------------------------------------------------------------------------------------------------
# 本机（开发机）侧助手：local/ 下的脚本用。它们不在采集主机上运行，轮次坐标一律向采集主机上的本轮参数文件“问”（parse_env.py），
# 不在本机再抄一份，免得两边不一致。需要 pipeline.env 里的 ARM64_VC_ENV（采集主机上本轮参数文件的路径）和 PIPE_SSH_HOST（缺省 ARM64）。
# ------------------------------------------------------------------------------------------------------------
pl_local_init() {
  PIPE_SSH_HOST=${PIPE_SSH_HOST:-ARM64}
  PIPE_LOCAL_REPO=${PIPE_LOCAL_REPO:-$(git -C "${PIPE_HOME}" rev-parse --show-toplevel 2>/dev/null || true)}
  [ -n "${PIPE_LOCAL_REPO}" ] || { echo "找不到本机仓库（在 pipeline.env 里给 PIPE_LOCAL_REPO）" >&2; return 1; }
  REMOTE_DRIVER=${PIPE_REMOTE_DRIVER_DIR:-/root/arm64-capture-driver/driver}
  [ -n "${ARM64_VC_ENV:-}" ] || { echo "缺 ARM64_VC_ENV（采集主机上本轮参数文件的路径，写在 pipeline.env 里）" >&2; return 1; }
  REMOTE_VARS=$(ssh -n "${PIPE_SSH_HOST}" "python3 -B '${REMOTE_DRIVER}/parse_env.py' '${ARM64_VC_ENV}'") || { echo "采集主机上的本轮参数文件解析失败" >&2; return 1; }
  REMOTE_RUNROOT=$(pl_remote_var RUNROOT)
  [ -n "${REMOTE_RUNROOT}" ] || { echo "取不到采集主机本轮参数文件的 RUNROOT" >&2; return 1; }
  REMOTE_PREP=${PIPE_REMOTE_PREP:-/root/pipeline-prep/$(basename "${REMOTE_RUNROOT}")}
  REMOTE_PIPE=${REMOTE_DRIVER}/pipeline
}
# 从 pl_local_init 取回的参数文件导出里读一个键。parse_env 用 shlex.quote 输出：值只含安全字符时不带引号
# （export KEY=VALUE，路径、版本号基本都是这种），含空格等字符时才带单引号（export KEY='VALUE'），两种形态都要认。
pl_remote_var() { printf '%s\n' "${REMOTE_VARS}" | sed -n -e "s/^export $1='\(.*\)'\$/\1/p" -e "s/^export $1=\([^']*\)\$/\1/p" | head -1; }
# 经 ssh 在采集主机上执行（不吃本机标准输入；带保活）
pl_ssh() { ssh -n -o ServerAliveInterval=30 "${PIPE_SSH_HOST}" "$@"; }
# 一轮的第一次部署时，采集主机上安装的还是上一版驱动，没有本目录的新脚本：把需要的脚本连同 pipeline-lib.sh 传到
# ${REMOTE_PREP}/bootstrap/（驱动目录之外，不破坏驱动清单），从那里运行。参数：要传的脚本文件名（相对本目录）。
pl_push_bootstrap() {
  ssh "${PIPE_SSH_HOST}" "install -d -m 700 '${REMOTE_PREP}' '${REMOTE_PREP}/bootstrap'" || return 1
  scp -q "${PIPE_HOME}/pipeline-lib.sh" "$@" "${PIPE_SSH_HOST}:${REMOTE_PREP}/bootstrap/" || return 1
}
