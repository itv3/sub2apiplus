#!/bin/bash
# 入口门禁（E2-04）：一次运行跑完入口要的全部门禁——采集工具测试、check-egress-spec 的各个子检查、后端三组测试、三组
# lint、前端检查、部署脚本测试与 pre-A3 场景——全部交给统一调度执行器（驱动随附的 unit_executor.py run-gates），在整机
# 额度内并行，一项失败其余照跑，全部跑完再按门禁项汇总。P0 证据、VC-0 预跑记录、部署前全量门禁记录与 pre-A3 认证都取自
# 这一次运行（entry_gates.py export 导出；pre-A3 认证由认证模块 issue 核对场景全集与网络计数后签发）。
#
# 组合（--profile）：
#   entry（默认）：全部门禁项＋pre-A3 场景。要求数据根已部署本提交（最新部署收据的整树摘要等于测试树的受管树）；本轮
#     pre-A3 认证已有、或有工具身份与策略未变的可复用认证时沿用它，pre-A3 场景不纳入本次运行；
#   full-gates：不含 pre-A3（部署前全量门禁，arm64-full-gates.sh 调用）；
#   preflight：只含 make test 的组成（VC-0 预跑，vc0-gate-target.sh 调用）；
#   pre-a3：只含 pre-A3 场景（入口门禁沿用、只有 pre-A3 要重做时编排器调用，E3-02）。测试树、部署绑定与一致性核对同 entry；
#   regression：定向回归（E4-01，修复轮的部署前提）：只含采集工具测试组，全集通过下只执行静态依赖闭包里有改动文件的测试
#     单元；改动落在闭包选不出的地方（后端、前端、文档、出站规格）时用 --with-gates <门禁项,…> 人工补门禁项。
# --with-gates（E4-01）：给任一组合补门禁项（pre-A3 除外），逗号分隔。
# --require-deployed-all（B5a-10）：同 --require-deployed，另核对本轮客户端之外每个已纳管客户端的部署也是本提交（整树摘要与
#   部署一致性，entry_steps.py deploy-check）；两客户端都纳管时后台验证用它，一次门禁结论对部署事务签出的每一份收据都成立，
#   核对过的收据写进总摘要来源的 deploy_receipts_others。
# --require-deployed（E4-01）：任一组合都先核对数据根部署的就是本提交（含 pre-A3 的组合本来就核对）；后台验证用它把结论
#   绑定到这次部署。
# --wait-lock <秒>（E4-01）：同一工作目录已有入口门禁在跑时排队等，最多等这么久（缺省 0：直接拒绝并发）；后台验证用。
# pre-A3（E3-02）：场景单元的命令只带场景名与固定的场景父目录 $D/staging/pre-a3-scenarios，可以承接；认证从本次运行的
#   清单 executor/unit-manifest.json 与记录库组装（pre-a3-path-certification/v2），每个场景引用一条正式执行记录。
# 隔离：测试树里的单元在私有挂载命名空间里遮住 /root/oauth-capture（与 lib.sh 的 isolated_run 同一做法），树外只读字节码
#   缓存；pre-A3 场景在数据根的生产布局里运行（受管树经生产别名访问的分支也要覆盖到），用生产字节码共享层与身份记忆化。
# Linux 上不执行 macOS 专用的部署脚本测试（写进门禁记录的 not_executed；CI 在 macos-15 上照常执行）。
# 模式（B-09）：默认 re-execute；full-set-pass 必须带 --full-set-request 并通过全集准入，否则全量重跑。旧模式说明：先查记录，只执行
#   其余单元；re-execute（重新执行全集）不承接。两种模式的单元执行记录都入库（--record-store，默认数据根之外跨轮次固定的
#   $(dirname $D)/unit-records），执行器另写清单 executor/unit-manifest.json 并自检。门禁项里有承接的单元时两份 P0 证据
#   写 v2（E3-03：以运行清单为证据，另登记记录库与测试树五摘要，签发与 VC-0 收口按记录库逐条重验）；记录库里没有已发布
#   的清单、五摘要算不出来时不写，总摘要的 p0_evidence_withheld 写明原因。
# 逐单元承接（B5a-08）：--mode unit-inherit --unit-inherit-request <JSON>（合同见驱动 unit_inherit.py）。来源记录可来自多次运行、
#   多个提交，不能承接的单元只重跑它自己；没给请求时回退重新执行全集，不能与 B-09 全集请求同用。执行器现算承接上下文要本轮
#   客户端、入口阶段（--unit-inherit-stage，缺省 pre-ledger）与数据根 control 目录里每个已纳管客户端最新的部署收据。与 B-09
#   一样核对部署、沿用能重放的原工作区、不做 Go 预热、通过后保留测试树。可带 --audit-reads [--audit-strict]（执行的单元照常
#   取证）与读集合同；宿主输入清单与收窄提案只许 re-execute。pre-A3 认证与 P0 证据照常取自这次运行，签发时按决策收据重验
#   形成期证明。--unit-inherit-propose <新目录>：在同一个执行环境里只让执行器判定、写出承接提案（当前上下文、来源集合与批准
#   要签的 binding.json）后退出 0，不执行单元、不导出、不签发；组合与认证坐标用正式运行的同一套（激活认证已在时不写任何文件）。
# 环境（E3-01）：门禁清单生成与执行器都在白名单环境里运行（env -i 只放行 PATH、HOME、语言与时区、TMPDIR、代理与证书、
#   Go／Docker／Node／pnpm 的变量，再显式给字节码、历史源码与 TypeScript 的设置）：单元能看到的环境就是环境指纹里的那一份，
#   本轮参数文件导出的键（D、STAMP、各认证坐标……）不再进入单元环境。
#
# 测试树与字节码缓存（--work，默认数据根之外跨轮次固定的 $(dirname $D)/entry-gates-work，见下方说明）通过即删。
# 产物（主体目录 --out，默认 $RUNROOT/entry-gates/entry-gates-<UTC 时间戳>）：entry-gates.json（总摘要）、logs/<门禁项>.gate.json
#   与 logs/full-regression.gate.json（make test 的组成全部通过与否）、p0/{check-egress-spec,test-capture-tools}.json（P0 证据）、
#   preflight.json（VC-0 预跑记录）、full-gates-summary.json（全量门禁记录，full-gates／entry）、pre-a3-executor-summary.json、
#   executor/（执行器记录与逐单元日志）、executor.log。不写候选门禁目录、候选目录、时间账本与 Campaign，零模型请求。
#
# 用法（采集主机 root；make test 里有挂断检测用例，必须 setsid -f 启动，不能 nohup）：
#   ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] \
#     [--go-warmup auto|off] [--with-gates <门禁项,…>] [--require-deployed|--require-deployed-all] [--out <主体目录>] \
#     [--work <测试树与缓存所在目录>] [--pycache <字节码缓存目录>] [--policy-activation <激活认证>] \
#     [--pre-a3-certification <pre-A3 认证坐标>] [--pre-a3-mode auto|present|run] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] \
#     [--unit-inherit-request <JSON> | --unit-inherit-propose <新目录>] [--unit-inherit-stage pre-ledger|post-ledger|formal:<campaign_id>] \
#     [--record-store <单元执行记录库>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle> <分支> <40 位提交> [<前端依赖目录>] \
#     > $RUNROOT/entry-gates.out 2>&1 < /dev/null
#   后三个选项供入口编排器（entry.sh，E2-06）用：激活认证与 pre-A3 认证坐标覆盖参数文件里的值（重做时编排器给新坐标）；
#   pre-A3 来源由编排器按步骤记录判定后指定——present 只复核并沿用已有认证，run 一定新跑（不按「工具五摘要与策略未变」
#   的旧规则在认证目录里找可复用的，那条规则不看测试、指南、录制数据等输入）；auto 是单独运行时的原有行为。
#   --audit-reads（E3-04 读集审计，只许 --mode re-execute）：每个正式执行的单元包在 strace 下，核对实际读取都在声明的输入
#   范围里（测试树单元读到数据根也报出），超出即判失败；明细在 executor/audit/read-audit.json。
# 退出码：0 全部门禁通过（entry 组合还要 pre-A3 认证签发或沿用成功）；1 有门禁未通过（门禁结论，测试树保留供排查）；
#   2 用法错误；3 准备或执行失败（bundle、测试树、前端依赖、字节码缓存、部署不一致、并发锁、执行器出错），没有门禁结论。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] [--parallel <正整数>] [--with-gates <门禁项,…>] [--require-deployed|--require-deployed-all] [--out <目录>] [--work <目录>] [--pycache <目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--pre-a3-staging-parent <绝对目录，默认 $D/staging/pre-a3-scenarios>] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] [--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }
# 测试树与字节码缓存默认放在数据根之外、跨轮次固定的目录：Go 在不加 -trimpath 时按包所在目录做构建缓存的键，测试树
# 路径每轮都变的话，后端三组测试与 lint 每轮第一次都要冷编译整个 backend。记录（主体目录）仍按轮次放在 $RUNROOT 下。
# 单元执行记录库同样在数据根之外、跨轮次固定（承接要跨运行找记录）。
PROFILE=entry; OUT=""; WORK="$(dirname "$D")/entry-gates-work"; PYC=""; PRE_A3_SOURCE=auto; PRE_A3_STAGING_PARENT=""; MODE=re-execute; STORE="$(dirname "$D")/unit-records"; AUDIT_READS=false
AUDIT_STRICT=false; AUDIT_HOST_INPUTS=""; INPUT_SCOPE_ARGS=(); AUDIT_POLICY_ARGS=()
WITH_GATES=""; REQUIRE_DEPLOYED=false; WAIT_LOCK=0; REQUIRE_DEPLOYED_ALL=false; DEPLOY_OTHERS=""; DEPLOY_OTHERS_SOURCE=()
FULL_SET_REQUEST=""; MODE_EXPLICIT=false; FULL_SET_ARGS=(); GO_WARMUP=auto
PARALLEL_ARGS=()
UNIT_INHERIT_REQUEST=""; UNIT_INHERIT_STAGE=pre-ledger; UNIT_INHERIT_ARGS=(); UNIT_INHERIT_PROPOSE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --go-warmup) GO_WARMUP="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --parallel)
      if ! [[ "${2:-}" =~ ^[1-9][0-9]*$ ]]; then echo "--parallel 必须是正整数" >&2; exit 2; fi
      PARALLEL_ARGS=(--parallel "$2"); shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --full-set-request) FULL_SET_REQUEST="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --unit-inherit-request) UNIT_INHERIT_REQUEST="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --unit-inherit-stage) UNIT_INHERIT_STAGE="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --unit-inherit-propose) UNIT_INHERIT_PROPOSE="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --mode) MODE_EXPLICIT=true; MODE="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --record-store) STORE="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --audit-reads) AUDIT_READS=true; shift ;;
    --audit-strict) AUDIT_STRICT=true; shift ;;
    --audit-policy) AUDIT_POLICY_ARGS=(--audit-policy "${2:-}"); shift 2 || { usage; exit 2; } ;;
    --audit-host-inputs) AUDIT_HOST_INPUTS="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --input-scope) INPUT_SCOPE_ARGS+=(--input-scope "${2:-}"); shift 2 || { usage; exit 2; } ;;
    --with-gates) WITH_GATES="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --require-deployed) REQUIRE_DEPLOYED=true; shift ;;
    --require-deployed-all) REQUIRE_DEPLOYED=true; REQUIRE_DEPLOYED_ALL=true; shift ;;
    --wait-lock) WAIT_LOCK="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --policy-activation) POLICY_ACTIVATION="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --pre-a3-certification) PRE_A3_CERTIFICATION="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --pre-a3-mode) PRE_A3_SOURCE="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --pre-a3-staging-parent) PRE_A3_STAGING_PARENT="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --out) OUT="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --work) WORK="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --pycache) PYC="${2:-}"; shift 2 || { usage; exit 2; } ;;
    --) shift; break ;;
    -*) usage; exit 2 ;;
    *) break ;;
  esac
done
if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then usage; exit 2; fi
PBUNDLE="$1"; PBRANCH="$2"; PCOMMIT="$3"; NM_DIR="${4:-$HISTORY_TEST_TREE/frontend}"
case "$PROFILE" in entry|full-gates|preflight|pre-a3|regression) ;; *) echo "未知的门禁组合：$PROFILE" >&2; exit 2 ;; esac
if ! [[ "$WITH_GATES" =~ ^([a-z0-9-]+(,[a-z0-9-]+)*)?$ ]]; then echo "--with-gates 是逗号分隔的门禁项：$WITH_GATES" >&2; exit 2; fi
if ! [[ "$WAIT_LOCK" =~ ^[0-9]+$ ]]; then echo "--wait-lock 是秒数：$WAIT_LOCK" >&2; exit 2; fi
case "$GO_WARMUP" in auto|off) ;; *) echo "未知的 Go 预热模式：$GO_WARMUP" >&2; exit 2 ;; esac
case "$PRE_A3_SOURCE" in auto|present|run) ;; *) echo "未知的 pre-A3 来源：$PRE_A3_SOURCE" >&2; exit 2 ;; esac
case "$MODE" in full-set-pass|re-execute|unit-inherit) ;; *) echo "未知的模式：$MODE" >&2; exit 2 ;; esac
# B5a-08：逐单元承接请求与 B-09 全集请求互斥；只写承接提案时两种请求都不给。
if [ -n "$UNIT_INHERIT_REQUEST" ] && [ -n "$FULL_SET_REQUEST" ]; then echo "逐单元承接请求不能与 B-09 全集请求同用" >&2; exit 2; fi
if [ -n "$UNIT_INHERIT_PROPOSE" ]; then
  if [[ "$UNIT_INHERIT_PROPOSE" != /* ]] || [ -e "$UNIT_INHERIT_PROPOSE" ]; then echo "承接提案目录必须是尚不存在的绝对路径：$UNIT_INHERIT_PROPOSE" >&2; exit 2; fi
  if [ -n "$UNIT_INHERIT_REQUEST" ] || [ -n "$FULL_SET_REQUEST" ]; then echo "写承接提案时不给承接请求" >&2; exit 2; fi
  if [ "$MODE_EXPLICIT" = true ] && [ "$MODE" != unit-inherit ]; then echo "--unit-inherit-propose 只与 --mode unit-inherit 同用" >&2; exit 2; fi
  MODE=unit-inherit; REQUIRE_DEPLOYED=true
fi
# B-09：入口的逐单元旧承接模式不能绕过全集准入。无请求时始终重新执行。
if [ -n "$FULL_SET_REQUEST" ]; then
  REQUIRE_DEPLOYED=true
  if [ "$MODE_EXPLICIT" = false ]; then MODE=full-set-pass; fi
elif [ "$MODE" = full-set-pass ]; then
  echo "B-09：未提供全集承接请求，回退重新执行全集"
  MODE=re-execute
fi
# B5a-08：有逐单元承接请求而没指定模式时取 unit-inherit；--mode unit-inherit 没有请求时回退重新执行全集。
if [ -n "$UNIT_INHERIT_REQUEST" ]; then
  if [[ "$UNIT_INHERIT_REQUEST" != /* ]]; then echo "逐单元承接请求必须是绝对路径：$UNIT_INHERIT_REQUEST" >&2; exit 2; fi
  REQUIRE_DEPLOYED=true
  if [ "$MODE_EXPLICIT" = false ]; then MODE=unit-inherit; fi
  if [ "$MODE" != unit-inherit ]; then echo "--unit-inherit-request 只与 --mode unit-inherit 同用" >&2; exit 2; fi
elif [ "$MODE" = unit-inherit ] && [ -z "$UNIT_INHERIT_PROPOSE" ]; then
  echo "B5a-08：未提供逐单元承接请求，回退重新执行全集"
  MODE=re-execute
fi
if [ "$MODE" = unit-inherit ] && ! [[ "$UNIT_INHERIT_STAGE" =~ ^(pre-ledger|post-ledger|formal:[A-Za-z0-9][A-Za-z0-9._-]*)$ ]]; then
  echo "未知的入口阶段：$UNIT_INHERIT_STAGE" >&2; exit 2
fi
if [ "$AUDIT_READS" = true ] && [ "$MODE" != re-execute ] && [ "$MODE" != unit-inherit ]; then echo "读集审计要真跑才有读集：--audit-reads 只许与 --mode re-execute 同用" >&2; exit 2; fi
if { [ "$AUDIT_STRICT" = true ] || [ -n "$AUDIT_HOST_INPUTS" ] || [ "${#INPUT_SCOPE_ARGS[@]}" -gt 0 ] || [ "${#AUDIT_POLICY_ARGS[@]}" -gt 0 ]; } && { [ "$AUDIT_READS" != true ] || [ "$AUDIT_STRICT" != true ] || [ "$MODE" != re-execute ]; } && [ -z "$FULL_SET_REQUEST" ] && [ "$MODE" != unit-inherit ]; then
  echo "宿主输入与收窄提案必须使用 --audit-reads --audit-strict --mode re-execute" >&2; exit 2
fi
# B5a-08：逐单元承接可带读集合同（--audit-policy）与严格审计（要同时 --audit-reads）；宿主输入清单与收窄提案只许 re-execute。
if [ "$MODE" = unit-inherit ]; then
  if [ -n "$AUDIT_HOST_INPUTS" ] || [ "${#INPUT_SCOPE_ARGS[@]}" -gt 0 ]; then echo "逐单元承接不接受宿主输入清单与收窄提案（只许 --mode re-execute）" >&2; exit 2; fi
  if [ "$AUDIT_STRICT" = true ] && [ "$AUDIT_READS" != true ]; then echo "严格读集审计必须同时启用 --audit-reads" >&2; exit 2; fi
fi
for file in "$POLICY_ACTIVATION" "$PRE_A3_CERTIFICATION"; do
  if [[ "$file" != /* ]]; then echo "认证坐标必须是绝对路径：$file" >&2; exit 2; fi
done
if [[ "$PBUNDLE" != /* ]] || [ -L "$PBUNDLE" ] || [ ! -f "$PBUNDLE" ]; then echo "bundle 必须是已存在的普通文件（绝对路径）：$PBUNDLE" >&2; exit 2; fi
if ! [[ "$PBRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then echo "分支名只允许字母、数字与 ._/-：$PBRANCH" >&2; exit 2; fi
if ! [[ "$PCOMMIT" =~ ^[0-9a-f]{40}$ ]]; then echo "提交必须是完整 40 位小写 sha1：$PCOMMIT" >&2; exit 2; fi
for dir in "$NM_DIR" "$WORK" "$STORE" ${OUT:+"$OUT"} ${PYC:+"$PYC"}; do
  if [[ "$dir" != /* ]]; then echo "目录必须是绝对路径：$dir" >&2; exit 2; fi
done
TREE="$WORK/test-tree"; PYC="${PYC:-$WORK/pycache}"
on_error() {
  local rc=$? line="$1"
  trap - ERR
  echo "ENTRY_GATES_ABORTED：准备或执行失败（entry-gates.sh 第 ${line} 行，rc=${rc}），没有门禁结论；原因见上方输出，主体目录 ${OUT:-（未建立）}"
  exit 3
}
trap 'on_error $LINENO' ERR
private_directory() {
  # 已有合法目录不重复 chmod：Linux 上即使模式相同也可能推进 ctime，导致原来源快照失效。
  python3 -B - "$1" <<'PY'
import os, stat, sys
path = sys.argv[1]
os.makedirs(path, mode=0o700, exist_ok=True)
if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
    os.chmod(path, 0o700)
PY
}
private_directory "$WORK"
# 同一物理工作目录共用 mkdir/PID 锁，客户端与路径别名不能另起锁。常驻协调目录必须在来源快照绑定前准备好；
# 运行期仅在其 held 叶目录增删，不再改变 WORK 本身的目录项、mtime 或 ctime。
LOCK_BASE=$(python3 -B - "$WORK" <<'PY'
import hashlib, os, pathlib, sys
work = pathlib.Path(os.path.realpath(sys.argv[1]))
root = work.parent / ".entry-gates-locks" / hashlib.sha256(str(work).encode()).hexdigest()
if any(item.is_symlink() for item in (root, *root.parents)):
    raise ValueError("入口锁目录不得含符号链接")
print(root)
PY
)
private_directory "$(dirname "$LOCK_BASE")"
private_directory "$LOCK_BASE"
LOCK="$LOCK_BASE/held"
LEGACY_LOCK="$WORK/.entry-gates.lock"
LEGACY_OWNED=false
LOCK_WAITED=0
while ! mkdir "$LOCK" 2>/dev/null; do
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then
    if [ "$LOCK_WAITED" -lt "$WAIT_LOCK" ]; then sleep 10; LOCK_WAITED=$((LOCK_WAITED + 10)); continue; fi
    echo "ENTRY_GATES_ABORTED：已有入口门禁在运行（PID ${HOLDER}），拒绝并发"; exit 3
  fi
  rm -rf "$LOCK"
done
if [ "$LOCK_WAITED" -gt 0 ]; then echo "等入口门禁工作目录的锁 ${LOCK_WAITED} 秒"; fi
printf '%s\n' "$$" > "$LOCK/pid"
cleanup_entry_lock() {
  # 常驻兼容目录让旧版看见活 PID；退出只清空自己的 PID，不移除 WORK 内目录。旧版自行回收该目录时，
  # 必须按真实元数据变化令旧来源失效，不能宣称跨版本回收也保持来源不变。
  if [ "$LEGACY_OWNED" = true ] && [ "$(cat "$LEGACY_LOCK/pid" 2>/dev/null || true)" = "$$" ]; then
    : > "$LEGACY_LOCK/pid"
  fi
  rm -rf "$LOCK"
}
trap cleanup_entry_lock EXIT
while [ -e "$LEGACY_LOCK" ]; do
  if [ -L "$LEGACY_LOCK" ] || [ ! -d "$LEGACY_LOCK" ] || [ -L "$LEGACY_LOCK/pid" ]; then
    echo "ENTRY_GATES_ABORTED：旧入口锁必须是普通目录与 PID 文件，拒绝迁移"; exit 3
  fi
  HOLDER=$(cat "$LEGACY_LOCK/pid" 2>/dev/null || true)
  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then
    if [ "$LOCK_WAITED" -lt "$WAIT_LOCK" ]; then sleep 10; LOCK_WAITED=$((LOCK_WAITED + 10)); continue; fi
    echo "ENTRY_GATES_ABORTED：已有旧版入口门禁在运行（PID ${HOLDER}），拒绝迁移"; exit 3
  fi
  break
done
private_directory "$LEGACY_LOCK"
printf '%s\n' "$$" > "$LEGACY_LOCK/pid"
LEGACY_OWNED=true
if [ -z "$OUT" ]; then
  OUT="$RUNROOT/entry-gates/entry-gates-$(date -u +%Y%m%dt%H%M%Sz)"
  if [ -e "$OUT" ]; then OUT="$OUT-$$"; fi
fi
SUBJECT=$(basename "$OUT")
# 主体目录可以由调用方预先建好（VC-0 预跑先在里面写 gate_before 环境收据），但不能已有门禁记录（不覆盖上一次的结论）。
mkdir -p "$(dirname "$OUT")"; private_directory "$OUT"
if [ -e "$OUT/logs" ] || [ -e "$OUT/entry-gates.json" ]; then echo "ENTRY_GATES_ABORTED：主体目录里已有门禁记录：${OUT}"; exit 3; fi
mkdir -m 0700 "$OUT/logs"
echo "=== 入口门禁 ${SUBJECT}（组合 ${PROFILE}）$(utc_now)"
echo "bundle=${PBUNDLE} 分支=${PBRANCH} 提交=${PCOMMIT} 前端依赖=${NM_DIR}"
echo "=== 测试树（clone_test_tree，umask 022）$(utc_now)"
umask 022
REUSE_WORKSPACE=false
if { [ -n "$FULL_SET_REQUEST" ] || [ "$MODE" = unit-inherit ]; } && [ -d "$TREE" ]; then
  if python3 -B "$DRV/entry_gates.py" verify-scope-workspace --tree "$TREE" --bytecode-cache "$PYC" \
      --commit "$PCOMMIT" --bundle "$PBUNDLE" --branch "$PBRANCH" > "$OUT/workspace-check.json"; then
    REUSE_WORKSPACE=true
  elif [ "$MODE" = unit-inherit ] && [ "$CT_CLIENT" = claude ]; then
    # 后继只更新真实内容差异，保留宿主快照绑定的未改实物；失败不得偷偷重克隆扩大重跑范围。
    verify_history_tree "$TREE" || { echo "ENTRY_GATES_ABORTED：原测试树历史不合合同"; exit 3; }
    if python3 -B "$DRV/entry_gates.py" prepare-scope-successor --tree "$TREE" --bytecode-cache "$PYC" \
        --commit "$PCOMMIT" --bundle "$PBUNDLE" --branch "$PBRANCH" --lock-directory "$LOCK" --lock-owner "$$" \
        > "$OUT/workspace-successor.json"; then
      REUSE_WORKSPACE=true
    else
      echo "ENTRY_GATES_ABORTED：后继不满足原位内容更新条件；保留原树和缓存，不派发单元"; exit 3
    fi
  elif [ "$MODE" = unit-inherit ]; then
    echo "B5a-08：原工作区无法重放，重建测试树（登记了宿主快照的来源记录按实际输入判失效，只重跑这些单元）"
  else
    echo "B-09：原工作区无法重放，重建并由准入判定全量重跑"
  fi
fi
if [ "${#INPUT_SCOPE_ARGS[@]}" -gt 0 ] || [ "$REUSE_WORKSPACE" = true ]; then
  python3 -B "$DRV/entry_gates.py" verify-scope-workspace --tree "$TREE" --bytecode-cache "$PYC" \
    --commit "$PCOMMIT" --bundle "$PBUNDLE" --branch "$PBRANCH" | tee "$OUT/scope-workspace.json"
  verify_history_tree "$TREE" "$PCOMMIT"
else
  clone_test_tree "$TREE" "$PBUNDLE" "$PBRANCH" "$PCOMMIT"
fi
if [ ! -d "$NM_DIR/node_modules" ] || ! cmp -s "$NM_DIR/pnpm-lock.yaml" "$TREE/frontend/pnpm-lock.yaml"; then
  echo "前端依赖不可用：${NM_DIR}/node_modules 不存在，或 ${NM_DIR}/pnpm-lock.yaml 与本树 frontend/pnpm-lock.yaml 不同"
  echo "本轮 lockfile 有变化时，先按 frontend.sh 同一方式（node:20 容器内 pnpm install --frozen-lockfile）在独立目录装好依赖，再把该目录作为最后一个参数传入"
  echo "ENTRY_GATES_ABORTED：前端依赖不可用，没有门禁结论；主体目录 ${OUT}"
  exit 3
fi
if [ "${#INPUT_SCOPE_ARGS[@]}" -eq 0 ] && [ "$REUSE_WORKSPACE" = false ]; then
  cp -a "$NM_DIR/node_modules" "$TREE/frontend/node_modules"
elif [ ! -d "$TREE/frontend/node_modules" ]; then
  echo "收窄复验缺少原前端依赖，不能自动补装后沿用旧读集" >&2; exit 3
fi
umask 077
TREE_HEAD=$(git -C "$TREE" rev-parse HEAD)
# 命令替换里一律 `|| true`：set -E 会把 ERR 陷阱带进命令替换，head 截断触发的 SIGPIPE 不能误判为准备失败。
tree_status() { git --no-optional-locks -C "$TREE" status --porcelain --untracked-files=all 2>&1 | head -n "${1:-1000000}" || true; }
echo "test-tree HEAD=${TREE_HEAD} status=[$(tree_status)]"
echo "=== 树外字节码缓存 ${PYC} $(utc_now)"
# 执行器的固定 PATH（用途见下方执行环境一段）。预编译必须与执行器是同一个解释器：.pyc 文件名带解释器版本标签，
# 按调用方 PATH 预编译、执行器再按固定 PATH 解析出另一个 python3 时（如本机 Homebrew 3.14 与 /usr/local/bin 的 3.13），
# 执行器找不到缓存（UM-21）；只有一个 Python 的 ARM64 与 CI 碰不到。
EXEC_PATH=/usr/local/go/bin:/opt/node-v20/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
if [ "${#INPUT_SCOPE_ARGS[@]}" -eq 0 ] && [ "$REUSE_WORKSPACE" = false ]; then
  env -u PYTHONPATH PATH="$EXEC_PATH" python3 "$DRV/bytecode_cache.py" "$PYC" "$TREE/tools" | tail -n 1 | cut -c1-300
else
  echo "收窄复验保留原字节码缓存，由严格轨迹核对实际读取及快照；不重新预编译"
fi

# 部署绑定：数据根部署的必须就是本提交。含 pre-A3 的组合必做（入口门禁验的是测试树，pre-A3 跑的是数据根的受管树）；
# 其余组合带 --require-deployed 时也做（后台验证据此把结论绑定到这次部署，E4-01）。设置 DEPLOY（最新部署收据）。
# B5a-06：按本轮客户端（客户端表，lib.sh 给出 CT_ 变量）取部署收据、算测试树的整树摘要（Claude 按 v3 引擎算两根身份，
# 收据里的身份在 identity 对象下）、比部署一致性的各部分；Codex 的取值与调用和改造前相同。
check_deployed() {
  DEPLOY=$(python3 -B - "$DRV/../install.py" "$D" "$CT_CLIENT" <<'PYDEPLOY'
import importlib.util, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('installed_driver', sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
print(module.latest_deploy_receipt(Path(sys.argv[2])/'control', sys.argv[3])[0])
PYDEPLOY
)
  # 部署收据的整树摘要等于测试树受管树。
  TREE_FILES=$(cd "$TREE" && env -u CODEX_UPGRADE_IDENTITY_MEMO PYTHONPATH=. PYTHONPYCACHEPREFIX="$PYC" python3 -c \
    "import sys; from tools.official_client_capture import codex_upgrade_policy_certification as pc; print(pc.current_identity(*sys.argv[1:])['tool_files_sha256'])" \
    ${CT_IDENTITY_ARGS[@]+"${CT_IDENTITY_ARGS[@]}"})
  DEPLOY_FILES=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print((d[sys.argv[2]] if sys.argv[2] else d)['tool_files_sha256'])" \
    "$DEPLOY" "$CT_DEPLOY_RECEIPT_IDENTITY_KEY")
  if [ "$TREE_FILES" != "$DEPLOY_FILES" ]; then
    echo "数据根部署的受管树（${DEPLOY_FILES}）不是本提交（测试树受管树 ${TREE_FILES}）：先受监督部署本提交，再跑入口门禁"
    echo "ENTRY_GATES_ABORTED：部署与测试树不一致，没有门禁结论；主体目录 ${OUT}"
    exit 3
  fi
  # E2-05：整树摘要不含测试目录，也不含指南与部署脚本副本，只比它会放过「测试改了、还没重新部署」（10-02 实测数据根
  # 的测试比测试树旧两个文件，整树摘要却相等）。入口门禁在测试树验过的，必须就是数据根实际部署的那一份：受管
  # 整树（含测试，不计字节码）、两份指南、部署脚本副本逐项比内容摘要。
  if ! MISMATCH=$(python3 -B "$DRV/entry_steps.py" deploy-consistency --tree "$TREE" --data-root "$D" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"}); then
    echo "数据根与本提交不一致：${MISMATCH}（整树摘要相同也不行）：先受监督部署本提交并同步部署脚本副本，再跑入口门禁"
    echo "ENTRY_GATES_ABORTED：部署与测试树不一致，没有门禁结论；主体目录 ${OUT}"
    exit 3
  fi
}
# B5a-10：--require-deployed-all 时本轮客户端之外每个已纳管客户端也核对（见文件头），核对过的收据记进总摘要来源。
check_deployed_others() {
  if ! DEPLOY_OTHERS=$(python3 -B "$DRV/entry_steps.py" deploy-check --tree "$TREE" --data-root "$D" --except "$CT_CLIENT" --pycache "$PYC"); then
    echo "其余已纳管客户端的部署不是本提交：${DEPLOY_OTHERS}：先受监督部署本提交（交换共享根的部署带上全部已纳管客户端），再跑入口门禁"
    echo "ENTRY_GATES_ABORTED：部署与测试树不一致，没有门禁结论；主体目录 ${OUT}"
    exit 3
  fi
  DEPLOY_OTHERS_SOURCE=(--source "deploy_receipts_others=$DEPLOY_OTHERS")
}
PRE_A3_MODE=none; PRE_A3_ARGS=(); PA3_PARENT=""; DEPLOY=""
RECORDED_BRIDGE=false
if [ -n "${CLAUDE_ENTRY_FULL79_SPEC:-}${CLAUDE_ENTRY_FULL79_SPEC_SHA256:-}" ]; then
  if [ "$CT_CLIENT" != claude ] || [ -z "${CLAUDE_ENTRY_FULL79_SPEC:-}" ] || [ -z "${CLAUDE_ENTRY_FULL79_SPEC_SHA256:-}" ]; then
    echo "ENTRY_GATES_ABORTED：完整79接线只允许Claude，文件与外持SHA必须成对提供" >&2; exit 3
  fi
  RECORDED_BRIDGE=true
fi
verify_recorded_full79() {
  local args=(--spec "$CLAUDE_ENTRY_FULL79_SPEC" --sha256 "$CLAUDE_ENTRY_FULL79_SPEC_SHA256") name
  for name in ${CT_PRE_A3_FIXTURE_KEYS[@]+"${CT_PRE_A3_FIXTURE_KEYS[@]}"}; do
    args+=(--pre-a3-env "$name=${!name:-}")
  done
  if [ "$#" -gt 0 ]; then args+=(--certification "$1" --tree "$TREE" --data-root "$D"); fi
  ( cd "$D" && PYTHONPATH="$D" python3 -B "$DRV/entry_gates.py" recorded-full79-inputs "${args[@]}" ) > "$OUT/recorded-full79-inputs.json"
}
# B5a-04：pre-A3 认证模块的 plan／find-reusable／issue／record-reuse 与策略认证按本轮客户端（参数 CLIENT，缺省 codex）。
# B5a-06 起模块名与追加的客户端参数取客户端表（CT_PRE_A3_MODULE、CT_POLICY_CERTIFICATION_MODULE、CT_CLIENT_ARGV）：Codex
# 的客户端参数为空，命令行与改造前逐字相同；Claude 带 --client claude（场景单元 ID 前缀、身份、部署收据与认证合同随之按
# 客户端，记录库里互不承接）。客户端参数追加在每条命令末尾（驱动测试按字面核对命令前缀）。
if [ "$PROFILE" != entry ] && [ "$PROFILE" != pre-a3 ] && [ "$REQUIRE_DEPLOYED" = true ]; then
  echo "=== 部署绑定 $(utc_now)"
  check_deployed
  echo "数据根部署的就是本提交：部署收据 ${DEPLOY}"
  if [ "$REQUIRE_DEPLOYED_ALL" = true ]; then
    check_deployed_others
    echo "其余已纳管客户端的部署也是本提交：${DEPLOY_OTHERS:-（没有其余已纳管客户端）}"
  fi
fi
if [ "$PROFILE" = entry ] || [ "$PROFILE" = pre-a3 ]; then
  echo "=== pre-A3 认证：部署绑定与沿用判断 $(utc_now)"
  check_deployed
  # 激活认证缺失时才签（先补兼容收据）；编排器传入现成的激活认证时这里什么都不写。
  if [ ! -f "$POLICY_ACTIVATION" ]; then
    [ -f "$POLICY_COMPAT_RECEIPT" ] || python3 -m "$CT_POLICY_CERTIFICATION_MODULE" compatibility --previous-policy "${PREVIOUS_POLICY:?缺少前序策略文件}" --output "$POLICY_COMPAT_RECEIPT" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} | cut -c1-160
    python3 -m "$CT_POLICY_CERTIFICATION_MODULE" activation --deployment-receipt "$DEPLOY" --compatibility-receipt "$POLICY_COMPAT_RECEIPT" --output "$POLICY_ACTIVATION" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} | cut -c1-160
  fi
  # E3-02：场景父目录固定，单元命令只带稳定内容（入口门禁据此承接已通过的场景），本次各场景的临时根在父目录下新建；
  # 数据根才有、或不保证与测试树一致的输入（冻结台账、录制数据、alpine 镜像）由门禁清单生成时在数据根算好。
  plan_pre_a3_run() {
    PRE_A3_MODE=run
    # 正式规划的场景父目录：未给时为本轮数据根下的固定父目录；给出时须是绝对路径，三链记录根由它派生（CLAUDE_RECORDED_CHAIN_RECORD_DIR）。
    PRE_A3_PARENT="${PRE_A3_STAGING_PARENT:-$D/staging/pre-a3-scenarios}"
    if [[ "$PRE_A3_PARENT" != /* ]]; then echo "ENTRY_GATES_ABORTED：--pre-a3-staging-parent 必须是绝对路径：${PRE_A3_PARENT}" >&2; exit 3; fi
    UNITS="$OUT/pre-a3-units.json"
    PA3_PARENT=$(python3 -m "$CT_PRE_A3_MODULE" plan --staging-parent "$PRE_A3_PARENT" --output "$UNITS" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["staging_parent"])')
    PRE_A3_ARGS=(--pre-a3-units "$UNITS" --pre-a3-data-root "$D" --pre-a3-env "PYTHONPATH=." --pre-a3-env "CODEX_UPGRADE_IDENTITY_MEMO=$CODEX_UPGRADE_IDENTITY_MEMO")
    # 工程历史来源只从本轮安全参数文件取固定白名单；干净 EXEC_ENV 不继承任意 CLAUDE_*。
    # 清单生产器同时登记路径与内容摘要，缺来源提前失败；Codex 的表为空，原参数数组不变。
    for name in ${CT_PRE_A3_FIXTURE_KEYS[@]+"${CT_PRE_A3_FIXTURE_KEYS[@]}"}; do
      if [ -z "${!name:-}" ]; then echo "ENTRY_GATES_ABORTED：pre-A3 缺少本轮显式来源 ${name}" >&2; exit 3; fi
      PRE_A3_ARGS+=(--pre-a3-env "$name=${!name}")
    done
    if [ "$RECORDED_BRIDGE" = true ]; then
      if [ "${#AUDIT_POLICY_ARGS[@]}" -eq 0 ]; then
        echo "ENTRY_GATES_ABORTED：固定79运行必须显式提供冻结audit-policy，不能改写其它单元承接策略" >&2; exit 3
      fi
      verify_recorded_full79
      PRE_A3_ARGS+=(--recorded-full79-spec "$CLAUDE_ENTRY_FULL79_SPEC" --recorded-full79-spec-sha256 "$CLAUDE_ENTRY_FULL79_SPEC_SHA256")
      # 固定79没有运行时闭合读集，只生成真实重跑记录；严格执行器不得把它登记为可承接。
      AUDIT_READS=true; AUDIT_STRICT=true
    fi
    if [ -d "$PYC_MANAGED" ]; then PRE_A3_ARGS+=(--pre-a3-env "PYTHONPYCACHEPREFIX=$PYC_MANAGED"); fi
    echo "pre-A3 场景纳入本次运行：场景父目录 ${PA3_PARENT}"
  }
  if [ "$PRE_A3_SOURCE" = run ]; then
    if [ -e "$PRE_A3_CERTIFICATION" ]; then echo "ENTRY_GATES_ABORTED：--pre-a3-mode run 要求认证坐标空着（重做换新坐标）：${PRE_A3_CERTIFICATION}"; exit 3; fi
    plan_pre_a3_run
  elif [ -f "$PRE_A3_CERTIFICATION" ]; then
    # 已有的本轮认证先按 stage1 同一口径复核（无效即在跑门禁之前停下，不白跑一遍）。
    python3 -m "$CT_PRE_A3_MODULE" find-reusable --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} >/dev/null
    if [ "$RECORDED_BRIDGE" = true ]; then verify_recorded_full79 "$PRE_A3_CERTIFICATION"; fi
    PRE_A3_MODE=present; echo "PRE_A3_PRESENT $PRE_A3_CERTIFICATION（不纳入本次运行）"
  elif [ "$PRE_A3_SOURCE" = present ]; then
    echo "ENTRY_GATES_ABORTED：--pre-a3-mode present 要求认证已在：${PRE_A3_CERTIFICATION}"; exit 3
  elif REUSE=$(python3 -m "$CT_PRE_A3_MODULE" find-reusable --search-root "$(dirname "$PRE_A3_CERTIFICATION")" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"}); then
    if [ "$RECORDED_BRIDGE" = true ]; then verify_recorded_full79 "$REUSE"; fi
    cp -p "$REUSE" "$PRE_A3_CERTIFICATION"; chmod 600 "$PRE_A3_CERTIFICATION"
    PRE_A3_MODE=reused; echo "PRE_A3_REUSED $REUSE（不纳入本次运行）"
  else
    plan_pre_a3_run
  fi
fi
PLAN_PROFILE="$PROFILE"
if [ "$PROFILE" = entry ] && [ "$PRE_A3_MODE" != run ]; then PLAN_PROFILE=full-gates; fi
# pre-a3 组合在沿用已有认证时没有要执行的单元：不生成门禁清单、不跑执行器，直接到下面的复核与复用登记。
RUN_GATES=true
if [ "$PROFILE" = pre-a3 ] && [ "$PRE_A3_MODE" != run ]; then RUN_GATES=false; fi
LAUNCHER='["unshare","-m","--propagation","private","bash","-c","mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec \"$@\"","entry-gates"]'
ISOLATION="测试树单元：unshare -m --propagation private，只读 tmpfs 遮住 /root/oauth-capture（生产别名），树外只读字节码缓存；pre-A3 场景：数据根生产布局"
TS="$TREE/frontend/node_modules/typescript/lib/typescript.js"
# 白名单环境（E3-01）：门禁清单生成与执行器只看见这份环境，单元继承的就是它，执行器把它整份算进环境指纹（只除去
# 决定缓存位置的字节码前缀）。本轮参数文件导出的键不放行：测试树单元本就不该读它们（也就碰不到生产数据根坐标），
# 放进来的话每换一次认证坐标环境指纹就变、什么都承接不了。
# 与调用方无关（E4-01 验收实测）：PATH 与语言用固定值，USER、LOGNAME、SHELL、LANGUAGE、LC_* 不放行。操作员 shell、修好接着跑、
# 后台验证、入口空跑（env -i）几种入口的这些变量各不相同，lib.sh 每被 source 一次还会把 go 与 node 目录再往 PATH 前面加一遍；
# 原来照搬调用方的值，同一棵测试树在不同入口下跑出的记录互相承接不了。EXEC_PATH 在上面预编译前已定义。
EXEC_ENV=(env -i "PATH=$EXEC_PATH" LANG=C.UTF-8)
for name in HOME TZ TMPDIR \
    http_proxy https_proxy no_proxy all_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY SSL_CERT_FILE SSL_CERT_DIR; do
  if [ -n "${!name+x}" ]; then EXEC_ENV+=("$name=${!name}"); fi
done
for name in $(compgen -e); do
  case "$name" in GO*|CGO_*|DOCKER_*|NODE_*|PNPM_*|npm_config_*|COREPACK_*) EXEC_ENV+=("$name=${!name}") ;; esac
done
EXEC_ENV+=(PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 "PYTHONPYCACHEPREFIX=$PYC" "CODEX_0_149_1_SOURCE_ROOT=$HISTORICAL_SOURCE_ROOT" "CAPTURE_TYPESCRIPT_MODULE=$TS")
# C-02：三组后端测试共用一次生产包预编译；每个工作根独立缓存，承接入口不改动来源缓存。
GO_WARMUP_ACTIVE=false
if [ "$GO_WARMUP" = auto ] && [ -z "$FULL_SET_REQUEST" ] && [ "$MODE" != unit-inherit ] && { [ "$PLAN_PROFILE" = entry ] || [ "$PLAN_PROFILE" = full-gates ]; }; then
  GO_WARMUP_ACTIVE=true
  EXEC_ENV+=("GOCACHE=${GOCACHE:-$WORK/go-build-cache}" "GOMODCACHE=${GOMODCACHE:-$WORK/go-mod-cache}" "GOTMPDIR=${GOTMPDIR:-$WORK/go-tmp}")
fi
GATES_STATUS=passed
if [ "$RUN_GATES" = true ]; then
private_directory "$STORE"
echo "=== 门禁清单（${PLAN_PROFILE}）$(utc_now)"
( cd "$TREE" && "${EXEC_ENV[@]}" python3 -B "$DRV/entry_gates.py" plan --tree "$TREE" --profile "$PLAN_PROFILE" --launcher-json "$LAUNCHER" \
    --typescript-module "$TS" --historical-source-root "$HISTORICAL_SOURCE_ROOT" ${PRE_A3_ARGS[@]+"${PRE_A3_ARGS[@]}"} \
    --with-gates "$WITH_GATES" ${INPUT_SCOPE_ARGS[@]+"${INPUT_SCOPE_ARGS[@]}"} ${AUDIT_POLICY_ARGS[@]+"${AUDIT_POLICY_ARGS[@]}"} --output "$OUT/gates-manifest.json" ) | cut -c1-400
if [ "$GO_WARMUP_ACTIVE" = true ]; then
  ( cd "$TREE" && "${EXEC_ENV[@]}" python3 -B "$DRV/go_compile_warmup.py" --tree "$TREE" --work "$WORK" \
      --manifest "$OUT/gates-manifest.json" --output "$OUT/go-compile-warmup.json" \
      --plan-output "$OUT/go-compile-warmup-plan.json" --launcher-json "$LAUNCHER" )
  # 独立准备记录纳入本轮总耗时；调度器处理整机预约和子进程，不把编译写成门禁通过。
  ( cd "$TREE" && "${EXEC_ENV[@]}" python3 -B "$DRV/unit_executor.py" run-commands \
      --manifest "$OUT/go-compile-warmup-plan.json" --out-dir "$OUT/go-compile-warmup-executor" \
      --shared-caches off ) > "$OUT/go-compile-warmup-executor.log" 2>&1 < /dev/null \
      || { echo "ENTRY_GATES_ABORTED：Go 预热失败，正式门禁尚未派发；日志 $OUT/go-compile-warmup-executor.log"; exit 3; }
fi
echo "=== 一次运行：统一调度执行器 run-gates（模式 ${MODE}，记录库 ${STORE}，记录 ${OUT}/executor）$(utc_now)"
EXEC_RC=0
AUDIT_ARGS=()
if [ "$AUDIT_READS" = true ]; then AUDIT_ARGS=(--audit-reads --audit-data-root "$D"); echo "读集检查：登记覆盖的单元严格取证；未登记项仅在显式重跑策略下执行（数据根 ${D}）"; fi
if [ "$AUDIT_STRICT" = true ]; then AUDIT_ARGS+=(--audit-strict); fi
if [ -n "$AUDIT_HOST_INPUTS" ]; then AUDIT_ARGS+=(--audit-host-inputs "$AUDIT_HOST_INPUTS"); fi
if [ -n "$FULL_SET_REQUEST" ]; then
  FULL_SET_ARGS=(--full-set-request "$FULL_SET_REQUEST" --full-set-deployment "$DEPLOY")
fi
# B5a-08：承接上下文要本轮客户端、入口阶段与 control 目录（执行器在每个复核点现算部署收据集合）；写提案时执行器只判定。
if [ "$MODE" = unit-inherit ]; then
  UNIT_INHERIT_ARGS=(--unit-inherit-client "$CT_CLIENT" --unit-inherit-stage "$UNIT_INHERIT_STAGE" --unit-inherit-control "$D/control")
  if [ -n "$UNIT_INHERIT_PROPOSE" ]; then
    UNIT_INHERIT_ARGS+=(--decide-only --unit-inherit-propose "$UNIT_INHERIT_PROPOSE")
  else
    UNIT_INHERIT_ARGS+=(--unit-inherit-request "$UNIT_INHERIT_REQUEST")
  fi
fi
( cd "$TREE" && "${EXEC_ENV[@]}" python3 "$DRV/unit_executor.py" run-gates --manifest "$OUT/gates-manifest.json" --out-dir "$OUT/executor" \
    --mode "$MODE" --record-store "$STORE" ${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"} ${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"} ${FULL_SET_ARGS[@]+"${FULL_SET_ARGS[@]}"} ${UNIT_INHERIT_ARGS[@]+"${UNIT_INHERIT_ARGS[@]}"} ) > "$OUT/executor.log" 2>&1 < /dev/null || EXEC_RC=$?
tail -n 25 "$OUT/executor.log" | cut -c1-240
if [ -n "$UNIT_INHERIT_PROPOSE" ]; then
  if [ "$EXEC_RC" -ne 0 ] || [ ! -f "$UNIT_INHERIT_PROPOSE/binding.json" ]; then
    echo "ENTRY_GATES_ABORTED：承接提案没写出（rc=${EXEC_RC}）；日志 ${OUT}/executor.log"; exit 3
  fi
  echo "UNIT_INHERIT_PROPOSAL ${UNIT_INHERIT_PROPOSE}：binding.json 的 binding_sha256 交两类批准签字（测试树与缓存保留在 ${WORK}）"
  exit 0
fi
if [ "$EXEC_RC" -gt 1 ] || [ ! -f "$OUT/executor/summary.json" ]; then
  echo "ENTRY_GATES_ABORTED：执行器出错（rc=${EXEC_RC}），没有门禁结论；日志 ${OUT}/executor.log"
  exit 3
fi
echo "=== 导出门禁记录、P0 证据与预跑／全量门禁记录 $(utc_now)"
python3 -B "$DRV/entry_gates.py" export --manifest "$OUT/gates-manifest.json" --summary "$OUT/executor/summary.json" --out "$OUT" \
  --subject "$SUBJECT" --round "$ROUND" --target-version "${TARGET_VERSION:-}" --tree "$TREE" --isolation "$ISOLATION" \
  --host "$(hostname -s)" --architecture "$(python3 -c 'import platform; print(platform.system().lower() + "/" + platform.machine())')" \
  --bytecode-cache "$PYC" --source "bundle=$PBUNDLE" --source "branch=$PBRANCH" --source "commit=$PCOMMIT" --source "tree_head=$TREE_HEAD" \
  --source "history_test_tree=$HISTORY_TEST_TREE" --source "node_modules_source=$NM_DIR/node_modules" --source "pre_a3=$PRE_A3_MODE" \
  --source "deploy_receipt=$DEPLOY" ${DEPLOY_OTHERS_SOURCE[@]+"${DEPLOY_OTHERS_SOURCE[@]}"} | cut -c1-600
GATES_STATUS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['status'])" "$OUT/entry-gates.json")
if [ "$PROFILE" = regression ]; then
  # 定向回归选出了哪些测试单元；一个都没选出时提示人工补门禁项（改动不在任何测试单元的静态依赖闭包里）。
  python3 -B - "$OUT/executor/unit-manifest.json" "$WITH_GATES" <<'PYREG'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
executed = [unit["unit_id"] for unit in manifest["units"] if unit["disposition"] == "executed" and unit.get("unit_type") == "test"]
print(f"定向回归：执行 {len(executed)} 个测试单元（静态依赖闭包里有改动文件的）{executed[:8]}，承接 {manifest['counts']['inherited']} 个；"
      f"人工补的门禁项：{sys.argv[2] or '无'}")
if not executed and not sys.argv[2]:
    print("提示：这次改动没有落在任何测试单元的静态依赖闭包里。改动若在后端、前端、文档或出站规格，用 --with-gates 人工补门禁项"
          "（如 backend-go-test、frontend-critical、check-egress-spec）；全量门禁由后台验证跑")
PYREG
fi
else
  echo "=== 本次没有要执行的单元（pre-A3 认证 ${PRE_A3_MODE}）$(utc_now)"
fi
PRE_A3_OK=true
if [ "$PRE_A3_MODE" = run ]; then
  # E3-02：从本次运行的清单与记录库组装 v2 认证（每个场景恰好一条通过的正式执行记录，本次执行或承接；网络计数为 0）。
  # 清单只在执行器自检通过后才发布进记录库，没发布的运行签不出认证。
  echo "=== pre-A3 认证签发（从单元执行记录组装，核对场景全集与网络计数）$(utc_now)"
  if ! python3 -m "$CT_PRE_A3_MODULE" issue --staging-root "$PA3_PARENT" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} \
      --unit-manifest "$OUT/executor/unit-manifest.json" --record-store "$STORE" \
      --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" --output "$PRE_A3_CERTIFICATION" | cut -c1-300; then
    PRE_A3_OK=false
  fi
fi
if [ "$PRE_A3_MODE" != none ] && [ "$PRE_A3_OK" = true ]; then
  # 与 stage1 建账本前同一口径复核本轮坐标；跨部署复用登记复用收据（同一组绑定不重复写）。
  python3 -m "$CT_PRE_A3_MODULE" find-reusable --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} >/dev/null
  if [ "$RECORDED_BRIDGE" = true ]; then
    verify_recorded_full79 "$PRE_A3_CERTIFICATION"
    # 保存原消费端的实际输出；编排器只据它登记真实收据，失败时不会凭已有认证写passed。
    python3 -m "$CT_PRE_A3_MODULE" record-reuse --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" --receipt-root "$(dirname "$PRE_A3_CERTIFICATION")" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} | tee "$OUT/pre-a3-reuse.json" | cut -c1-300
  else
    python3 -m "$CT_PRE_A3_MODULE" record-reuse --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" --receipt-root "$(dirname "$PRE_A3_CERTIFICATION")" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"} | cut -c1-300
  fi
  echo "PRE_A3_DONE $PRE_A3_CERTIFICATION（${PRE_A3_MODE}）"
fi
AFTER_STATUS=$(tree_status 5)
trap - ERR
if [ "$GATES_STATUS" = passed ] && [ "$PRE_A3_OK" = true ] && [ -z "$AFTER_STATUS" ]; then
  if [ "$AUDIT_STRICT" = true ] || [ -n "$FULL_SET_REQUEST" ] || [ "$MODE" = unit-inherit ]; then
    echo "入口门禁通过：总摘要 ${OUT}/entry-gates.json；保留严格审计的测试树与缓存供后续复核 ${WORK}"
  else
    rm -rf "$TREE" "$PYC"
    echo "入口门禁通过：总摘要 ${OUT}/entry-gates.json；测试树与字节码缓存已删除"
  fi
  echo "ENTRY_GATES_DONE rc=0 subject=${SUBJECT} out=${OUT}"
  exit 0
fi
echo "入口门禁未通过：$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(' '.join(g['gate_id'] for g in d['gates'] if g['status'] != 'passed') or '（门禁项都通过）')" "$OUT/entry-gates.json")"
if [ "$PRE_A3_OK" != true ]; then echo "pre-A3 认证没有签发：没通过的认证只写旁路文件 $(dirname "$PRE_A3_CERTIFICATION")/*.failed-*.json，修好后同一 STAMP 直接重跑"; fi
if [ -n "$AFTER_STATUS" ]; then echo "门禁后测试树不干净：[${AFTER_STATUS}]"; fi
grep -E "^(单元未通过|诊断重跑|测试组 .*全集核对失败)" "$OUT/executor.log" | head -n 20 | cut -c1-240 || true
echo "  总摘要 ${OUT}/entry-gates.json；执行器日志 ${OUT}/executor.log；测试树保留在 ${TREE}（排查用，下次运行会重建）"
echo "ENTRY_GATES_FAILED subject=${SUBJECT} out=${OUT}"
exit 1
