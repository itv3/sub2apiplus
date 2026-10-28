#!/bin/bash
# VC-1～VC-6 无人值守流水线（采集主机，随驱动安装在 <驱动目录>/pipeline/vc-pipeline.sh）。
# 轮次、版本、分支、账号、路径全部来自本轮参数文件 ARM64_VC_ENV 与可选的 pipeline.env（见 pipeline-lib.sh、README），脚本里没有任何写死的版本。
#
# 阶段顺序（官方顺序，BGV 夹在 VC-4 与 VC-5 之间）：VC1 → VC2 → VC3 → VC4 → BGV → VC5 → VC6
#   VC1  等 VC-0 收口（entry.sh）派发的首批跑完，必要时转入 VC-1 封存（预览→批准），核对 vc-1-checkpoint 与账本重放
#   VC2  人工分类审核产物就绪后，vc2/vc23-seq.sh（批次序号可变版的 vc23.sh：VC-2 离线判据门、批准、VC-3 stage-profile）
#   VC3  核对 VC-3 暂存收据与 checkpoint，自动写 PROFILE_DIGEST（同源核对）
#   VC4  候选链交接（本机产出 bundle）→ 从 bundle 推出 C／DC／RECEIPT 写 env → 构建网络批准 → arm64-vc4-gates.sh → vc4-all.sh
#   BGV  按入口提交重跑后台验证（background-validate.sh start --no-dryrun），通过后删测试树并 housekeeping
#   VC5  COMPOSE_BACKUP → 准入（dry-run→批准件→apply→consume）→ vc5-all.sh；采集账号只在 vc5-all 打出 VC5_ALL_DONE 之后才关回
#   VC6  按 <PREP>/vc6/ORDER.txt 顺序执行 vc6/ 下的 VC-6 脚本（需本机提交的步骤等交接标记）
#
# 用法（采集主机 root；ARM64_VC_ENV 与 PIPELINE_ENV 经环境变量给出，见 pipeline.env.example）：
#   bash vc-pipeline.sh --check [--from S] [--to S]        只检查：逐阶段列命令、输入、缺什么；不写任何文件
#   bash vc-pipeline.sh start [--from S] [--to S] [--approver '批准人及依据'] [--wait-handoff 分钟] [--bgv-args '…']
#                                                            前台自检通过后 setsid -f 后台起运行体；状态写 <PREP>/pipeline/vc-status.txt
#   （--run 是运行体，不要手工执行）
# 硬规则：
#   * 失败一律写 STOPPED <阶段> :: <原因> 然后停（退出码 1）；不自动延期、不改采集账号以外的任何状态；
#   * 自动重试只用于“可证明幂等”的步骤（只读重放、准入 dry-run、采集账号开关、后台验证 preflight），
#     有次数上限并逐次写 RETRY 行；占批次序号、发真实请求、写批准件／账本的步骤一律不重试（清单见 README“重试清单”）；
#     两个耗时的门禁步骤（arm64-vc4-gates、vc4-all）默认不重试，需在 pipeline.env 里显式打开；
#   * 最后一行必为 EXIT rc=N（监视器据此判终）；
#   * 全程不用 nohup（SigIgn 第 1 位非零就拒绝）；
#   * 批准类动作（构建网络、VC-5 准入）只在调度者起跑时传了 --approver 才签，文本由调度者负责；
#   * rm 只用字面路径。
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 2; }
pl_defaults
STATUS=${PDIR}/vc-status.txt
ALL_STAGES=(VC1 VC2 VC3 VC4 BGV VC5 VC6)
ZERO_DIGEST=$(printf '%064d' 0)   # 占位摘要（全 0），不在源码里写 64 位字面量
# 本阶段预计下限（分钟，计时账本剩余不足就拒绝起跑；缺省值取自上一版真实计时账本，可在 pipeline.env 用 PIPE_NEED_<阶段> 覆盖）
declare -A NEED=( [VC1]=${PIPE_NEED_VC1:-90} [VC2]=${PIPE_NEED_VC2:-45} [VC3]=${PIPE_NEED_VC3:-5} [VC4]=${PIPE_NEED_VC4:-160} [BGV]=${PIPE_NEED_BGV:-120} [VC5]=${PIPE_NEED_VC5:-300} [VC6]=${PIPE_NEED_VC6:-60} )
declare -A PHASE_OF=( [VC1]=VC-1 [VC2]=VC-2 [VC3]=VC-3 [VC4]=VC-4 [BGV]=VC-5 [VC5]=VC-5 [VC6]=VC-6 )

MODE=start; FROM=VC1; TO=VC6; APPROVER=""; WAIT_HANDOFF=0; BGV_ARGS="--no-dryrun"; ATTACH_VC5=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    start) MODE=start ;;
    --run) MODE=run ;;
    --from) FROM=${2:-}; shift ;;
    --to) TO=${2:-}; shift ;;
    --approver) APPROVER=${2:-}; shift ;;
    --wait-handoff) WAIT_HANDOFF=${2:-0}; shift ;;
    --bgv-args) BGV_ARGS=${2:-}; shift ;;
    --attach-vc5) ATTACH_VC5=1 ;;   # VC5 接管已在跑的 vc5-all（恢复链之后手工起的），跳过准入／开采集账号／起 vc5-all
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done
idx_of() { local i; for i in "${!ALL_STAGES[@]}"; do [ "${ALL_STAGES[$i]}" = "$1" ] && { echo "$i"; return 0; }; done; return 1; }
FI=$(idx_of "${FROM}") || { echo "--from 阶段名不对：${FROM}（可选 ${ALL_STAGES[*]}）"; exit 2; }
TI=$(idx_of "${TO}") || { echo "--to 阶段名不对：${TO}"; exit 2; }
[ "${FI}" -le "${TI}" ] || { echo "--from 必须不晚于 --to"; exit 2; }
SELECTED=("${ALL_STAGES[@]:${FI}:$((TI - FI + 1))}")

# ---------------------------------------------------------------- 公共函数
utc() { date -u +%FT%TZ; }
log() { echo "$(utc) $*" >> "${STATUS}"; echo "$(utc) $*"; }
disk_pct() { df / | awk 'NR==2{gsub("%","",$5); print $5}'; }
disk_gib() { df -BG / | awk 'NR==2{gsub("G","",$4); print $4}'; }
disk_txt() { df -h / | awk 'NR==2{print $5" 已用，可用 "$4}'; }
sigign_ok() { pl_sigign_ok; }
load_env() { pl_load_round_env && pl_defaults; }
# 采集账号开关（账号号来自 pipeline.env 的 CAPTURE_ACCOUNT_ID 或本轮参数文件的采集账号）。capture-account-on／off 脚本文档保证幂等，
# 退出码 3＝另一实例在跑、5＝管理接口失败、6＝改完核对不符，这三种可重试；2＝账号身份对不上、4＝访问令牌快过期是确定性拒绝，不重试。
CAPTURE_STATE=unknown
capture_on()  { pl_retry "capture-account on" "3,5" bash "${PIPE_HOME}/capture-account-on.sh" >> "${PDIR}/capture-account.out" 2>&1 < /dev/null; local rc=$?; log "CAPTURE on rc=${rc}"; [ ${rc} -eq 0 ] && CAPTURE_STATE=on;  return ${rc}; }
capture_off() { pl_retry "capture-account off" "3,5,6" bash "${PIPE_HOME}/capture-account-off.sh" >> "${PDIR}/capture-account.out" 2>&1 < /dev/null; local rc=$?; log "CAPTURE off rc=${rc}"; [ ${rc} -eq 0 ] && CAPTURE_STATE=off; return ${rc}; }
on_exit() {
  local rc=$?
  if [ "${MODE}" = run ]; then
    [ "${CAPTURE_STATE}" = on ] && log "NOTE 采集账号仍是开启状态（本脚本没替你关）：确认后执行 bash ${PIPE_HOME}/capture-account-off.sh"
    log "EXIT rc=${rc} | 根盘 $(disk_txt)"
    rmdir "${PREP}/vc-pipeline.lock" 2>/dev/null
  fi
}
stop() { log "STOPPED $1 :: $2"; exit 1; }

# 同一时间只允许一个编排在跑（本阶段自己要起的除外）。$1 = 阶段，VC1 阶段允许首批批次在跑。
others_running() {
  pgrep -af '[e]ntry_orchestrator.py|[b]ackground_validation.py (start|run|reuse)|[e]ntry-gates.sh|[c]hain-rehearsal|[a]rm64-vc4-gates.sh|[v]c4-all.sh|[v]c5-all.sh|[v]c23(-seq)?\.sh|[e]ntry-dryrun.sh start|[s]tart-entry.sh' | grep -v 'vc-pipeline.sh' || true
}
disk_guard() { # $1 阶段 $2 停线百分比（缺省 69）
  local limit=${2:-69}
  if [ "$(disk_pct)" -le "${limit}" ] && [ "$(disk_gib)" -ge 40 ]; then return 0; fi
  log "DISKWARN $1 根盘 $(disk_txt)，超线 ${limit}%，先跑 housekeeping"
  (cd "${DRV}" && bash entry-dryrun.sh housekeeping > "${PDIR}/housekeeping-guard-$1.out" 2>&1)
  if [ "$(disk_pct)" -le "${limit}" ] && [ "$(disk_gib)" -ge 40 ]; then log "DISKWARN $1 housekeeping 后恢复：$(disk_txt)"; return 0; fi
  stop "$1" "根盘 $(disk_txt) 做完 housekeeping 仍超过停线 ${limit}%/40G，按清盘规则由调度者处理（本脚本不自己删别的东西）"
}
housekeeping() { log "STEP housekeeping start"; (cd "${DRV}" && bash entry-dryrun.sh housekeeping > "${PDIR}/housekeeping-$1.out" 2>&1); log "STEP housekeeping end rc=$? | 根盘 $(disk_txt)"; }

# 计时账本里的剩余时间：输出 "phase 执行层剩余分钟 总截止剩余分钟 项目截止剩余分钟 暂停范围"
deadline_info() {
  ( cd "${D}" && PYTHONPATH="${D}" python3 -B - "${NEWDIR}" <<'PY'
import datetime, sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade_vc_artifacts as a
d = a.effective_deadlines(Path(sys.argv[1]))
now = datetime.datetime.now(datetime.timezone.utc)
def mins(v):
    t = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return int((t - now).total_seconds() // 60)
print(d["phase"], mins(d["execution_deadline_at_utc"]), mins(d["total_deadline_at_utc"]), mins(d["project_deadline_at_utc"]), ",".join(d.get("paused_scopes") or []) or "-")
PY
  )
}
# 阶段截止检查：不够就 STOPPED（交调度者 deadline-extend，本脚本不自动延期）。检查模式 CHECKING=1 时只打印不退出。
stage_time_check() { # $1 阶段
  local info ph ex tot proj paused rem need=${NEED[$1]}
  info=$(deadline_info 2>/dev/null) || { [ "${CHECKING:-0}" = 1 ] && { echo "无法读计时账本"; return 2; }; stop "$1" "读不到计时账本（effective_deadlines 失败）"; }
  read -r ph ex tot proj paused <<<"${info}"
  rem=${tot}; [ "${proj}" -lt "${rem}" ] && rem=${proj}
  if [ "${ph}" = "${PHASE_OF[$1]}" ] && [ "${ex}" -lt "${rem}" ]; then rem=${ex}; fi
  TIME_TXT="账本阶段=${ph} 本阶段所需下限=${need}分 可用剩余=${rem}分（执行层 ${ex}／总 ${tot}／项目 ${proj}，暂停范围 ${paused}）"
  if [ "${paused}" != "-" ] || [ "${rem}" -lt "${need}" ]; then
    [ "${CHECKING:-0}" = 1 ] && return 1
    stop "$1" "计时账本剩余不足或已暂停：${TIME_TXT}；请调度者按 DL-vc0-extend.md 第 5 节 deadline-extend，本脚本不自动延期"
  fi
  return 0
}

# 等标记：$1 文件 $2 成功正则 $3 失败正则 $4 总秒数 $5 标签 $6 进程 pgrep 模式（进程消失且无标记 → 返回 2）
wait_marker() {
  local f=$1 ok=$2 bad=$3 max=$4 label=$5 pat=$6 t0 last miss=0
  t0=$(date +%s); last=${t0}
  while [ $(( $(date +%s) - t0 )) -lt "${max}" ]; do
    if [ -f "${f}" ]; then
      grep -Eq "${ok}" "${f}" && return 0
      [ -n "${bad}" ] && grep -Eq "${bad}" "${f}" && return 3
    fi
    if [ -n "${pat}" ]; then
      if pgrep -f "${pat}" >/dev/null 2>&1; then miss=0; else miss=$((miss + 1)); fi
      if [ "${miss}" -ge 3 ]; then
        # 进程消失后再读一次标记，避免刚好写完就退出
        [ -f "${f}" ] && grep -Eq "${ok}" "${f}" && return 0
        return 2
      fi
    fi
    if [ $(( $(date +%s) - last )) -ge 1200 ]; then last=$(date +%s); log "WAIT ${label} | 根盘 $(disk_txt)"; fi
    sleep 30
  done
  return 1
}
# 等一份 JSON 结论到终态（后台验证）：打印终态；进程消失而非终态打印 orphaned；超时打印 timeout
wait_json() {
  python3 -B - "$1" "$2" "$3" "${STATUS}" <<'PY'
import json, os, subprocess, sys, time
res, limit, label, status_file = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
t0 = last = time.time(); peak = 0
live = ("running", "starting", "queued", None)
def used():
    row = subprocess.run(["df", "/"], capture_output=True, text=True).stdout.splitlines()[1].split()
    return int(row[4].rstrip("%")), row[3]
while time.time() - t0 < limit:
    pct, avail = used(); peak = max(peak, pct)
    try:
        data = json.load(open(res))
    except Exception:
        time.sleep(20); continue
    state = data.get("status")
    if state not in live:
        print(state)
        open(status_file, "a").write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + f" WAITEND {label} {state} 根盘峰值(采样)={peak}%\n")
        raise SystemExit(0)
    pid = data.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            time.sleep(45)
            try: data = json.load(open(res))
            except Exception: pass
            print(data.get("status") if data.get("status") not in live else "orphaned"); raise SystemExit(0)
        except PermissionError:
            pass
    if time.time() - last >= 1200:
        last = time.time()
        open(status_file, "a").write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + f" WAIT {label} {state} | 根盘 {pct}% 可用 {avail} 峰值 {peak}%\n")
    time.sleep(30)
print("timeout")
PY
}
json_field() { # 输出文件里最后一个 JSON 对象的字段
  python3 -B - "$1" "$2" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read(); found = None
for line in text.splitlines():
    line = line.strip()
    if line.startswith("{") and line.endswith("}"):
        try: found = json.loads(line)
        except ValueError: pass
if found is None:
    try: found = json.loads(text)
    except ValueError: found = {}
v = (found or {}).get(sys.argv[2], "")
print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
PY
}
# 等交接标记文件（本机/调度者产物）：WAIT_HANDOFF 分钟内每 30 秒查一次；0 表示只查一次。
wait_handoff() { # $1 阶段 $2 文件 $3 说明
  local end=$(( $(date +%s) + WAIT_HANDOFF * 60 )) last=0
  while true; do
    [ -f "$2" ] && return 0
    [ "$(date +%s)" -ge "${end}" ] && return 1
    if [ $(( $(date +%s) - last )) -ge 1200 ]; then last=$(date +%s); log "WAIT $1 等交接：$3（$2）"; fi
    sleep 30
  done
}
vc1_awaiting_attempt() { # 恰好一个 awaiting_receipts 的 attempt 目录（VC-1 封存的对象）
  python3 -B -c "
import json, sys
from pathlib import Path
ready = [p.parent for p in sorted(Path(sys.argv[1]).glob('*/attempt.json')) if json.load(open(p)).get('status') == 'awaiting_receipts']
if len(ready) != 1:
    raise SystemExit(1)
print(ready[0])" "${NEWDIR}/official/attempts"
}
passed() { grep -q " PASS $1\$" "${STATUS}" 2>/dev/null; }
ckpt() { ls "${NEWDIR}/control/vc/vc-$1-checkpoint.json" 2>/dev/null | head -1; }
# 官方只读重放：round_context 能重放 Formal+账本（--mode campaign）
replay_once() { (cd "${D}" && python3 -B "${DRV}/round_context.py" --env "${ARM64_VC_ENV}" --mode campaign >/dev/null 2>&1); }
replay_ok() { pl_retry "round_context 只读重放" "" replay_once; }   # 只读，可证明幂等
prev_passed() { # $1 阶段：前一阶段通过 = 状态文件里有 PASS 行，或（--from 续跑时）现场产物能证明
  case "$1" in
    VC1) [ -d "${NEWDIR}" ] && { [ -n "$(ckpt 0)" ] || [ -f "${RR}/entry-steps/vc0-closeout.json" ]; } ;;
    VC2) [ -n "$(ckpt 1)" ] ;;
    VC3) [ -n "$(ckpt 2)" ] ;;
    VC4) [ -n "$(ckpt 3)" ] && [ -f "${D}/control/${CAMPAIGN_PREFIX}-vc3-candidate-catalog-${ROUND}-${STAMP}/catalog-stage-receipt.json" ] ;;
    BGV) [ -n "$(ckpt 4)" ] ;;
    VC5) bash "${DRV}/background-validate.sh" require-passed >/dev/null 2>&1 ;;
    VC6) [ -n "$(ckpt 5)" ] && [ -f "${RR}/vc5-admission.json" ] ;;
  esac
}
need_prev() { # $1 阶段 → 上一阶段确已通过，否则 STOPPED
  prev_passed "$1" || stop "$1" "上一阶段尚未通过（现场产物不能证明；检查 vc-status.txt 与对应 checkpoint），不起跑"
}
common_start_checks() { # $1 阶段 $2 是否允许首批在跑（VC1）
  sigign_ok || stop "$1" "本脚本继承了忽略 SIGHUP（nohup 起的），test_campaign_exec_session_hangup 必红；改用 setsid -f"
  local o; o=$(others_running)
  if [ -n "${o}" ] && [ "${2:-0}" != 1 ]; then stop "$1" "机器上有别的编排在跑：$(echo "${o}" | head -2 | cut -c1-160)"; fi
  [ "${2:-0}" = 1 ] && { echo "${o}" | grep -q 'entry_orchestrator\|start-entry' && stop "$1" "入口编排器仍在跑（VC-0 未收口）"; }
  disk_guard "$1"
  stage_time_check "$1"
}

# ---------------------------------------------------------------- 各阶段
# 官方采集登录态访问令牌剩余有效期必须覆盖整个 VC-1（缺省阈值 PIPE_TOKEN_MIN_MINUTES=150 分钟＝预计时长 + 1 小时）。
# 正常情况下 start-entry.sh 起跑前已经保证过；这里是第二道：VC-1 首批若还没开始跑（没有 campaign-run／vc-batch 进程）就当场直连刷新，
# 首批已在跑时不在半路碰登录态（刷新会改写 auth.json），只能明确报错停下由人处置。刷新失败一律明确报错停下。
token_guard() {
  local left min=${PIPE_TOKEN_MIN_MINUTES:-150}
  left=$(bash "${PIPE_HOME}/preflight-refresh.sh" --remaining 2>/dev/null) || stop VC1 "读不出官方采集登录态访问令牌的剩余有效期（preflight-refresh.sh --remaining 失败），不猜；人工核对 auth.json"
  if [ "${left}" -ge "${min}" ]; then log "STEP VC1 官方采集登录态访问令牌剩余 ${left} 分钟 ≥ 阈值 ${min} 分钟"; return 0; fi
  if pgrep -f '[c]ampaign-run|[c]ompile-and-run-vc-batch|[v]c-batch.sh' >/dev/null 2>&1; then
    stop VC1 "官方采集登录态访问令牌只剩 ${left} 分钟 < 阈值 ${min} 分钟，而 VC-1 首批已在跑，不在半路刷新；处置：等首批结束后评估，或由人直连刷新（${PIPE_HOME}/preflight-refresh.sh --ensure）"
  fi
  log "STEP VC1 官方采集登录态访问令牌只剩 ${left} 分钟 < 阈值 ${min} 分钟，VC-1 首批尚未开始，先直连刷新一次"
  bash "${PIPE_HOME}/preflight-refresh.sh" --ensure --min "${min}" >> "${PDIR}/preflight-refresh.out" 2>&1 \
    || stop VC1 "直连刷新官方采集登录态失败（详情 ${PDIR}/preflight-refresh.out）；VC-1 官方作业会在令牌过期前后碰中继刷新得到 403，不起跑"
  log "STEP VC1 直连刷新完成"
}
run_VC1() {
  need_prev VC1; common_start_checks VC1 1
  log "STAGE VC1 等 VC-0 收口派发的首批跑完 | ${TIME_TXT} | 根盘 $(disk_txt)"
  token_guard
  local budget=$(( 150 * 60 ))
  wait_marker "${NEWDIR}/control/vc/vc-1-checkpoint.json" '.' '' "${budget}" vc1 '[c]ampaign-run|[c]ompile-and-run-vc-batch|[v]c-batch.sh'
  local rc=$?
  # Codex 的 VC-1 封存没有驱动脚本，首批（capture-official run）跑完后 attempt 停在 awaiting_receipts，
  # 要派发“封存预览”“封存批准”两个批次才有 vc-1-checkpoint，这里用 ${PIPE_HOME}/vc2/vc1-seal.sh；批准是治理动作，需 --approver。
  if [ ${rc} -ne 0 ] && [ -z "$(ckpt 1)" ]; then
    local att_dir; att_dir=$(vc1_awaiting_attempt 2>/dev/null) || att_dir=""
    if [ -n "${att_dir}" ]; then
      log "STEP VC1 首批已跑完、attempt 处于 awaiting_receipts（${att_dir##*/}）：转入 VC-1 封存（预览 → 批准）"
      if ! ls "${att_dir}"/seal-preview*.json >/dev/null 2>&1; then
        bash "${PIPE_HOME}/vc2/vc1-seal.sh" preview >> "${PDIR}/vc1-seal.out" 2>&1 < /dev/null || stop VC1 "封存预览批次失败，见 ${PDIR}/vc1-seal.out；失败后的恢复走 recovery-plan.sh，本脚本不自动重试"
        log "STEP VC1 封存预览已生成（${PDIR}/vc1-seal.out 末尾有 review_sha256 与 scan_summary）"
      fi
      [ -n "${APPROVER}" ] || stop VC1 "待调度者：封存预览已就绪（${att_dir}/seal-preview.json 的 scan_summary 要核对）；核对后手工 bash ${PIPE_HOME}/vc2/vc1-seal.sh approve --yes，或带 --approver '依据' 重跑 --from VC1"
      log "STEP VC1 封存批准（依据：${APPROVER}）"
      bash "${PIPE_HOME}/vc2/vc1-seal.sh" approve --yes >> "${PDIR}/vc1-seal.out" 2>&1 < /dev/null || stop VC1 "封存批准批次失败，见 ${PDIR}/vc1-seal.out"
      [ -n "$(ckpt 1)" ] && rc=0
    fi
  fi
  [ ${rc} -eq 0 ] || stop VC1 "首批未产出 vc-1-checkpoint（rc=${rc}：1=超时 2=首批进程已消失）；先 bash ${DRV}/recovery-plan.sh <参数文件>（只读预览）看恢复计划；官方首批恢复走 entry.sh --approve-sha256"
  replay_ok || stop VC1 "vc-1-checkpoint 已有，但 round_context --mode campaign 重放失败"
  log "PASS VC1"
}
run_VC2() {
  need_prev VC2; common_start_checks VC2
  [ -d "${D}/backend/internal/officialegress/catalogdata/runtime/profiles/${BASELINE_VERSION}" ] \
    || stop VC2 "数据根 backend 快照里没有 Active ${BASELINE_VERSION} 画像目录：prepare-profile／stage-profile 用 go run（cwd=数据根 backend），须先把数据根 backend 换成集成分支的 backend（先例：备份成 backend.pre-<提交>-<日期> 再换，可用 sync-data-root-backend.sh），本脚本不自己换"
  local rm ts mk
  rm=${INPUT_RULE_MIGRATION}; ts=${INPUT_TARGET_SNAPSHOT}; mk=${W}/VC2-INPUTS-REVIEWED
  wait_handoff VC2 "${mk}" "VC-2 人工分类审核产物（rule-migration-approved.json、target-snapshot-approved.json）及审核标记" \
    || stop VC2 "待调度者：人工分类审核产物与标记 ${mk} 未就绪（标记 JSON 含两文件 sha256，见 VCPREP.md 第 3 节）"
  python3 -B - "${mk}" "${rm}" "${ts}" <<'PY' || stop VC2 "审核标记与两份输入的 sha256 不一致或文件缺失（审核后又改过？）"
import hashlib, json, sys
mk, rm, ts = sys.argv[1:]
m = json.load(open(mk))
for key, path in (("rule_migration_sha256", rm), ("target_snapshot_sha256", ts)):
    assert m.get(key) == hashlib.sha256(open(path, "rb").read()).hexdigest(), key
assert m.get("reviewed_by")
PY
  log "STAGE VC2 vc23-seq.sh（VC-2 批次 SEQ0～SEQ0+3 + VC-3 stage-profile 批次 SEQ0+4） | ${TIME_TXT} | 根盘 $(disk_txt)"
  # 驱动 vc23.sh 把批次序号写死成 2～6，只适用于 VC-1 只用批次 1；Codex 的 VC-1 封存还要两个批次，所以改用批次序号可变的 vc23-seq.sh（机械改写自 vc23.sh）
  local seq0; seq0=$(python3 -B -c "import glob,os,sys; xs=[int(os.path.basename(p).split('-')[0]) for p in glob.glob(sys.argv[1]+'/control/vc/batches/*.json')]; print(max(xs)+1 if xs else 1)" "${NEWDIR}")
  log "STEP vc23-seq SEQ0=${seq0}（下一个可用批次序号）"
  ( cd "${DRV}" && SEQ0=${seq0} setsid -f bash "${PIPE_HOME}/vc2/vc23-seq.sh" > "${RR}/vc23.out" 2>&1 < /dev/null )
  wait_marker "${RR}/vc23.out" 'VC23_DONE' 'Traceback|拒绝|失败|rc=[1-9]|断言|Error' $(( 120 * 60 )) vc23 '[v]c23(-seq)?\.sh'
  local rc=$?
  [ ${rc} -eq 0 ] || stop VC2 "vc23.sh 未以 VC23_DONE 结束（rc=${rc}：1=超时 2=进程已消失 3=命中失败字样），见 ${RR}/vc23.out；不自动重试（批次序号已占用，按指南对账后重派）"
  [ -n "$(ckpt 2)" ] || stop VC2 "vc23.sh 结束但没有 vc-2-checkpoint.json"
  log "PASS VC2"
}
run_VC3() {
  need_prev VC3; sigign_ok || stop VC3 "SigIgn"; stage_time_check VC3
  local rcpt=${D}/control/${CAMPAIGN_PREFIX}-vc3-candidate-catalog-${ROUND}-${STAMP}/catalog-stage-receipt.json
  [ -f "${rcpt}" ] && [ -n "$(ckpt 3)" ] || stop VC3 "缺 catalog-stage-receipt.json 或 vc-3-checkpoint.json：vc23.sh 在 VC-3 stage-profile（批次 6）处中断？按指南对账后重派批次 6（action-plan-vc3-stage-profile.json 在 ${W}），本脚本不自动重派"
  log "STAGE VC3 核对暂存收据并写 PROFILE_DIGEST"
  local dig pid
  read -r dig pid < <(python3 -B - "${rcpt}" "${W}/profile.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1])); p = json.load(open(sys.argv[2]))
d = r.get("target_profile_digest")
assert isinstance(d, str) and len(d) == 64 and d == p.get("profile_digest"), ("暂存收据与已批准 profile.json 的画像摘要不一致", d, p.get("profile_digest"))
assert r.get("campaign_id"), r
print(d, p["profile_id"])
PY
  ) || stop VC3 "暂存收据 target_profile_digest 与 ${W}/profile.json 的 profile_digest 不一致（或读不出）"
  [ "${pid}" = "${TARGET_PROFILE_ID}" ] || log "NOTE VC-2 批准的 profile_id=${pid} 与 env 的 TARGET_PROFILE_ID=${TARGET_PROFILE_ID} 不同，同步改 env（PROFILE_ID 在 env 里引用 TARGET_PROFILE_ID）"
  local keys=("PROFILE_DIGEST=${dig}")
  [ "${pid}" != "${TARGET_PROFILE_ID}" ] && keys+=("TARGET_PROFILE_ID=${pid}")
  bash "${PIPE_HOME}/set-env-keys.sh" "${ARM64_VC_ENV}" "${keys[@]}" >> "${PDIR}/set-env.out" 2>&1 || stop VC3 "set-env-keys 失败：$(tail -1 "${PDIR}/set-env.out")"
  load_env || stop VC3 "改 env 后 parse_env 重载失败"
  [ "${PROFILE_DIGEST}" = "${dig}" ] || stop VC3 "改 env 后 PROFILE_DIGEST 未生效"
  log "STEP env PROFILE_DIGEST=${dig:0:16}… 已写入 env.sh（驱动准入另会从 VC-3 收据独立推导有效值，两者须一致）"
  log "PASS VC3"
}
# 从 VC-4 交接的 bundle 推出 C／DC／RECEIPT（C=DC 的父、RECEIPT=C..DC 唯一改动文件）。结果放全局 D_DC D_C D_RECEIPT。
derive_chain() {
  local S=/var/tmp/vcp-chain-scratch
  rm -rf /var/tmp/vcp-chain-scratch
  git init -q --bare "${S}" || return 1
  echo "${HISTORY_TEST_TREE}/.git/objects" > "${S}/objects/info/alternates"
  git --git-dir="${S}" bundle verify "${BUNDLE}" >/dev/null 2>&1 || { echo "bundle verify 失败（前置提交不在历史测试树里？）"; rm -rf /var/tmp/vcp-chain-scratch; return 1; }
  git --git-dir="${S}" fetch -q "${BUNDLE}" "${BUNDLE_BRANCH}:refs/heads/vc4" 2>/dev/null || { echo "bundle 里没有分支 ${BUNDLE_BRANCH}"; rm -rf /var/tmp/vcp-chain-scratch; return 1; }
  D_DC=$(git --git-dir="${S}" rev-parse refs/heads/vc4)
  D_C=$(git --git-dir="${S}" rev-parse refs/heads/vc4^)
  local files; files=$(git --git-dir="${S}" diff --name-only "${D_C}" "${D_DC}")
  rm -rf /var/tmp/vcp-chain-scratch
  [ "$(echo "${files}" | wc -l | tr -d " ")" = 1 ] && echo "${files}" | grep -Eq '^docs/egress/maintenance/[A-Za-z0-9._-]+\.json$' || { echo "C..DC 的改动必须恰好是 docs/egress/maintenance/ 下一个 JSON，实际 [${files}]"; return 1; }
  D_RECEIPT=${files}
  [ "${D_DC}" != "${ENTRY_COMMIT}" ] && [ "${D_C}" != "${ENTRY_COMMIT}" ] || { echo "推出的 C／DC 等于入口提交（占位），交接的 bundle 不对"; return 1; }
}
vc4_network_approval() { # 构建网络专项批准（host）；需要 --approver
  if grep -q '^VC4_BUILD_NETWORK=' "${ARM64_VC_ENV}" && grep -q '^VC4_BUILD_NETWORK_APPROVAL=' "${ARM64_VC_ENV}"; then
    # 批准件绑定候选提交与 bundle：换过候选链后旧批准件失效（vc4-all 前提合同会因此拒绝），先核对绑定再沿用，不一致就按当前 intent 重签
    if ( cd "${D}" && python3 -B "${DRV}/vc4_contract.py" network-check >/dev/null 2>&1 ); then log "STEP 构建网络批准已在 env 里且与当前候选绑定一致，沿用"; return 0; fi
    log "STEP env 里的构建网络批准与当前候选绑定不一致（换过候选链），按当前 network-intent 重签"
  fi
  [ -n "${APPROVER}" ] || stop VC4 "待调度者：构建网络专项批准需要起跑时传 --approver '批准人及依据'（历次升级都因 R15 不放行 BuildKit RUN 容器，要 host 网络）"
  cp -p "${ARM64_VC_ENV}" "${ARM64_VC_ENV}.pre-build-network-$(date -u +%Y%m%dt%H%M%Sz)"
  python3 -B - "${ARM64_VC_ENV}" <<'PY' || stop VC4 "env 里 VC4_BUILD_NETWORK 行改写失败"
import re, sys
p = sys.argv[1]; t = open(p).read().split("\n")
for key, val in (("VC4_BUILD_NETWORK", "host"),):
    hits = [i for i, l in enumerate(t) if re.match(r"#?\s*" + key + "=", l)]
    assert len(hits) == 1, (key, hits)
    t[hits[0]] = f"{key}={val}"
open(p, "w").write("\n".join(t))
PY
  load_env || stop VC4 "VC4_BUILD_NETWORK 改写后 parse_env 失败"
  local intent=${RR}/build-network-intent.json approval=${RR}/build-network-approval-$(date -u +%Y%m%dt%H%M%Sz).json
  ( cd "${D}" && python3 -B "${DRV}/vc4_contract.py" network-intent > "${intent}" ) || stop VC4 "network-intent 失败"
  APPROVER="${APPROVER}" python3 -B - "${intent}" "${approval}" <<'PY' || stop VC4 "写构建网络批准件失败"
import datetime, json, os, sys
intent = json.load(open(sys.argv[1]))
assert intent["status"] == "requires_manual_approval" and intent["binding"]["network_mode"] == "host", intent
now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0); fmt = "%Y-%m-%dT%H:%M:%SZ"
value = {"schema_version": "arm64-vc4-build-network-approval/v1", "status": "approved", "binding": intent["binding"],
         "approved_by": os.environ["APPROVER"], "approved_at_utc": now.strftime(fmt),
         "expires_at_utc": (now + datetime.timedelta(days=7)).strftime(fmt)}
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as s:
    s.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
PY
  # 已有批准行就原地替换（重签时追加会出现两行同名键，parse_env 拒绝），没有才追加
  python3 -B - "${ARM64_VC_ENV}" "${approval}" <<'PY' || stop VC4 "env 里 VC4_BUILD_NETWORK_APPROVAL 行写入失败"
import sys
p, new = sys.argv[1], sys.argv[2]
t = open(p, encoding="utf-8").read().split("\n")
hits = [i for i, l in enumerate(t) if l.startswith("VC4_BUILD_NETWORK_APPROVAL=")]
assert len(hits) <= 1, hits
if hits:
    t[hits[0]] = "VC4_BUILD_NETWORK_APPROVAL=" + new
else:
    t.insert(len(t) - 1 if t and t[-1] == "" else len(t), "VC4_BUILD_NETWORK_APPROVAL=" + new)
open(p, "w", encoding="utf-8").write("\n".join(t))
PY
  load_env || stop VC4 "写入批准路径后 parse_env 失败"
  ( cd "${D}" && python3 -B "${DRV}/vc4_contract.py" network-check >/dev/null ) || stop VC4 "network-check 不通过"
  log "STEP 构建网络批准已写（host）：${approval}"
}
run_VC4() {
  need_prev VC4; common_start_checks VC4
  local hf=${RR}/handoff/vc4-chain.json
  wait_handoff VC4 "${hf}" "本机候选链 bundle 已上传到 \$BUNDLE 并写交接文件（本机 vc4-handoff.sh）" \
    || stop VC4 "待调度者：本机候选链（local-candidate-chain.sh）产出的 bundle 未交接（${hf}）"
  [ -f "${BUNDLE}" ] || stop VC4 "交接文件在但 ${BUNDLE} 不存在"
  local sha; sha=$(sha256sum "${BUNDLE}" | cut -c1-64)
  python3 -B - "${hf}" "${sha}" "${BUNDLE_BRANCH}" <<'PY' || stop VC4 "交接文件与 \$BUNDLE 的 sha256／分支不符"
import json, sys
h = json.load(open(sys.argv[1]))
assert h.get("bundle_sha256") == sys.argv[2] and h.get("branch") == sys.argv[3], h
PY
  derive_chain > "${PDIR}/derive-chain.out" 2>&1 || stop VC4 "从 bundle 推 C／DC／RECEIPT 失败：$(tail -1 "${PDIR}/derive-chain.out")"
  python3 -B -c "import json,sys; h=json.load(open(sys.argv[1])); assert h.get('dc') in (None, sys.argv[2]), '交接文件声明的 DC 与 bundle 分支头不一致'" "${hf}" "${D_DC}" || stop VC4 "交接文件声明的 DC 与 bundle 分支头不一致"
  log "STAGE VC4 候选链 C=${D_C:0:9} DC=${D_DC:0:9} RECEIPT=${D_RECEIPT} | ${TIME_TXT} | 根盘 $(disk_txt)"
  bash "${PIPE_HOME}/set-env-keys.sh" "${ARM64_VC_ENV}" "C=${D_C}" "DC=${D_DC}" "RECEIPT=${D_RECEIPT}" >> "${PDIR}/set-env.out" 2>&1 || stop VC4 "set-env-keys C/DC/RECEIPT 失败：$(tail -1 "${PDIR}/set-env.out")"
  load_env || stop VC4 "parse_env 重载失败"
  [ "${C}" = "${D_C}" ] && [ "${DC}" = "${D_DC}" ] || stop VC4 "env 里的 C／DC 未按推出值生效"
  log "STEP env C／DC／RECEIPT 已写入并通过 parse_env"
  vc4_network_approval
  # 门禁沿用：本 C／DC 最近一次 ARM64 版 VC-4 门禁已通过（summary.json status=passed、两项门禁 0、ready）且实现日志已交付（impl-logs/READY），
  # 就不重跑门禁、直接进 vc4-all（门禁 2 小时已过后 vc4-all 因构建网络批准绑定旧候选而停时，修好续跑不应再跑一遍门禁）
  local gates_ok=""
  gates_ok=$(python3 -B - "${RR}/arm64-vc4-gates" "${C}" "${DC}" <<'PY'
import json, sys
from pathlib import Path
root, c, dc = sys.argv[1:]
for s in sorted(Path(root).glob("arm64-vc4-gates-*/summary.json"), reverse=True):
    d = json.load(open(s, encoding="utf-8"))
    if d.get("candidate_commit") == c and d.get("executed_on_commit") == dc:
        gates = d.get("gates") or {}
        if d.get("status") == "passed" and d.get("ready") is True and gates and all(v == 0 for v in gates.values()):
            print(s.parent.name)
        break
PY
) || gates_ok=""
  if [ -n "${gates_ok}" ] && [ -f "${RR}/impl-logs/READY" ]; then
    log "STEP arm64-vc4-gates 本 C／DC 已通过（${gates_ok}），沿用，不重跑"
  else
  # ARM64 版 VC-4 门禁：check-egress-spec + make test（setsid -f，约 2h10m；成功会自己删 wt-D／wt-C 测试树）
  disk_guard VC4
  : > "${RR}/arm64-vc4-gates.out"
  ( cd "${DRV}" && setsid -f bash arm64-vc4-gates.sh > "${RR}/arm64-vc4-gates.out" 2>&1 < /dev/null )
  log "STEP arm64-vc4-gates 已起"
  wait_marker "${RR}/arm64-vc4-gates.out" 'ARM64_VC4_GATES_DONE rc=0' 'ARM64_VC4_GATES_FAILED|ARM64_VC4_GATES_ABORTED' $(( 240 * 60 )) vc4-gates '[a]rm64-vc4-gates.sh'
  local rc=$?
  # 门禁整套重跑是幂等的（每次从干净测试树起），但一轮要 2 小时以上，真缺陷会白烧；默认不重试，PIPE_RETRY_VC4_GATES=1 才在“进程消失／门禁失败”后补跑一次
  if [ ${rc} -ne 0 ] && [ ${rc} -ne 1 ] && [ "${PIPE_RETRY_VC4_GATES:-0}" = 1 ]; then
    log "RETRY arm64-vc4-gates 第 1 次失败 rc=${rc}，按 PIPE_RETRY_VC4_GATES=1 补跑一次（先复核计时账本与根盘）"
    stage_time_check VC4; disk_guard VC4
    : > "${RR}/arm64-vc4-gates.out"
    ( cd "${DRV}" && setsid -f bash arm64-vc4-gates.sh > "${RR}/arm64-vc4-gates.out" 2>&1 < /dev/null )
    wait_marker "${RR}/arm64-vc4-gates.out" 'ARM64_VC4_GATES_DONE rc=0' 'ARM64_VC4_GATES_FAILED|ARM64_VC4_GATES_ABORTED' $(( 240 * 60 )) vc4-gates '[a]rm64-vc4-gates.sh'
    rc=$?
  fi
  [ ${rc} -eq 0 ] || stop VC4 "arm64-vc4-gates 未通过（rc=${rc}：1=超时 2=进程消失 3=门禁失败/中止）；结论与日志 ${RR}/arm64-vc4-gates/ ，测试树保留供取证"
  # 成功后脚本自己会删测试树，这里只补核对（字面路径）
  [ -d "${RR}/arm64-vc4-gates/wt-D" ] && rm -rf "${RR}/arm64-vc4-gates/wt-D"
  [ -d "${RR}/arm64-vc4-gates/wt-C" ] && rm -rf "${RR}/arm64-vc4-gates/wt-C"
  housekeeping vc4-gates
  fi
  disk_guard VC4
  : > "${RR}/vc4-all.out"
  ( cd "${DRV}" && setsid -f bash vc4-all.sh > "${RR}/vc4-all.out" 2>&1 < /dev/null )
  log "STEP vc4-all 已起"
  wait_marker "${RR}/vc4-all.out" 'VC4_ALL_DONE' 'Traceback|拒绝|失败|BATCH FAILED' $(( 240 * 60 )) vc4-all '[v]c4-all.sh'
  rc=$?
  # vc4-all 文档写明重入同一命令即可续，但失败多为确定性（批准绑定、前提合同），默认不重试；PIPE_RETRY_VC4_ALL=1 才在“失败字样”（rc=3）后重入一次
  if [ ${rc} -eq 3 ] && [ "${PIPE_RETRY_VC4_ALL:-0}" = 1 ]; then
    log "RETRY vc4-all 第 1 次失败 rc=${rc}，按 PIPE_RETRY_VC4_ALL=1 重入同一命令一次"
    disk_guard VC4
    ( cd "${DRV}" && mv -f "${RR}/vc4-all.out" "${RR}/vc4-all.out.attempt1" 2>/dev/null; setsid -f bash vc4-all.sh > "${RR}/vc4-all.out" 2>&1 < /dev/null )
    wait_marker "${RR}/vc4-all.out" 'VC4_ALL_DONE' 'Traceback|拒绝|失败|BATCH FAILED' $(( 240 * 60 )) vc4-all '[v]c4-all.sh'
    rc=$?
  fi
  [ ${rc} -eq 0 ] || stop VC4 "vc4-all 未以 VC4_ALL_DONE 结束（rc=${rc}）；重入同一命令即可续（上传中断用 --resume-from upload-wait），本脚本不自动重试；日志 ${RR}/vc4-all.out"
  # 构建收据由 Campaign 写在 $NEWDIR/candidates/$CAND/ 下（vc4.sh 自己也读这里），不在构建目录 $B（按 $B 核对会误判停下）
  [ -n "$(ckpt 4)" ] && [ -f "${NEWDIR}/candidates/${CAND}/build-receipt.json" ] || stop VC4 "vc4-all 结束但缺 vc-4-checkpoint 或 build-receipt.json"
  replay_ok || stop VC4 "VC-4 后 round_context 重放失败"
  housekeeping vc4
  log "PASS VC4"
}
run_BGV() {
  need_prev BGV; common_start_checks BGV
  log "STAGE BGV 按 ${ENTRY_COMMIT:0:9} 重跑后台验证（${BGV_ARGS}）| ${TIME_TXT} | 根盘 $(disk_txt)"
  pl_retry "background-validate preflight（只读）" "" bash -c 'cd "$1" && bash background-validate.sh preflight "$2" "$3" "$4" > "$5" 2>&1' _ "${DRV}" "${ENTRY_BUNDLE}" "${ENTRY_BRANCH}" "${ENTRY_COMMIT}" "${PDIR}/bgv-preflight.out" || stop BGV "background-validate preflight 不通过：$(tail -2 "${PDIR}/bgv-preflight.out" | cut -c1-200)"
  : > "${PDIR}/bgv-start.out"
  # shellcheck disable=SC2086
  ( cd "${DRV}" && setsid -f bash background-validate.sh start "${ENTRY_BUNDLE}" "${ENTRY_BRANCH}" "${ENTRY_COMMIT}" ${BGV_ARGS} > "${PDIR}/bgv-start.out" 2>&1 < /dev/null )
  local i; for i in $(seq 1 120); do [ -s "${PDIR}/bgv-start.out" ] && break; sleep 5; done
  local res act out
  res=$(json_field "${PDIR}/bgv-start.out" result); act=$(json_field "${PDIR}/bgv-start.out" action); out=$(json_field "${PDIR}/bgv-start.out" out)
  [ -n "${res}" ] && [ "${act}" = started ] || stop BGV "后台验证没有新起（action=${act}；同提交同部署已有结论时不会重起）：见 ${PDIR}/bgv-start.out"
  log "STEP bgv 已起 action=${act} result=${res} run=${out}"
  local st; st=$(wait_json "${res}" $(( 8 * 3600 )) bgv)
  log "MILESTONE BGV 终态=${st} | 根盘 $(disk_txt)"
  bash "${PIPE_HOME}/post-bgv.sh" "${res}" "${out}" > "${PDIR}/bgv-evidence.txt" 2>&1
  [ "${st}" = passed ] || stop BGV "后台验证终态=${st}，取证在 ${PDIR}/bgv-evidence.txt；不清理测试树，等调度者看"
  # 通过：删测试树（字面路径；固定位置现在只剩可再生的 Go 缓存，测试树由工具自己清）
  BGV_WORK=$(dirname "${D}")/entry-gates-source-work/fixed/codex
  [ -d "${BGV_WORK}/test-tree" ] && rm -rf "${BGV_WORK}/test-tree"
  housekeeping bgv
  # 根盘仍高于 66% 时才丢弃可再生的 Go 缓存（passed 终态工具也不清；VC-5 启动前要压到 ≤67%）
  if [ "$(disk_pct)" -gt 66 ]; then
    log "NOTE 根盘 $(disk_txt) > 66%，丢弃 BGV 的 Go 编译/模块缓存（可再生）"
    rm -rf "${BGV_WORK}/go-build-cache" "${BGV_WORK}/go-mod-cache"
    log "NOTE 清缓存后 根盘 $(disk_txt)"
  fi
  bash "${DRV}/background-validate.sh" require-passed >/dev/null 2>&1 || stop BGV "require-passed 不认当前部署的后台验证结论"
  log "PASS BGV"
}
vc5_admission() {
  local rep=${PDIR}/vc5-admission-dryrun.json rc
  ( cd "${DRV}" && bash vc5-precheck.sh --consume >/dev/null 2>&1 ) && { log "STEP VC-5 准入收据已存在且 consume 通过"; return 0; }
  dryrun_once() { ( cd "${DRV}" && bash vc5-precheck.sh --dry-run > "${rep}" 2> "${PDIR}/vc5-admission-dryrun.err" ); }
  pl_retry "vc5-precheck --dry-run（只读预览）" "1,2,4,5,6,7" dryrun_once; rc=$?
  [ ${rc} -eq 3 ] || [ ${rc} -eq 0 ] || stop VC5 "vc5-precheck --dry-run 异常 rc=${rc}"
  local status; status=$(python3 -B -c "import json,sys; print(json.load(open(sys.argv[1])).get('status'))" "${rep}") || stop VC5 "dry-run 输出不是 JSON"
  [ "${status}" = blocked ] && stop VC5 "VC-5 准入 blocked：$(python3 -B -c "import json,sys; print([c for c in json.load(open(sys.argv[1])).get('checks',[]) if c.get('status')=='blocked'])" "${rep}" | cut -c1-400)"
  [ -n "${APPROVER}" ] || stop VC5 "待调度者：VC-5 准入批准件需要起跑时传 --approver '批准人及依据'（工具不代签，批准绑定 admission_key 与完整 actions）"
  local ap=${RR}/vc5-admission-approval-$(date -u +%Y%m%dt%H%M%Sz).json
  APPROVER="${APPROVER}" python3 -B - "${rep}" "${ap}" <<'PY' || stop VC5 "写准入批准件失败"
import datetime, json, os, sys
r = json.load(open(sys.argv[1]))
now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0); fmt = "%Y-%m-%dT%H:%M:%SZ"
v = {"schema_version": "codex-vc5-admission-approval/v1", "admission_key": r["admission_key"], "actions": r["actions"], "seal_admission": True,
     "approval_id": "vc5-adm-" + now.strftime("%Y%m%dt%H%M%Sz"), "approved_by": os.environ["APPROVER"],
     "approved_at_utc": now.strftime(fmt), "expires_at_utc": (now + datetime.timedelta(hours=2)).strftime(fmt)}
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as s:
    s.write(json.dumps(v, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
PY
  ( cd "${DRV}" && bash vc5-precheck.sh --apply --approval "${ap}" > "${PDIR}/vc5-admission-apply.out" 2>&1 ) || stop VC5 "vc5-precheck --apply 失败：$(tail -1 "${PDIR}/vc5-admission-apply.out" | cut -c1-300)；失败补偿后不可盲目重复同一批准件（README），换批准件前先读 vc5-admission-events/"
  ( cd "${DRV}" && bash vc5-precheck.sh --consume >/dev/null 2>&1 ) || stop VC5 "apply 后 consume 不通过"
  log "STEP VC-5 准入已批准并 apply"
}
run_VC5() {
  need_prev VC5
  local mark=${PDIR}/.vc5-start-mark
  if [ "${ATTACH_VC5}" = 1 ]; then
    # 接管模式：失败批次走恢复链（recovery-plan → vc5-recover → 手工起 vc5-all）后，vc5-all 已在跑；
    # 准入已消费、采集账号首轮已开，这里不再准入、不开采集账号、不起 vc5-all（others_running 也会因 vc5-all 在跑而误拒），只接后面的监视与收尾
    sigign_ok || stop VC5 "本脚本继承了忽略 SIGHUP（nohup 起的）；改用 setsid -f"
    disk_guard VC5 67
    stage_time_check VC5
    pgrep -f '[v]c5-all.sh' >/dev/null 2>&1 || stop VC5 "--attach-vc5 但 vc5-all 没在跑"
    log "STAGE VC5（接管已在跑的 vc5-all）| ${TIME_TXT} | 根盘 $(disk_txt)"
    : > "${mark}"
  else
  common_start_checks VC5
  disk_guard VC5 67
  log "STAGE VC5 | ${TIME_TXT} | 根盘 $(disk_txt)"
  # COMPOSE_BACKUP：vc5-switch.sh 是从它 cp 回 docker-compose.yml 的，必须先有（AP 报告说“切换脚本会创建”是错的）
  if [ ! -f "${COMPOSE_BACKUP}" ]; then
    grep -c "^    image: ${PRODUCTION_IMAGE}\$" "${COMPOSE_DIR}/docker-compose.yml" | grep -qx 1 || stop VC5 "现网 compose 里生产镜像行不是恰好一行 ${PRODUCTION_IMAGE}（vc5-switch 的逐字断言会失败）"
    cp -p "${COMPOSE_DIR}/docker-compose.yml" "${COMPOSE_BACKUP}" && log "STEP 已建 COMPOSE_BACKUP ${COMPOSE_BACKUP}" || stop VC5 "建 COMPOSE_BACKUP 失败"
  fi
  vc5_admission
  capture_on || stop VC5 "采集账号打开失败（访问令牌剩余 <60 分钟会被拒绝，见 ${PDIR}/capture-account.out）"
  : > "${mark}"
  : > "${RR}/vc5-all.out"
  ( cd "${DRV}" && setsid -f bash vc5-all.sh > "${RR}/vc5-all.out" 2>&1 < /dev/null )
  log "STEP vc5-all 已起（候选采集 → Kilo 双入口 → seal → compare → accept → canonical）"
  fi
  # 监视循环。采集账号要到 VC5_ALL_DONE 之后才关：seal checkpoint 的“Kilo 后环境恢复门禁”拿 Kilo 后探针（client-after）对比
  # 候选采集后探针（after），账号 status／schedulable 必须一致；上一版在 Kilo 一结束就关采集账号，seal 因此判环境污染失败。
  # 所以这里循环里不动账号，只有 grep 到 VC5_ALL_DONE 跳出循环后才 capture_off（测试 test_account_closed_only_after_vc5_all_done 守这一条）。
  local t0 miss=0 last=$(date +%s)
  t0=${last}
  while [ $(( $(date +%s) - t0 )) -lt $(( 20 * 3600 )) ]; do
    grep -q 'VC5_ALL_DONE' "${RR}/vc5-all.out" && break
    if grep -q 'VC5_ALL_PAUSED' "${RR}/vc5-all.out"; then stop VC5 "vc5-all 暂停等人处置：$(grep 'VC5_ALL_PAUSED' "${RR}/vc5-all.out" | tail -1 | cut -c1-300)"; fi
    if pgrep -f '[v]c5-all.sh' >/dev/null 2>&1; then miss=0; else miss=$((miss + 1)); fi
    if [ "${miss}" -ge 3 ]; then grep -q 'VC5_ALL_DONE' "${RR}/vc5-all.out" && break; stop VC5 "vc5-all 进程已消失且无 VC5_ALL_DONE；日志 ${RR}/vc5-all.out；恢复走 fix-and-continue.sh／recovery-plan.sh（先读 vc5-start-env-prerequisites 第 4 条：recover 跑完再起 all），本脚本不自动重试"; fi
    if [ $(( $(date +%s) - last )) -ge 1200 ]; then last=$(date +%s); log "WAIT vc5-all | 根盘 $(disk_txt) | $(tail -1 "${RR}/vc5-all.out" | cut -c1-120)"; fi
    sleep 30
  done
  grep -q 'VC5_ALL_DONE' "${RR}/vc5-all.out" || stop VC5 "vc5-all 超时（20 小时）未结束"
  capture_off || log "NOTE 采集账号关闭失败，需手工 bash ${PIPE_HOME}/capture-account-off.sh"
  [ -n "$(ckpt 5)" ] && [ -n "$(find "${NEWDIR}" -maxdepth 6 -name 'vc5-completion.json' 2>/dev/null | head -1)" ] || stop VC5 "VC5_ALL_DONE 之后缺 vc-5-checkpoint 或 vc5-completion.json"
  replay_ok || stop VC5 "VC-5 后 round_context 重放失败"
  housekeeping vc5
  log "PASS VC5"
}
run_VC6() {
  need_prev VC6; common_start_checks VC6
  # ORDER.txt 放在本轮工作目录 ${PREP}/vc6/（由 ${PIPE_HOME}/vc6/ORDER.example.txt 复制修改）；兼容旧位置 ${RR}/vc6/ORDER.txt
  local order=${PREP}/vc6/ORDER.txt
  [ -f "${order}" ] || order=${RR}/vc6/ORDER.txt
  [ -f "${order}" ] || stop VC6 "待调度者：缺 ${PREP}/vc6/ORDER.txt（从 ${PIPE_HOME}/vc6/ORDER.example.txt 复制并按本轮改；格式见该文件头注释）"
  log "STAGE VC6 按 ${order} 执行 | ${TIME_TXT} | 根盘 $(disk_txt)"
  local kind name arg flag n=0 last_capture donedir=${PDIR}/vc6-done script rc
  mkdir -p "${donedir}"
  # 标志用逗号分隔：capture＝步骤前开采集账号（业务验证要发真实请求）；off＝步骤成功后立刻关；最后一个带 capture 的步骤成功后无论如何关；
  # retry＝调度者声明该步骤可证明幂等，失败后按 PIPE_RETRY_MAX 重试（缺省没有任何步骤带 retry）
  last_capture=$(awk '$1=="run" && $4 ~ /(^|,)capture(,|$)/ {n=$2} END{print n}' "${order}")
  while read -r kind name arg flag; do
    case "${kind}" in ''|'#'*) continue ;; esac
    n=$((n + 1))
    case "${kind}" in
      handoff)
        wait_handoff VC6 "${RR}/handoff/${name}.done" "本机步骤 ${name}：${arg}" || stop VC6 "待本机：${name}（${arg} …），完成后（本机脚本会自动创建 ${RR}/handoff/${name}.done）再 --from VC6 续跑；已成功的 run 步骤会自动跳过" ;;
      run)
        # 脚本路径写绝对路径，或相对 ${PIPE_HOME}/vc6/ 的文件名
        case "${arg}" in /*) script=${arg} ;; *) script=${PIPE_HOME}/vc6/${arg} ;; esac
        [ -f "${script}" ] || stop VC6 "ORDER 里的脚本不存在：${script}"
        # 续跑：上次已成功的步骤不重跑（这些脚本多数拒绝覆盖已完成的阶段，重跑只会失败）；要强制重跑先删标记
        if [ -f "${donedir}/${name}" ]; then log "STEP vc6 ${name} skip（上次已成功；强制重跑请先删 ${donedir}/${name}）"; continue; fi
        case ",${flag:-}," in *,capture,*) if [ "${CAPTURE_STATE}" != on ]; then capture_on || stop VC6 "步骤 ${name} 需要采集账号（业务验证真实请求），打开失败"; fi ;; esac
        log "STEP vc6 ${name} start"
        case ",${flag:-}," in
          *,retry,*) pl_retry "vc6 ${name}（ORDER 声明幂等）" "" bash "${script}" > "${PDIR}/vc6-${name}.out" 2>&1 < /dev/null; rc=$? ;;
          *)         bash "${script}" > "${PDIR}/vc6-${name}.out" 2>&1 < /dev/null; rc=$? ;;   # stdin 必须断开：本循环从 ORDER 文件读行，子进程不能吃掉后面的行
        esac
        log "STEP vc6 ${name} end rc=${rc}"
        [ ${rc} -eq 0 ] || stop VC6 "步骤 ${name} 失败 rc=${rc}，见 ${PDIR}/vc6-${name}.out；该步骤没有声明可重试，不自动重试"
        : > "${donedir}/${name}"
        case ",${flag:-}," in *,off,*) if [ "${CAPTURE_STATE}" = on ]; then capture_off; fi ;; esac
        if [ "${name}" = "${last_capture}" ] && [ "${CAPTURE_STATE}" = on ]; then capture_off; fi ;;
      *) stop VC6 "ORDER 第 ${n} 条类型未知：${kind}" ;;
    esac
  done < "${order}"
  if [ "${CAPTURE_STATE}" = on ]; then capture_off; fi
  [ -n "$(ckpt 6)" ] || stop VC6 "ORDER 执行完但没有 vc-6-checkpoint.json"
  replay_ok || stop VC6 "VC-6 后 round_context 重放失败"
  log "PASS VC6"
}

# ---------------------------------------------------------------- 检查模式
CK_OK=0; CK_WAIT=0; CK_MISS=0; CK_FAIL=0
ck() { # $1 状态(OK|待VC-0|待上游|待调度者|缺|不通过) $2 文本
  case "$1" in OK) CK_OK=$((CK_OK + 1)) ;; 待*) CK_WAIT=$((CK_WAIT + 1)) ;; 缺) CK_MISS=$((CK_MISS + 1)) ;; *) CK_FAIL=$((CK_FAIL + 1)) ;; esac
  printf '  [%s] %s\n' "$1" "$2"
}
entry_state() { # 打印 VC-0 状态：running|closed|none
  if pgrep -f '[e]ntry_orchestrator.py' >/dev/null 2>&1; then echo running
  elif [ -d "${NEWDIR}" ] && [ -f "${RR}/entry-steps/vc0-closeout.json" ]; then echo closed
  else echo none; fi
}
check_common() {
  echo "== 公共前置"
  sigign_ok && ck OK "SigIgn 第 1 位（SIGHUP）未被忽略" || ck 不通过 "本 shell 继承了忽略 SIGHUP（nohup？）"
  [ "$(disk_pct)" -le 69 ] && [ "$(disk_gib)" -ge 40 ] && ck OK "根盘 $(disk_txt)（停线 ≤69% 且 ≥40G）" || ck 不通过 "根盘 $(disk_txt) 超停线"
  local o; o=$(others_running)
  [ -z "${o}" ] && ck OK "没有别的编排在跑" || ck 待VC-0 "有编排在跑（第四轮入口，VC-0 收口后应消失）：$(echo "${o}" | head -1 | cut -c1-110)"
  [ -f "${ARM64_VC_ENV}" ] && load_env && ck OK "env.sh 经 parse_env 解析通过（提交 ${ENTRY_COMMIT:0:9}，账号 ${CODEX_ACCOUNT_ID}／Key ${API_KEY_ID}）" || ck 不通过 "env.sh 缺失或 parse_env 拒绝"
  for f in set-env-keys.sh capture-account-on.sh capture-account-off.sh post-bgv.sh; do [ -f "${PIPE_HOME}/${f}" ] && ck OK "${f} 在位" || ck 缺 "${PIPE_HOME}/${f}"; done
  [ -d "${L}" ] && ck OK "计时账本目录在位 ${L##*/}" || ck 缺 "计时账本目录"
  echo "  说明：采集账号现状请以 ${PREP}/logs/ 下的 capture-account.log 为准；本流水线只在 VC5（VC5_ALL_DONE 之后关）与 VC6 业务验证步骤前后动它。"
}
check_stage() {
  CHECKING=1
  local s=$1 es; es=$(entry_state)
  echo "== 阶段 ${s}（预计下限 ${NEED[$s]} 分钟）"
  if [ -d "${NEWDIR}" ]; then
    stage_time_check "$s" && ck OK "计时账本：${TIME_TXT}" || ck 不通过 "计时账本：${TIME_TXT:-读不到}"
  else
    ck 待VC-0 "Formal Campaign 尚未建立（entry.sh 的 VC-0 收口建），计时检查等 VC-0 后"
  fi
  case "$s" in
  VC1)
    echo "  命令：（无新命令）VC-0 收口 entry.sh 已派发首批；首批跑完后 attempt 停在 awaiting_receipts，流水线转入 ${PIPE_HOME}/vc2/vc1-seal.sh（预览 → 批准，批准需 --approver），再等 ${NEWDIR##*/}/control/vc/vc-1-checkpoint.json 并 round_context --mode campaign 重放"
    echo "  输入：Formal 已建、vc0-closeout 步骤记录、入口编排器退出"
    [ "${es}" = closed ] && ck OK "vc0-closeout 已完成" || ck 待VC-0 "入口状态=${es}（VC-0 未收口）"
    echo "  失败处理：recovery-plan.sh（只读预览）／entry.sh --approve-sha256（官方首批恢复）" ;;
  VC2)
    echo "  命令：cd ${DRV} && SEQ0=<下一个可用批次序号> setsid -f bash ${PIPE_HOME}/vc2/vc23-seq.sh > ${RR}/vc23.out（含管理 token 签发、VC-2 四个批次、离线判据门、VC-3 stage-profile；驱动 vc23.sh 的批次序号写死 2～6，不适用）"
    echo "  输入：下列文件 + 人工审核标记"
    for v in RULES_JSON SCENARIOS_JSON EXPECTATIONS_JSON PROFILE_PATCH_JSON; do [ -f "${!v:-/nonexistent}" ] && ck OK "${v}=${!v##*/}" || ck 缺 "${v}=${!v:-未设}"; done
    [ -f "${INPUT_RULE_MIGRATION}" ] && ck OK "INPUT_RULE_MIGRATION" || ck 待调度者 "INPUT_RULE_MIGRATION（${INPUT_RULE_MIGRATION##*/}）：VC-1 的 source-diff 和动态差异出来后才能生成 discovery 分类"
    [ -f "${INPUT_TARGET_SNAPSHOT}" ] && ck OK "INPUT_TARGET_SNAPSHOT" || ck 待调度者 "INPUT_TARGET_SNAPSHOT（${INPUT_TARGET_SNAPSHOT##*/}，Digest 置空）"
    [ -f "${W}/VC2-INPUTS-REVIEWED" ] && ck OK "审核标记在位" || ck 待调度者 "审核标记 ${W}/VC2-INPUTS-REVIEWED（JSON：rule_migration_sha256、target_snapshot_sha256、reviewed_by）"
    [ -f "${COMPOSE_DIR}/.env" ] && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx sub2apiplus-postgres && ck OK "管理 token 签发前提：compose .env、sub2apiplus-postgres 在位" || ck 不通过 "管理 token 签发前提缺（.env 或 postgres 容器）"
    [ -x "${JWTGEN_BIN}" ] && ck OK "JWTGEN_BIN 可执行" || ck 缺 "JWTGEN_BIN"
    for f in vc23-seq.sh vc2-approve-and-stage-seq.sh vc1-seal.sh vc2-mark-reviewed.sh gen_vc1_seal_plans.py gen_vc2_seq_scripts.py; do
      [ -f "${PIPE_HOME}/vc2/${f}" ] && ck OK "VC-2 套件 ${f} 在位" || ck 缺 "${PIPE_HOME}/vc2/${f}"
    done
    [ -d "${D}/backend/internal/officialegress/catalogdata/runtime/profiles/${BASELINE_VERSION}" ] && ck OK "数据根 backend 含 Active ${BASELINE_VERSION} 画像" || ck 待调度者 "数据根 backend 是旧快照（没有 ${BASELINE_VERSION} 画像目录）：prepare-profile／stage-profile 前须换成集成分支 backend（先例：VC-1 期间换，旧的备份成 backend.pre-<提交>-<日期>）" ;;
  VC3)
    echo "  命令：（无新命令）vc23.sh 已做 stage-profile；流水线核对 catalog-stage-receipt.json 与 vc-3-checkpoint，并自动写 env 的 PROFILE_DIGEST"
    echo "  PROFILE_DIGEST 来源：control/${CAMPAIGN_PREFIX}-vc3-candidate-catalog-${ROUND}-${STAMP}/catalog-stage-receipt.json 的 target_profile_digest，与 ${W##*/}/profile.json 的 profile_digest 交叉核对；写入用 set-env-keys.sh（白名单已含）"
    case "${PROFILE_DIGEST}" in "${ZERO_DIGEST}") ck 待上游 "PROFILE_DIGEST 仍是占位全 0（VC-3 后自动写）" ;; *) ck OK "PROFILE_DIGEST 已是真值" ;; esac
    echo "  注：驱动准入（vc5-precheck）另会从 VC-3 收据独立推导有效 PROFILE_DIGEST 写入 vc5-effective-parameters.json，env 值只需保持一致。" ;;
  VC4)
    echo "  命令（本机）：local-candidate-chain.sh → bundle 上传 \$BUNDLE → vc4-handoff.sh 写交接文件（本机脚本在驱动目录 pipeline/local/）"
    echo "  命令（ARM64，流水线）：推 C／DC／RECEIPT → set-env-keys → 构建网络批准 → setsid -f arm64-vc4-gates.sh（约 2h10m）→ housekeeping → setsid -f vc4-all.sh"
    [ "${C}" = "${ENTRY_COMMIT}" ] && ck 待上游 "env 的 C／DC／RECEIPT 仍是占位（C=${C:0:9} 等于入口提交）：由流水线从候选链 bundle 推出后写入" || ck OK "C／DC 已是候选链值"
    [ -f "${RR}/handoff/vc4-chain.json" ] && ck OK "候选链交接文件在位" || ck 待上游 "候选链交接文件 ${RR}/handoff/vc4-chain.json（VC-3 后本机建链，约 8 分钟，再上传）"
    [ -f "${BUNDLE}" ] && ck OK "\$BUNDLE 在位" || ck 待上游 "\$BUNDLE ${BUNDLE}"
    grep -q '^VC4_BUILD_NETWORK=' "${ARM64_VC_ENV}" && ck OK "构建网络已配置" || ck 待调度者 "构建网络专项批准（host）：env 里是注释；起跑时传 --approver 由流水线签，历次升级都是这样"
    [ -d "${HISTORICAL_SOURCE_ROOT}" ] && ck OK "历史门禁源码基线 ${HISTORICAL_SOURCE_ROOT##*/}" || ck 缺 "HISTORICAL_SOURCE_ROOT"
    [ -d "${HISTORY_TEST_TREE}/frontend/node_modules" ] && ck OK "历史测试树前端依赖在位" || ck 缺 "历史测试树 frontend/node_modules"
    [ -d "${B}" ] && ck OK "候选目录已存在（重入）" || ck OK "候选目录 ${B##*/} 由 trees.sh 建（约 +1.5G，构建缓存 +2～4G，注意停线）" ;;
  BGV)
    echo "  命令：background-validate.sh preflight → setsid -f background-validate.sh start ${ENTRY_BUNDLE##*/} ${ENTRY_BRANCH} ${ENTRY_COMMIT:0:9} ${BGV_ARGS}（预计 ~2h；auto 若判子集会短得多）→ post-bgv.sh 取证 → 删测试树 → housekeeping"
    [ -f "${ENTRY_BUNDLE}" ] && ck OK "入口 bundle 在位" || ck 缺 "ENTRY_BUNDLE"
    local pf; pf=$(cd "${DRV}" && timeout 180 bash background-validate.sh preflight "${ENTRY_BUNDLE}" "${ENTRY_BRANCH}" "${ENTRY_COMMIT}" 2>&1); local prc=$?
    if [ ${prc} -eq 0 ]; then ck OK "background-validate preflight 五项通过"; else ck 待VC-0 "background-validate preflight rc=${prc}：$(echo "${pf}" | tail -2 | tr '\n' ' ' | cut -c1-200)"; fi
    ls "${RR}"/background-validation/${ENTRY_COMMIT:0:12}-*.json >/dev/null 2>&1 && ck OK "已有绑定 ${ENTRY_COMMIT:0:9} 的结论文件（同提交同部署不会重起，需 stop 或换部署）" || ck OK "尚无绑定 ${ENTRY_COMMIT:0:9} 的后台验证结论（正常，本阶段产出）" ;;
  VC5)
    echo "  命令：建 COMPOSE_BACKUP → vc5-precheck.sh --dry-run → 批准件 → --apply → --consume → setsid -f vc5-all.sh；Kilo 双入口完成后 capture-account-off.sh"
    grep -c "^    image: ${PRODUCTION_IMAGE}\$" "${COMPOSE_DIR}/docker-compose.yml" | grep -qx 1 && ck OK "现网 compose 生产镜像行恰好一行且等于 PRODUCTION_IMAGE" || ck 不通过 "现网 compose 生产镜像行与 PRODUCTION_IMAGE 不符（vc5-switch 逐字断言会失败）"
    [ -f "${COMPOSE_BACKUP}" ] && ck OK "COMPOSE_BACKUP 已存在" || ck OK "COMPOSE_BACKUP 尚不存在，流水线 VC5 起跑前建（vc5-switch 是从它 cp 回 compose，AP 报告说“切换前脚本创建”不准）"
    [ -n "${APPROVER}" ] && ck OK "已传 --approver" || ck 待调度者 "VC-5 准入批准件需要起跑时 --approver（工具不代签）"
    [ -x "${KILO_BIN}" ] && ck OK "Kilo 二进制在位" || ck 缺 "KILO_BIN"
    ck 待上游 "vc5-precheck --dry-run 要有 VC-4 候选与 BGV passed 才有意义（现在跑只会 blocked）" ;;
  VC6)
    echo "  命令：按 ${PREP}/vc6/ORDER.txt 顺序（run 脚本／handoff 本机步骤；已成功的 run 步骤续跑时跳过）；业务验证步骤标 capture（流水线在其前开采集账号，off 标志处关）"
    local ord=${PREP}/vc6/ORDER.txt; [ -f "${ord}" ] || ord=${RR}/vc6/ORDER.txt
    if [ -f "${ord}" ]; then
      ck OK "ORDER.txt 在位（${ord}）"
      local nrun=0 nbad=0 k nm ar fl
      while read -r k nm ar fl; do
        [ "${k}" = run ] || continue
        nrun=$((nrun + 1))
        if [ ! -f "${ar}" ]; then echo "    缺脚本：${nm} -> ${ar}"; nbad=$((nbad + 1)); elif ! bash -n "${ar}" 2>/dev/null; then echo "    语法不通过：${ar}"; nbad=$((nbad + 1)); fi
      done < "${ord}"
      [ "${nbad}" = 0 ] && ck OK "ORDER 引用的 ${nrun} 个 run 脚本都在位且 bash -n 通过" || ck 不通过 "ORDER 引用的脚本有 ${nbad} 个缺失或语法不通过"
      [ -f "${PREP}/vc6/vc6-env.sh" ] && ck OK "vc6-env.sh 在位（公共前导）" || ck 缺 "${PREP}/vc6/vc6-env.sh"
    else
      ck 缺 "${PREP}/vc6/ORDER.txt 与 VC-6 脚本集"
    fi
    [ -f "${P0_ROLLBACK_EVIDENCE}" ] && ck OK "回退依据在位" || ck 缺 "P0_ROLLBACK_EVIDENCE"
    ck 待上游 "VC-5 验收 attempt（vc5-att.txt）等 VC-5 完成后才有" ;;
  esac
  CHECKING=0
}
if [ "${MODE}" = check ]; then
  load_env || { echo "env 解析失败"; exit 1; }
  echo "vc-pipeline --check  $(utc)  阶段 ${SELECTED[*]}  （只读：不写状态文件、不改 env、不起任何进程）"
  check_common
  for s in "${SELECTED[@]}"; do check_stage "$s"; done
  echo "== 汇总：OK ${CK_OK}／待（上游或 VC-0 或调度者）${CK_WAIT}／缺 ${CK_MISS}／不通过 ${CK_FAIL}"
  [ "${CK_FAIL}" = 0 ] && exit 0 || exit 1
fi

# ---------------------------------------------------------------- 起跑与运行体
mkdir -p "${PDIR}"
if [ "${MODE}" = start ]; then
  load_env || { echo "env 解析失败"; exit 2; }
  sigign_ok || { echo "SigIgn 第 1 位被忽略，拒绝（不要 nohup）"; exit 2; }
  [ -e "${PREP}/vc-pipeline.lock" ] && { echo "已有流水线（${PREP}/vc-pipeline.lock），拒绝"; exit 2; }
  extra=(); [ "${ATTACH_VC5}" = 1 ] && extra+=(--attach-vc5)
  ARM64_VC_ENV="${ARM64_VC_ENV}" setsid -f bash "$0" --run --from "${FROM}" --to "${TO}" --approver "${APPROVER}" --wait-handoff "${WAIT_HANDOFF}" --bgv-args "${BGV_ARGS}" "${extra[@]}" > "${PDIR}/vc-pipeline.out" 2>&1 < /dev/null
  sleep 3; echo "已后台起跑，状态：${STATUS}"; exit 0
fi
# MODE=run
mkdir "${PREP}/vc-pipeline.lock" 2>/dev/null || { echo "已有流水线在跑"; exit 2; }
trap on_exit EXIT
load_env || stop START "env 解析失败"
sigign_ok || stop START "SigIgn 第 1 位被忽略"
log "START 阶段 ${SELECTED[*]} | 提交 ${ENTRY_COMMIT:0:9} | 账号 ${CODEX_ACCOUNT_ID}／Key ${API_KEY_ID} | approver=$([ -n "${APPROVER}" ] && echo 已传 || echo 未传) | 根盘 $(disk_txt)"
for s in "${SELECTED[@]}"; do
  case "$s" in
    VC1) run_VC1 ;; VC2) run_VC2 ;; VC3) run_VC3 ;; VC4) run_VC4 ;; BGV) run_BGV ;; VC5) run_VC5 ;; VC6) run_VC6 ;;
  esac
done
log "DONE 阶段 ${SELECTED[*]} 全部通过"
