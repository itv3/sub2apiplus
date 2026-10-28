#!/bin/bash
# 正式 Campaign 停在 VC-2（批准预览 classify-preview 被拒）时：部署新头之后的“工具演进 → 对账 → 续跑 VC-2/VC-3”一条龙。
# 采集主机侧脚本，随驱动安装在 <驱动目录>/pipeline/evolve-and-resume.sh。
#
# 适用场景（上一版实际发生过）：VC-2 批次 6（classify-preview）被拒——受管清单里残留 baseline 版本字面量。修清单、重绑冻结摘要、
#   重封身份基线后产出集成分支新头；新头受监督部署后，对已建 Campaign 必须走 tool-evolution 登记，再对账失败批次、以 N+1 批次逐字重派。
#   本脚本只覆盖“失败动作是 classify-preview、其余现场符合预期”这一种；别的失败交调度者，不要硬套。
#
# 用法（ARM64，root）：
#   bash evolve-and-resume.sh --check [--head <40 位新头>] [--new-root <新树副本目录>]
#        只做判定：不写账本、不写 Campaign、不写 vc-status.txt（只打印）。--new-root 给出未部署的新树副本
#        （内含 tools/official_client_capture）时，部署前就能预测演进预览与批准输入校验的结果。
#   bash evolve-and-resume.sh --apply --head <40 位新头> --approver '<批准人及依据>' [--from <步骤>] [--foreground]
#        真执行：默认 setsid -f 后台运行体，状态写 pipeline/vc-status.txt（最后一行必为 EXIT rc=N），输出在 evolve/run-<UTC>.out。
#   步骤（--from 可选）：E0 现场前置 → E1 部署核对 → E2 演进预览 → E3 演进登记 → E4 刷新输入并预检 → E5 对账失败批次
#                       → E6 重派 VC-2 预览／批准与 VC-3 → E7 移交（打印流水线 VC3 与 BGV 命令）
#
# 部署本身在本机做（要把新头打包上传），不在本脚本里：先在本机跑
#   bash <驱动目录>/pipeline/local/evo-local.sh <新头>   （部署 → 历史测试树 → 入口 bundle → 参数文件五键）
# 环境变量（pipeline.env 或命令行）：EVO_REASON 登记原因（--apply 必给）；EVO_FAILED_AT_UTC 失败批次的失败时刻（E1 用它断言部署收据晚于失败；可省）；EVO_BASE_COMMIT 部署前的基线提交（写进原因，可省）。
# 然后才来本脚本。E1 会核对部署确已完成；没完成就 STOPPED 并打印该跑的本机命令。
#
# 硬规则：
#   * 失败一律写 STOPPED <步骤> :: <原因> 并退出 1，不自动重试、不自动延期、不改账号状态、不 --force；
#   * 每一步前都重新判定现场（已完成的自动跳过），所以重复执行、断点续跑都安全；
#   * 写账本的动作只有三个：tool-evolution 登记（E3）、reconcile-supervisor-run（E5）、VC 批次派发（E6）；
#     批准人文本必须由调度者通过 --approver 给出，脚本不自己编；
#   * 不用 nohup（SigIgn 第 1 位非零就拒绝）；rm 只用字面路径。
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 2; }
pl_defaults
STATUS=${PDIR}/vc-status.txt
EVO=${PIPE_HOME}/evo
OUTDIR=${RR}/evolve
mkdir -p "${PDIR}"

MODE=""; HEAD=""; APPROVER=""; FROM="E0"; FOREGROUND=0; NEW_ROOT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --apply) MODE=apply ;;
    --run) MODE=run ;;
    --head) HEAD=${2:-}; shift ;;
    --approver) APPROVER=${2:-}; shift ;;
    --from) FROM=${2:-}; shift ;;
    --new-root) NEW_ROOT=${2:-}; shift ;;
    --foreground) FOREGROUND=1 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done
[ -n "${MODE}" ] || { echo "必须给 --check 或 --apply"; exit 2; }
case "${FROM}" in E0|E1|E2|E3|E4|E5|E6|E7) ;; *) echo "--from 只能是 E0～E7"; exit 2 ;; esac
if [ -n "${HEAD}" ] && ! [[ "${HEAD}" =~ ^[0-9a-f]{40}$ ]]; then echo "--head 必须是 40 位小写十六进制（本机 git rev-parse 现取）"; exit 2; fi

# ---------------------------------------------------------------- 公共函数
utc() { date -u +%FT%TZ; }
log() { echo "$(utc) $*" >> "${STATUS}"; echo "$(utc) $*"; }
disk_pct() { df / | awk 'NR==2{gsub("%","",$5); print $5}'; }
disk_gib() { df -BG / | awk 'NR==2{gsub("G","",$4); print $4}'; }
disk_txt() { df -h / | awk 'NR==2{print $5" 已用，可用 "$4}'; }
sigign_ok() { pl_sigign_ok; }
load_env() {
  pl_load_round_env || return 1
  pl_defaults
  # evo/*.py 从这些环境变量取数据根、Campaign、输入目录、目标版本（脚本里不写死任何一轮的名字）
  export EVO_DATA_ROOT=${D} EVO_CAMPAIGN=${NEW} EVO_INPUTS=${IN} EVO_TARGET_VERSION=${TARGET_VERSION}
  TT=${TARGET_VERSION//./_}
}
stop() { log "STOPPED $1 :: $2"; exit 1; }
others_running() {
  pgrep -af '[e]ntry_orchestrator.py|[b]ackground_validation.py|[e]ntry-gates.sh|[c]hain-rehearsal|[a]rm64-vc4-gates.sh|[v]c4-all.sh|[v]c5-all.sh|[v]c23(-seq)?\.sh|[v]c2-approve|[e]ntry-dryrun.sh start|[s]tart-entry.sh|[c]ompile-and-run-vc-batch|[v]c-batch.sh|[v]c-pipeline.sh (start|--run)|[f]ix-and-continue.sh|[r]ecovery-plan.sh' | grep -v 'evolve-and-resume.sh' || true
}
# 受管 CLI（与 fix-and-continue 的 managed 同一调用形态：cwd=数据根、PYTHONPATH=数据根）
cu_cli() { ( cd "${D}" && PYTHONPATH="${D}" CODEX_UPGRADE_IDENTITY_MEMO="$(dirname "${D}")/identity-memo" python3 -B -m tools.official_client_capture.codex_upgrade "$@" ); }
jget() { python3 -B -c "
import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1].split('.'):
    d = d.get(k) if isinstance(d, dict) else None
print(d if not isinstance(d, (dict, list)) else json.dumps(d, ensure_ascii=False))" "$1"; }
state_json() { python3 -B "${EVO}/evo_state.py" --campaign-dir "${NEWDIR}" --ledger-dir "${L}"; }
evo_status_json() { cu_cli tool-evolution-status --campaign-dir "${NEWDIR}" 2>/dev/null; }

# VC-2 阶段剩余分钟（执行层／总／项目取小）。输出 “phase 执行层 总 项目 暂停范围 剩余”
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
ex, tot, proj = mins(d["execution_deadline_at_utc"]), mins(d["total_deadline_at_utc"]), mins(d["project_deadline_at_utc"])
print(d["phase"], ex, tot, proj, ",".join(d.get("paused_scopes") or []) or "-", min(ex, tot, proj))
PY
  )
}
NEED_MIN=45   # VC-2 批准预览＋批准＋VC-3 实测约 10～15 分钟，留足对账与重试余量

# ---------------------------------------------------------------- --check 的逐项输出
CK_OK=0; CK_WAIT=0; CK_MISS=0; CK_BAD=0
ck() { # $1 OK|待上游|缺|不通过  $2 说明
  case "$1" in OK) CK_OK=$((CK_OK+1));; 待上游|待调度者) CK_WAIT=$((CK_WAIT+1));; 缺) CK_MISS=$((CK_MISS+1));; *) CK_BAD=$((CK_BAD+1));; esac
  echo "[$1] $2"
}

do_check() {
  echo "== EVO --check $(utc)（只读：不写账本、不写 Campaign、不写 vc-status.txt）"
  # --- 公共
  [ "$(id -u)" = 0 ] && ck OK "以 root 运行" || ck 不通过 "必须 root"
  sigign_ok && ck OK "SigIgn 未忽略 SIGHUP（不是 nohup 起的）" || ck 不通过 "继承了忽略 SIGHUP，改用 setsid -f"
  if load_env; then ck OK "env.sh 经 parse_env 解析（ENTRY_COMMIT=${ENTRY_COMMIT:0:9}）"; else ck 不通过 "env.sh 解析失败"; return; fi
  [ "$(disk_pct)" -le 67 ] && [ "$(disk_gib)" -ge 40 ] && ck OK "根盘 $(disk_txt)" || ck 不通过 "根盘 $(disk_txt) 超线（≤67%/≥40G）"
  local o; o=$(others_running)
  [ -z "${o}" ] && ck OK "机器上没有别的编排／批次在跑" || ck 不通过 "有别的编排在跑：$(echo "${o}" | head -2 | cut -c1-140)"
  local pyc; pyc=$(find "${D}/tools" "${DRV}" -name __pycache__ 2>/dev/null | wc -l)
  [ "${pyc}" = 0 ] && ck OK "受管工具树与驱动目录 __pycache__=0" || ck 不通过 "存在 __pycache__ ${pyc} 处（门禁前置检查会退出 9）"
  [ ! -d "${PREP}/evolve.lock" ] && ck OK "没有残留的 evolve.lock" || ck 不通过 "evolve.lock 在（上次运行未收尾或正在跑）"
  # --- Campaign 现场
  local st; st=$(state_json 2>&1) || { ck 不通过 "读不了 Campaign 现场：${st:0:200}"; return; }
  local lstat lrev lseq
  lstat=$(echo "${st}" | jget ledger_status); lrev=$(echo "${st}" | jget ledger_review_phase)
  echo "     账本：status=${lstat} review_phase=${lrev} head=$(echo "${st}" | jget ledger_head_sequence) 根因=$(echo "${st}" | jget ledger_review_root_cause_id)"
  case "${lstat}:${lrev}" in
    stage_review_required:VC-2) ck OK "账本停在 VC-2 阶段审核（stage_review_required），演进登记允许（只拒 stopped／complete／abandoned）";;
    active:*) ck OK "账本已 active（已对账放行或尚未失败）：E5 之后的状态";;
    *) ck 不通过 "账本状态 ${lstat}/${lrev} 不是预期的 stage_review_required/VC-2";;
  esac
  lseq=$(echo "${st}" | jget last_commit.sequence)
  echo "     最近已提交批次：序号 ${lseq} phase=$(echo "${st}" | jget last_commit.phase) 父 run 状态=$(echo "${st}" | jget last_commit.run_state) 动作=$(echo "${st}" | jget last_commit.action_ids)"
  [ "$(echo "${st}" | jget checkpoints.1)" = True ] && ck OK "VC-1 checkpoint 在（官方作业已封存）" || ck 不通过 "缺 VC-1 checkpoint"
  [ "$(echo "${st}" | jget checkpoints.2)" = False ] && ck OK "VC-2 checkpoint 尚未写（待本脚本续跑）" || ck OK "VC-2 checkpoint 已在（E6 将跳过已完成部分）"
  [ "$(echo "${st}" | jget classification_approved_exists)" = False ] && ck OK "classification/approved 不存在（没有半批准产物）" || ck 不通过 "classification/approved 已存在，按 VC-2 失败矩阵先人工审"
  [ "$(echo "${st}" | jget classification_draft_count)" = 1 ] && ck OK "classification/draft 恰好 1 份（批次 5 的草案，演进后仍有效）" || ck 不通过 "草案数不是 1：$(echo "${st}" | jget classification_draft_count)"
  # 阶段时间
  local di; di=$(deadline_info 2>/dev/null) || di=""
  if [ -n "${di}" ]; then
    read -r ph ex tot proj paused rem <<<"${di}"
    echo "     计时：账本阶段=${ph} 执行层 ${ex}／总 ${tot}／项目 ${proj} 分钟，暂停范围 ${paused}"
    if [ "${paused}" = "-" ] && [ "${rem}" -ge "${NEED_MIN}" ]; then ck OK "阶段剩余 ${rem} 分钟 ≥ 下限 ${NEED_MIN}"; else ck 不通过 "阶段剩余 ${rem} 分钟不足或有暂停范围（deadline-extend 由调度者做，脚本不自动延期）"; fi
  else ck 不通过 "读不到有效截止（effective_deadlines 失败）"; fi
  # 失败批次可重派性（受管只读判定，与对账用的同一函数）
  local run; run=$(echo "${st}" | jget last_commit.run_dir)
  local replay
  replay=$( cd "${D}" && PYTHONPATH="${D}" python3 -B - "${NEWDIR}" "${run}" <<'PY' 2>&1
import json, sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
inner = json.load(open(Path(sys.argv[2]) / "campaign-run-manifest.json"))["manifest"]
f = cu._campaign_stage_replay_facts(Path(sys.argv[1]), inner)
print("allowed=%s reasons=%s actions=%s" % (f["allowed"], f["reasons"], [a["action_id"] for a in f["actions"]]))
PY
  )
  echo "     阶段幂等重派判定：${replay}"
  echo "${replay}" | grep -q "allowed=True" && ck OK "失败批次具备阶段幂等重派合同（reconcile 会写 stage-replay 证明）" || ck 不通过 "失败批次不具备重派合同：${replay:0:200}"
  # --- 部署与演进
  local es; es=$(evo_status_json) || es=""
  if [ -z "${es}" ]; then ck 不通过 "tool-evolution-status 读不出"; else
    local est idx; est=$(echo "${es}" | jget status); idx=$(echo "${es}" | jget effective_index)
    echo "     工具演进：status=${est} 有效序号=${idx} 当前 files_sha256=$(echo "${es}" | jget current_summary.files_sha256 | cut -c1-12)"
    case "${est}:${idx}" in
      registered:0) ck 待上游 "还没部署新头（当前受管工具＝Campaign 冻结身份，无漂移）：等修复交回后本机跑 local/evo-local.sh <新头>";;
      evolution_required:*) ck OK "新头已部署（受管工具与有效身份有漂移，待登记）：$(echo "${es}" | jget unregistered_drift | cut -c1-160)";;
      registered:*) ck OK "演进已登记（有效序号 ${idx}），E2/E3 将跳过";;
      *) ck 不通过 "未预期的演进状态 ${est}:${idx}";;
    esac
    # 已部署：直接用真实预览判定影响（只读，不带 --approve-sha256）
    if [ "${est}" = evolution_required ] && [ -n "${HEAD}" ]; then
      local pv; pv=$(cu_cli tool-evolution --campaign-dir "${NEWDIR}" --fix-commit "${HEAD}" --reason "EVO --check 预览（不登记）" 2>&1) || true
      echo "${pv}" | python3 -B -c "
import json, sys
t = sys.stdin.read()
try: d = json.loads(t)
except ValueError: print('     预览输出不是 JSON：' + t[-300:]); raise SystemExit(0)
c = d.get('changes', {}); i = d.get('impact', {})
print('     真实预览：status=%s official 受影响=%s unmapped=%s wire 闭包变化=%s 层=%s' % (d.get('status'), (i.get('official') or {}).get('affected_job_ids'), c.get('unmapped_paths'), c.get('wire_closure_changed'), {k: len(v) for k, v in (c.get('paths_by_layer') or {}).items() if v}))
print('     review_sha256=%s' % d.get('review_sha256'))"
      echo "${pv}" | grep -q '"status": "approval_required"' && ck OK "真实 tool-evolution 预览可批准（approval_required）" || ck 不通过 "真实预览未得到 approval_required：$(echo "${pv}" | tail -c 240)"
    fi
  fi
  # --- 未部署时的预测（可选）
  if [ -n "${NEW_ROOT}" ]; then
    if [ -f "${NEW_ROOT}/tools/official_client_capture/codex_upgrade.py" ]; then
      local pr; pr=$(python3 -B "${EVO}/evo_predict.py" --new-root "${NEW_ROOT}" 2>&1); local prc=$?
      echo "${pr}" | sed 's/^/     预测：/'
      [ ${prc} -eq 0 ] && ck OK "部署前预测：演进预览会放行" || ck 不通过 "部署前预测：预览被拒（见上）"
      local tools="${NEW_ROOT}/tools/official_client_capture" K=/var/tmp/evo-predict/inputs-check
      mkdir -p "${K}"; chmod 700 "${K}"
      python3 -B - "${tools}/codex_upgrade_scenarios_${TT}.json" "${W}/profile.json" "${K}/scenarios.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1])); p = json.load(open(sys.argv[2])); s["profile_id"] = p["profile_id"]
open(sys.argv[3], "w").write(json.dumps(s, ensure_ascii=False, indent=2) + "\n")
PY
      cp "${tools}/candidate_rule_expectations_${TT}.json" "${K}/assertion-profile.json"
      cp "${tools}/codex_upgrade_rules_${TT}.json" "${K}/target-rules.json"
      local ic; ic=$(python3 -B "${EVO}/evo_inputs_check.py" --rules "${K}/target-rules.json" --scenarios "${K}/scenarios.json" --assertion "${K}/assertion-profile.json" --patch "${tools}/profile_rule_patches_${TT}.json" 2>&1); local icrc=$?
      echo "${ic}" | sed 's/^/     输入预检：/'
      [ ${icrc} -eq 0 ] && ck OK "部署前预测：classify-preview 的输入校验全部通过（baseline 版本字面量等校验）" || ck 不通过 "部署前预测：输入校验被拒（见上）"
    else ck 不通过 "--new-root 下没有 tools/official_client_capture/codex_upgrade.py：${NEW_ROOT}"; fi
  else
    ck 待上游 "未给 --new-root：部署前预测跳过（修复交回后用 git archive 出新头的 tools 目录再传一份）"
  fi
  # --- 批准与 BGV 前置
  if [ -n "${APPROVER}" ]; then ck OK "已传 --approver"; else ck 待调度者 "--apply 需要 --approver（批准人及依据，由调度者写）"; fi
  if [ -n "${HEAD}" ]; then
    [ "${ENTRY_COMMIT}" = "${HEAD}" ] && ck OK "env.sh ENTRY_COMMIT 已是新头（BGV 前置）" || ck 待上游 "env.sh ENTRY_COMMIT=${ENTRY_COMMIT:0:9}，新头 ${HEAD:0:9}：local/evo-local.sh 会用 set-env-keys.sh 改 5 键（BGV 前置）"
    ls "${PREP}/stage/stage-${HEAD:0:12}.tgz" >/dev/null 2>&1 && ck OK "新头暂存包已上传" || ck 待上游 "新头暂存包 stage-${HEAD:0:12}.tgz 未上传"
  else ck 待上游 "未给 --head：新头尚未交回"; fi
  echo "== 汇总：OK ${CK_OK}／待（上游或调度者）${CK_WAIT}／缺 ${CK_MISS}／不通过 ${CK_BAD}"
  [ "${CK_BAD}" = 0 ] && [ "${CK_MISS}" = 0 ]
}

# ---------------------------------------------------------------- 运行体
# 登记原因：调度者通过 EVO_REASON 给出（写明修了什么、哪些层变化、官方作业是否受影响）；没给就用保守的通用说明
EV_REASON_FIX() {
  if [ -n "${EVO_REASON:-}" ]; then echo "${EVO_REASON}"; return; fi
  echo "VC-2 批准预览被拒后的工具修复（提交范围 ${EVO_BASE_COMMIT:-基线}..${HEAD:0:9}）；预览已核对：官方作业零受影响、wire 闭包不变、无映射外路径"
}

step_E0() {
  sigign_ok || stop E0 "本脚本继承了忽略 SIGHUP（nohup 起的）"
  load_env || stop E0 "env.sh 解析失败"
  [ -n "${HEAD}" ] || stop E0 "--apply 必须给 --head（本机 git rev-parse 现取的 40 位新头）"
  [ -n "${APPROVER}" ] || stop E0 "待调度者：--apply 必须给 --approver（批准人及依据；脚本不代签）"
  [ -n "${EVO_REASON:-}" ] || log "NOTE 未给 EVO_REASON，登记原因用通用说明；建议调度者写明修了什么再登记"
  [ -z "$(others_running)" ] || stop E0 "机器上有别的编排在跑：$(others_running | head -2 | cut -c1-140)"
  { [ "$(disk_pct)" -le 67 ] && [ "$(disk_gib)" -ge 40 ]; } || stop E0 "根盘 $(disk_txt) 超线（≤67%/≥40G），按清盘规则由调度者处理"
  local st lstat; st=$(state_json) || stop E0 "读不了 Campaign 现场"
  lstat=$(echo "${st}" | jget ledger_status)
  case "${lstat}" in stage_review_required|active) ;; *) stop E0 "账本状态 ${lstat} 不在预期（stage_review_required／active）";; esac
  [ "$(echo "${st}" | jget checkpoints.1)" = True ] || stop E0 "缺 VC-1 checkpoint"
  local di rem paused; di=$(deadline_info) || stop E0 "读不到有效截止"
  read -r _ph _ex _tot _proj paused rem <<<"${di}"
  if [ "${paused}" != "-" ] || [ "${rem}" -lt "${NEED_MIN}" ]; then
    stop E0 "阶段剩余 ${rem} 分钟（暂停范围 ${paused}），不够 ${NEED_MIN}：请调度者按 DL-vc0-extend.md 第 5 节 deadline-extend（scope=stage, phase=VC-2），脚本不自动延期"
  fi
  log "STEP E0 现场前置通过 | ${di} | 账本 ${lstat} | 最近批次 $(echo "${st}" | jget last_commit.sequence)（$(echo "${st}" | jget last_commit.run_state)）| 根盘 $(disk_txt)"
}

step_E1() { # 部署核对
  local es est idx; es=$(evo_status_json) || stop E1 "tool-evolution-status 读不出"
  est=$(echo "${es}" | jget status); idx=$(echo "${es}" | jget effective_index)
  if [ "${est}" = registered ] && [ "${idx}" != 0 ]; then log "STEP E1 演进已登记（序号 ${idx}），部署核对跳过"; return 0; fi
  if [ "${est}" = registered ]; then
    stop E1 "受管工具与 Campaign 冻结身份一致——新头还没部署。本机先跑：bash <仓库>/tools/arm64_capture_driver/driver/pipeline/local/evo-local.sh ${HEAD}"
  fi
  [ "${est}" = evolution_required ] || stop E1 "未预期的演进状态 ${est}"
  ls "${PREP}/stage/stage-${HEAD:0:12}.tgz" >/dev/null 2>&1 || stop E1 "新头暂存包 stage-${HEAD:0:12}.tgz 不在 ARM64：部署的不是这个头？"
  local dep want have
  dep=$(ls -t "${D}"/control/codex-*-supervisor-enable-*.json 2>/dev/null | head -1)
  [ -n "${dep}" ] || stop E1 "找不到受监督部署收据"
  want=$(echo "${es}" | jget current_summary.files_sha256)
  have=$(python3 -B -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('status'), d.get('tool_files_sha256'), d.get('created_at_utc'))" "${dep}")
  echo "${have}" | grep -q "^passed ${want} " || stop E1 "最新部署收据 ${dep##*/} 与当前受管树摘要不符（收据：${have}；受管树 files_sha256=${want}）"
  if [ -n "${EVO_FAILED_AT_UTC:-}" ]; then
    python3 -B -c "
import datetime, json, sys
d = json.load(open(sys.argv[1])); t = datetime.datetime.fromisoformat(d['created_at_utc'].replace('Z', '+00:00'))
f = datetime.datetime.fromisoformat(sys.argv[2].replace('Z', '+00:00'))
assert t > f, '部署收据早于失败批次的失败时刻'" "${dep}" "${EVO_FAILED_AT_UTC}" || stop E1 "部署收据不晚于失败批次的失败时刻（${EVO_FAILED_AT_UTC}），说明部署的不是修复后的新头"
  fi
  [ "$(find "${D}/tools" "${DRV}" -name __pycache__ 2>/dev/null | wc -l)" = 0 ] || stop E1 "部署后受管树／驱动目录出现 __pycache__"
  log "STEP E1 部署核对通过 | 收据 ${dep##*/} | files_sha256=${want:0:12} | 新头 ${HEAD:0:9}"
}

step_E2E3() { # 演进预览（E2）与登记（E3）
  local es idx; es=$(evo_status_json) || stop E2 "tool-evolution-status 读不出"
  idx=$(echo "${es}" | jget effective_index)
  if [ "$(echo "${es}" | jget status)" = registered ] && [ "${idx}" != 0 ]; then log "STEP E2 演进已登记（序号 ${idx}），预览与登记跳过"; return 0; fi
  mkdir -p "${OUTDIR}"; chmod 700 "${OUTDIR}"
  local ts pv rv; ts=$(date -u +%Y%m%dt%H%M%Sz)
  pv="${OUTDIR}/evolution-preview-${ts}.json"
  cu_cli tool-evolution --campaign-dir "${NEWDIR}" --fix-commit "${HEAD}" --reason "$(EV_REASON_FIX)" > "${pv}" 2> "${pv}.err" || true
  rv=$(python3 -B - "${pv}" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except ValueError:
    print("BAD 预览输出不是 JSON"); raise SystemExit(0)
c, i = d.get("changes", {}), d.get("impact", {})
off, cands = i.get("official", {}), i.get("candidates", {})
problems = []
if d.get("status") != "approval_required": problems.append("status=%s" % d.get("status"))
if c.get("wire_closure_changed"): problems.append("编排器 wire 闭包变化")
if c.get("unmapped_paths"): problems.append("存在映射不到作业的变化路径 %s" % c.get("unmapped_paths"))
if off.get("affected_job_ids"): problems.append("官方作业受影响 %s" % off.get("affected_job_ids"))
if cands: problems.append("出现候选范围 %s" % list(cands))
if (d.get("evaluator") or {}).get("changed_fields"): problems.append("评估器摘要变化 %s" % d["evaluator"]["changed_fields"])
layers = {k for k, v in (c.get("paths_by_layer") or {}).items() if v}
if layers - {"evidence_semantics", "wire_producer", "control"}: problems.append("出现预期之外的层 %s" % sorted(layers))
if problems:
    print("BAD " + "；".join(problems))
else:
    print("OK %s" % d["review_sha256"])
PY
  )
  case "${rv}" in
    OK*) ;;
    *) stop E2 "演进预览不符预期：${rv#BAD }（完整预览 ${pv}，stderr ${pv}.err）。这是需要调度者判断的情形（可能要重采或拆开登记），脚本不继续" ;;
  esac
  local review=${rv#OK }
  log "STEP E2 演进预览符合预期：官方作业零受影响、wire 闭包不变、无映射外路径 | review_sha256=${review:0:16} | ${pv}"
  local ap="${OUTDIR}/evolution-apply-${ts}.json"
  cu_cli tool-evolution --campaign-dir "${NEWDIR}" --fix-commit "${HEAD}" --reason "$(EV_REASON_FIX)" \
    --approve-sha256 "${review}" --approved-by "${APPROVER}" > "${ap}" 2> "${ap}.err" || stop E3 "tool-evolution 登记失败，见 ${ap}.err"
  grep -q '"status": "evolution_applied"' "${ap}" || stop E3 "登记输出没有 evolution_applied，见 ${ap}"
  local after; after=$(evo_status_json | jget status)
  [ "${after}" = registered ] || stop E3 "登记后 tool-evolution-status 仍是 ${after}"
  log "STEP E3 工具演进已登记 | 批准人=${APPROVER} | 收据 $(python3 -B -c "import json,sys; print(json.load(open(sys.argv[1])).get('path'))" "${ap}")"
}

step_E4() { # 刷新批准输入并预检
  local ts; ts=$(date -u +%Y%m%dt%H%M%Sz)
  local newrules="${RULES_JSON}" newscen="${SCENARIOS_JSON}" newexp="${EXPECTATIONS_JSON}"
  mkdir -p "${OUTDIR}"
  # 生成到暂存文件，比对后才替换（旧文件改名留档，不删除）
  local S="${OUTDIR}/inputs-${ts}"; mkdir -p "${S}"; chmod 700 "${S}"
  cp "${newrules}" "${S}/target-rules.json"
  python3 -B - "${newscen}" "${W}/profile.json" "${S}/scenarios.json" <<'PY' || stop E4 "生成 scenarios.json 失败"
import json, sys
s = json.load(open(sys.argv[1])); p = json.load(open(sys.argv[2])); s["profile_id"] = p["profile_id"]
open(sys.argv[3], "w").write(json.dumps(s, ensure_ascii=False, indent=2) + "\n")
PY
  cp "${newexp}" "${S}/assertion-profile.json"
  chmod 600 "${S}"/*.json
  local changed=0 f diffs=0
  for f in target-rules.json scenarios.json assertion-profile.json; do cmp -s "${S}/${f}" "${W}/${f}" || diffs=$((diffs+1)); done
  # 对账放行（账本 active）之后，阶段幂等证明已绑定当时的输入摘要，输入再变会被监督器按“对账后漂移”拒绝重派。
  if [ "${diffs}" -gt 0 ] && [ "$(state_json | jget ledger_status)" = active ] && [ "$(state_json | jget checkpoints.2)" = False ]; then
    stop E4 "账本已 active（E5 对账已放行），但批准输入与新工具树仍有 ${diffs} 处不同；此时改输入会使重派被拒。交调度者：需先让账本回到可重新对账的状态，不要手改"
  fi
  for f in target-rules.json scenarios.json assertion-profile.json; do
    if ! cmp -s "${S}/${f}" "${W}/${f}"; then
      changed=1
      mv "${W}/${f}" "${W}/${f}.superseded-${ts}"
      cp "${S}/${f}" "${W}/${f}"; chmod 600 "${W}/${f}"
      log "STEP E4 已刷新批准输入 ${f}（旧文件改名 ${f}.superseded-${ts}）"
    fi
  done
  [ "${changed}" = 0 ] && log "STEP E4 批准输入已是新版本，无需刷新"
  # 离线只读预检：classify-preview 会做的全部输入校验
  python3 -B "${EVO}/evo_inputs_check.py" > "${OUTDIR}/inputs-check-${ts}.out" 2>&1 \
    || stop E4 "批准输入离线预检被拒（不派批次，避免同根因触顶）：$(tail -2 "${OUTDIR}/inputs-check-${ts}.out" | tr '\n' ' ')"
  log "STEP E4 批准输入离线预检通过（${OUTDIR}/inputs-check-${ts}.out）"
  # 联合摘要与批次计划（与 vc23-seq.sh 同一算法）
  JOINT=$(cd "${D}" && PYTHONPATH="${D}" python3 -B - "${W}" <<'PY'
import sys, json
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
W = Path(sys.argv[1])
files = {"target_rule_manifest": "target-rules.json", "migration_manifest": "rule-migration.json", "scenario_manifest": "scenarios.json", "profile_manifest": "profile.json", "assertion_profile_manifest": "assertion-profile.json"}
print(cu._fingerprint({k: cu._normalized_json_sha256(json.loads((W / v).read_text())) for k, v in files.items()}))
PY
  )
  [[ "${JOINT}" =~ ^[0-9a-f]{64}$ ]] || stop E4 "联合摘要计算失败"
  ( cd "${DRV}" && python3 -B "${DRV}/gen_vc2_plans.py" "${W}" "${NEW}" "${IN}" "${JOINT}" | cut -c1-140 ) || stop E4 "生成批次计划失败"
  chmod 600 "${W}"/*.json
  echo "${JOINT}" > "${OUTDIR}/JOINT"
  log "STEP E4 联合摘要 JOINT=${JOINT:0:16}…已重算，批次计划已重写"
}

step_E5() { # 对账失败批次
  local st lstat run; st=$(state_json) || stop E5 "读不了现场"
  lstat=$(echo "${st}" | jget ledger_status)
  run=$(echo "${st}" | jget last_commit.run_dir)
  if [ "${lstat}" = active ]; then log "STEP E5 账本已 active（失败批次已对账放行），对账跳过"; return 0; fi
  [ "${lstat}" = stage_review_required ] || stop E5 "账本状态 ${lstat} 不是 stage_review_required"
  [ "$(echo "${st}" | jget last_commit.run_state)" = failed ] || stop E5 "最近批次的父 run 状态不是 failed：$(echo "${st}" | jget last_commit.run_state)"
  echo "$(echo "${st}" | jget last_commit.action_ids)" | grep -q '\["classify-preview"\]' || stop E5 "失败动作不是 classify-preview（$(echo "${st}" | jget last_commit.action_ids)），超出本脚本覆盖范围，交调度者"
  local ts out; ts=$(date -u +%Y%m%dt%H%M%Sz); out="${OUTDIR}/reconcile-${ts}.json"
  cu_cli reconcile-supervisor-run --campaign-dir "${NEWDIR}" --run-dir "${run}" > "${out}" 2> "${out}.err"; local rc=$?
  local verdict; verdict=$(python3 -B - "${out}" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except ValueError:
    print("BAD 对账输出不是 JSON"); raise SystemExit(0)
dec = d.get("decision") or {}
counts = (d.get("project_head") or {}).get("root_cause_counts") or {}
print("%s status=%s decision=%s stage_replay_allowed=%s root_cause_counts=%s" % (
    "OK" if d.get("status") == "recoverable" else "BAD", d.get("status"), dec.get("decision"),
    (d.get("stage_replay") or {}).get("allowed"), json.dumps(counts, ensure_ascii=False)))
PY
  )
  [ ${rc} -eq 0 ] && [ "${verdict%% *}" = OK ] || stop E5 "对账未得到 recoverable（rc=${rc}）：${verdict}；见 ${out}、${out}.err。注意同根因已计 1 次，上限 2，不要盲目重试"
  [ "$(state_json | jget ledger_status)" = active ] || stop E5 "对账后账本仍不是 active"
  log "STEP E5 失败批次已对账：${verdict}（${out}）"
}

step_E6() { # 逐字重派 classify-preview（N+1）→ 离线判据门 → 批准 → VC-3 stage-profile
  local st seq0; st=$(state_json) || stop E6 "读不了现场"
  if [ "$(echo "${st}" | jget checkpoints.3)" = True ]; then log "STEP E6 VC-3 checkpoint 已在，续跑跳过"; return 0; fi
  [ "$(state_json | jget ledger_status)" = active ] || stop E6 "账本不是 active（先完成 E5）"
  local last_seq; last_seq=$(echo "${st}" | jget max_batch_sequence)
  # 最近批次必须是已对账放行的失败批次 6（preview）；SEQ0 使 vc2-approve-and-stage-seq.sh 的 SEQ0+2 恰为 N+1
  [ "$(echo "${st}" | jget last_commit.run_state)" = failed ] || stop E6 "最近批次不是失败批次（run_state=$(echo "${st}" | jget last_commit.run_state)），现场与本脚本假设不符，交调度者（可能是上次续跑做了一半）"
  seq0=$(( last_seq + 1 - 2 ))
  [ -s "${OUTDIR}/JOINT" ] || stop E6 "缺 ${OUTDIR}/JOINT（先完成 E4）"
  local joint; joint=$(cat "${OUTDIR}/JOINT")
  local cur; cur=$(cd "${D}" && PYTHONPATH="${D}" python3 -B - "${W}" <<'PY'
import sys, json
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
W = Path(sys.argv[1])
files = {"target_rule_manifest": "target-rules.json", "migration_manifest": "rule-migration.json", "scenario_manifest": "scenarios.json", "profile_manifest": "profile.json", "assertion_profile_manifest": "assertion-profile.json"}
print(cu._fingerprint({k: cu._normalized_json_sha256(json.loads((W / v).read_text())) for k, v in files.items()}))
PY
  )
  [ "${cur}" = "${joint}" ] || stop E6 "批准输入在 E4 之后又变了（联合摘要 ${cur:0:12} ≠ ${joint:0:12}）；对账的阶段幂等证明绑定输入摘要，须重做 E4→E5"
  log "STEP E6 重派 SEQ0=${seq0}（批次 $((seq0+2)) 预览 → 离线判据门 → 批次 $((seq0+3)) 批准 → 批次 $((seq0+4)) VC-3） JOINT=${joint:0:12}…"
  local out="${RR}/vc2-resume.out"
  [ -f "${out}" ] && mv "${out}" "${out}.$(date -u +%Y%m%dt%H%M%Sz)"
  ( cd "${DRV}" && SEQ0=${seq0} setsid -f bash -c "bash '${PIPE_HOME}/vc2/vc2-approve-and-stage-seq.sh' '${joint}'; echo \"EVO_RESUME_DONE rc=\$?\"" > "${out}" 2>&1 < /dev/null )
  local t0 rc=1; t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -lt $(( 40 * 60 )) ]; do
    if grep -q '^EVO_RESUME_DONE rc=0' "${out}" 2>/dev/null; then rc=0; break; fi
    if grep -q '^EVO_RESUME_DONE rc=' "${out}" 2>/dev/null; then rc=3; break; fi
    sleep 20
  done
  [ ${rc} -eq 0 ] || stop E6 "续跑未以 EVO_RESUME_DONE rc=0 结束（rc=${rc}：1=超时 3=失败），见 ${out}；失败后不要重派，先看 reconcile 再决定（同根因上限 2）"
  local st2; st2=$(state_json)
  [ "$(echo "${st2}" | jget checkpoints.2)" = True ] || stop E6 "续跑结束但没有 vc-2-checkpoint"
  [ "$(echo "${st2}" | jget checkpoints.3)" = True ] || stop E6 "续跑结束但没有 vc-3-checkpoint"
  [ -f "${D}/control/${CAMPAIGN_PREFIX}-vc3-candidate-catalog-${ROUND}-${STAMP}/catalog-stage-receipt.json" ] || stop E6 "缺 VC-3 暂存收据 catalog-stage-receipt.json"
  log "STEP E6 VC-2 批准与 VC-3 暂存完成：vc-2-checkpoint、vc-3-checkpoint、catalog-stage-receipt 都在（${out}）"
}

step_E7() {
  log "STEP E7 移交：VC-2/VC-3 已续跑完成。下一步（按序）："
  log "NOTE 1) 写 PROFILE_DIGEST：bash ${PIPE_HOME}/vc-pipeline.sh start --from VC3 --to VC3"
  log "NOTE 2) 后台验证（新部署上跑；与 VC-4 互斥，建议趁本机建候选链的空档先跑）：见 EVO.md 第 6 节命令"
  log "NOTE 3) 本机建候选链（driver/local/local-candidate-chain.sh），再 vc4-handoff.sh"
}

rank_of() { case "$1" in E0) echo 0;; E1) echo 1;; E2|E3) echo 2;; E4) echo 4;; E5) echo 5;; E6) echo 6;; E7) echo 7;; esac; }
run_body() {
  mkdir -p "${OUTDIR}"
  if ! mkdir "${PREP}/evolve.lock" 2>/dev/null; then echo "evolve.lock 已存在（另一实例在跑，或上次异常退出；确认没有进程后用 rmdir ${PREP}/evolve.lock）"; exit 1; fi
  trap 'rc=$?; log "EXIT rc=${rc} | 根盘 $(disk_txt)"; rmdir "${PREP}/evolve.lock" 2>/dev/null' EXIT
  log "START EVO 工具演进并续跑 VC-2 | 新头 ${HEAD:0:9} | from=${FROM} | approver=已传 | 根盘 $(disk_txt)"
  local from_rank s
  from_rank=$(rank_of "${FROM}")
  # E0 每次都跑（加载变量、检查现场）；其余步骤从 --from 起，且每步自己按现场判定是否已完成
  for s in E0 E1 E2 E4 E5 E6 E7; do
    if [ "${s}" != E0 ] && [ "$(rank_of "${s}")" -lt "${from_rank}" ]; then continue; fi
    log "STAGE ${s}"
    case "${s}" in E0) step_E0;; E1) step_E1;; E2) step_E2E3;; E4) step_E4;; E5) step_E5;; E6) step_E6;; E7) step_E7;; esac
    log "PASS ${s}"
  done
  log "DONE EVO 全部步骤完成"
}

case "${MODE}" in
  check) load_env || { echo "env.sh 解析失败"; exit 2; }; do_check; exit $? ;;
  run)   load_env || { echo "env.sh 解析失败"; exit 2; }; run_body; exit 0 ;;
  apply)
    sigign_ok || { echo "继承了忽略 SIGHUP（nohup 起的），拒绝"; exit 2; }
    load_env || { echo "env.sh 解析失败"; exit 2; }
    [ -n "${HEAD}" ] || { echo "--apply 必须给 --head"; exit 2; }
    [ -n "${APPROVER}" ] || { echo "--apply 必须给 --approver（批准人及依据，由调度者写）"; exit 2; }
    ARGS=(--run --head "${HEAD}" --approver "${APPROVER}" --from "${FROM}")
    if [ "${FOREGROUND}" = 1 ]; then exec bash "$0" "${ARGS[@]}"; fi
    mkdir -p "${OUTDIR}"; OUTF="${OUTDIR}/run-$(date -u +%Y%m%dt%H%M%Sz).out"
    setsid -f bash "$0" "${ARGS[@]}" > "${OUTF}" 2>&1 < /dev/null
    echo "已后台启动（setsid -f）；状态：tail -f ${STATUS}；输出：${OUTF}；最后一行为 EXIT rc=N 即结束" ;;
esac
