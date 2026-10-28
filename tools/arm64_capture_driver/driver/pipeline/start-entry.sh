#!/bin/bash
# 正式入口起跑脚本（采集主机，随驱动安装在 <驱动目录>/pipeline/start-entry.sh）。轮次与版本全部来自本轮参数文件 ARM64_VC_ENV。
# 正式入口 entry.sh 会建项目账本和 Formal Campaign（不可撤销地占用 Formal ID 与 STAMP），所以起跑前先做一遍自检，
# 全部通过才用 setsid -f 起后台运行；运行状态写 ${PREP}/pipeline/entry-status.txt。
#
# 用法（采集主机 root；入口门禁里有挂断检测用例，必须不带 nohup、只用 setsid -f 起，下面的 SigIgn 自检会拦住继承了忽略 SIGHUP 的 shell）：
#   bash <驱动目录>/pipeline/start-entry.sh --check   只做自检：逐项打印 PASS／WARN／FAIL，不起 entry.sh、不写密钥文件、不建任何东西
#   bash <驱动目录>/pipeline/start-entry.sh           自检通过后后台起跑（自检有 FAIL 就拒绝，退出 1）
#   （--run 是后台运行体，由本脚本自己用 setsid -f 调用，不要手工执行）
# 可选环境变量：
#   ENTRY_ARGS="…"                    原样追加给 entry.sh 的参数（缺省空＝完整入口；例：--plan 只判定不执行）
#   SKIP_BGV_CHECK=1                  明知入口提交还没有 passed 的后台验证结论、仍要起跑（把 C4b 的 FAIL 降为 WARN；入口门禁本身会对该提交重跑全集）
#   SKIP_CAPTURE_ACCOUNT_CHECK=1      明知采集账号／Key 还没就绪、仍要起跑（把 C6 的 FAIL 降为 WARN）。注意：账号与 Key 的 ID 会被建账本后的
#                                     预检 plan 冻结进 Campaign 配置，事后更换要走后继 Campaign 的重绑，所以只有确认后面会用同一对 ID 才这样做。
#
# 对账号的唯一改动：起跑前调用 capture-account-on.sh 把采集账号打开（只改它的 status／schedulable，写 ${PREP}/logs/capture-account.log）；
# 升级结束后由人执行 capture-account-off.sh 关回去。除此之外不改任何账号，不碰 sub2apiplus 容器配置，对库只做 SELECT。
# 退出码（前台）：0 已起跑或 --check 全绿；1 自检有 FAIL；2 用法错误。
set -u -o pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
# 载入本轮参数（只读解析，不执行参数文件）
pl_load_round_env || { echo "参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 1; }
pl_defaults
P=${PDIR}
STATUS=${P}/entry-status.txt
ENVF=${ARM64_VC_ENV}
COMPOSE_ENV=${PIPE_COMPOSE_ENV:-${COMPOSE_DIR}/.env}
PROBE_KEY=${PREP}/probe-real.key
CAP_ID=${CAPTURE_ACCOUNT_ID:-${CLIENT_ACCOUNT_ID:-${CODEX_ACCOUNT_ID:-}}}
CAP_GROUP=${CAPTURE_GROUP_ID:-3}
CAP_EMAIL=${CAPTURE_USER_EMAIL:-capture-user@entry.invalid}
MODE=${1:-start}
case "${MODE}" in --check) MODE=check ;; --run) MODE=run ;; start) ;; *) echo "用法：start-entry.sh [--check]"; exit 2 ;; esac

mkdir -p "${P}"
log() { echo "$(date -u +%FT%TZ) $*" >> "${STATUS}"; }
disk() { df -h / | awk 'NR==2{print $5" 已用，可用 "$4}'; }


# -------------------------------------------------------------------------------------------------
# 后台运行体：跑 entry.sh 到结束，期间每 20 分钟写一行心跳；无论怎么退出都写 EXIT 行（监视器据此判定进程已结束）
# -------------------------------------------------------------------------------------------------
if [ "${MODE}" = run ]; then
  on_exit() { rc=$?; rm -f "${PROBE_KEY}"; log "NOTE 采集账号仍是开启状态（本脚本不替你关）：升级结束后执行 bash ${PIPE_HOME}/capture-account-off.sh"; log "EXIT rc=${rc} | 根盘 $(disk)"; }
  trap on_exit EXIT
  # 防护：不得继承“忽略 SIGHUP”（nohup 起的进程会让全部子进程忽略它，test_campaign_exec_session_hangup_is_detected 必红）。SigIgn 第 1 位是 SIGHUP。
  SIGIGN=$(awk '/^SigIgn:/{print $2}' /proc/$$/status)
  if [ $(( 0x${SIGIGN} & 1 )) -ne 0 ]; then log "STOPPED 运行体继承了忽略 SIGHUP（SigIgn=${SIGIGN}），不起 entry.sh；请不带 nohup、只用 setsid -f 起"; exit 1; fi
  source "${PREP}/opening-env.sh"
  export ARM64_VC_ENV=${ENVF}
  log "STAGE ENTRY 正式入口 entry.sh ${ENTRY_ARGS:-} 开始 | 提交 ${ENTRY_COMMIT:0:9} | 总账截止 ${PROJECT_DEADLINE_UTC} | 根盘 $(disk)"
  cd "${DRV}"
  # shellcheck disable=SC2086
  bash entry.sh ${ENTRY_ARGS:-} > "${RUNROOT}/entry.out" 2>&1 < /dev/null &
  EP=$!
  log "STEP entry.sh 已起 pid=${EP}，输出 ${RUNROOT}/entry.out"
  LAST=$(date +%s)
  while kill -0 "${EP}" 2>/dev/null; do
    sleep 10
    if [ $(( $(date +%s) - LAST )) -ge 1200 ]; then
      LAST=$(date +%s)
      RUNDIR=$(ls -1dt "${RUNROOT}"/entry-runs/*/ 2>/dev/null | head -1)
      LOGF=$( [ -n "${RUNDIR}" ] && ls -1t "${RUNDIR}" 2>/dev/null | head -1 )
      log "WAIT entry.sh 运行中 | 根盘 $(disk) | 最近日志 ${RUNDIR:-无}${LOGF:-}"
    fi
  done
  wait "${EP}"; RC=$?
  STOP_LINE=$(grep -E '^停在：|^ENTRY_RUN_DONE' "${RUNROOT}/entry.out" | tail -2 | tr '\n' ' ' | cut -c1-400)
  log "MILESTONE entry.sh 结束 rc=${RC}（0 全部完成或沿用 1 有步骤失败 2 用法或配置错误 3 被阻塞或已有编排在运行）| ${STOP_LINE}"
  if [ "${RC}" = 0 ]; then log "DONE 正式入口完成：Formal Campaign 已建，VC-0 收口通过"; fi
  exit "${RC}"
fi

# -------------------------------------------------------------------------------------------------
# 前台：起跑前自检
# -------------------------------------------------------------------------------------------------
FAILS=0
pass() { echo "PASS  $1"; }
warn() { echo "WARN  $1"; }
fail() { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }

# C1 SigIgn：本 shell 不得忽略 SIGHUP（见上）
SIGIGN=$(awk '/^SigIgn:/{print $2}' /proc/$$/status)
if [ $(( 0x${SIGIGN} & 1 )) -ne 0 ]; then fail "C1 SigIgn=${SIGIGN}：本 shell 忽略 SIGHUP（多半是 nohup 起的），入口门禁的挂断检测用例必红；换一个干净的 ssh 会话再来"; else pass "C1 SigIgn=${SIGIGN}（未忽略 SIGHUP）"; fi

# C2 机器空闲：没有完整链空跑、门禁、后台验证、别的入口编排在跑（本脚本自己的命令行不含这些词）
BUSY=$(pgrep -af "[c]hain-rehearsal|[u]nit_executor|[e]ntry-gates|[b]ackground_validation|[b]ackground-validate|[e]ntry_orchestrator|[e]ntry-dryrun|[e]ntry_dryrun" | grep -v "start-entry.sh" | cut -c1-120 || true)
if [ -n "${BUSY}" ]; then fail "C2 机器上有别的重任务在跑，等它结束再起："; echo "${BUSY}" | sed 's/^/        /' | head -6; else pass "C2 没有完整链空跑／门禁／后台验证／入口编排在跑"; fi

# C3 项目总账截止与参数文件一致，且装得下一整轮阶段预算
LEDGER_HEAD=${D}/evidence/campaigns/upgrade-project-ledger/head.json
DL_CHECK=$(python3 -B - "${LEDGER_HEAD}" "${PROJECT_DEADLINE_UTC}" "${STAGE_BUDGETS}" <<'PY'
import json, re, sys
from datetime import datetime, timezone
head = json.load(open(sys.argv[1], encoding="utf-8"))
effective = head["effective_absolute_deadline_utc"]
budget = sum(int(m) for m in re.findall(r"=(\d+)", sys.argv[3]))
left = (datetime.fromisoformat(effective.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() / 60
state = "ok" if effective == sys.argv[2] and left >= budget + 60 else "bad"
print(state, effective, sys.argv[2], budget, int(left))
PY
)
set -- ${DL_CHECK}
if [ "$1" = ok ]; then pass "C3 总账有效截止 $2 = 参数文件 $3；剩 $5 分钟 ≥ 阶段预算合计 $4 分钟 + 60 分钟余量"; else fail "C3 总账有效截止/参数文件/余量不符：总账 $2，参数文件 $3，阶段预算合计 $4 分钟，剩 $5 分钟（需 ≥ 预算 + 60）"; fi

# C4 入口 bundle 里的分支头就是 ENTRY_COMMIT，且该提交有“通过”的后台验证结论
HEADS=$(git bundle list-heads "${ENTRY_BUNDLE}" 2>/dev/null || true)
if echo "${HEADS}" | grep -q "^${ENTRY_COMMIT} refs/heads/${ENTRY_BRANCH}$"; then pass "C4a 入口 bundle 的 ${ENTRY_BRANCH} 头 = ${ENTRY_COMMIT:0:12}"; else fail "C4a 入口 bundle 缺少 ${ENTRY_BRANCH} @ ${ENTRY_COMMIT:0:12}（bundle：${ENTRY_BUNDLE}）"; fi
BGV_OK=$(python3 -B - "${RUNROOT}/background-validation" "${ENTRY_COMMIT}" "${ENTRY_BRANCH}" "${ENTRY_BUNDLE}" <<'PY'
import glob, json, sys
root, commit, branch, bundle = sys.argv[1:5]
hits = []
for path in sorted(glob.glob(root + "/*.json")):
    if path.endswith(".core-deviation.json"):
        continue
    try:
        data = json.load(open(path, encoding="utf-8"))
    except ValueError:
        continue
    if data.get("commit") == commit and data.get("branch") == branch and data.get("bundle") == bundle and data.get("status") == "passed":
        hits.append(path.rsplit("/", 1)[-1] + " " + str(data.get("completed_at_utc")))
print(hits[-1] if hits else "")
PY
)
if [ -n "${BGV_OK}" ]; then pass "C4b 后台验证 passed 结论：${BGV_OK}"
elif [ "${SKIP_BGV_CHECK:-0}" = 1 ]; then
  # 10-27 RUN3 路加：入口自己的 entry-gates 步骤会对同一提交重跑全集，且失败发生在建 Formal Campaign（vc0-closeout）之前，
  # 所以没有后台验证结论时，C4b 只是“省一次重复全集”的前置，不是安全前提；明确知情时才降为 WARN。
  # 注意：VC-5 验收的 require-passed 仍要求“当前部署”有 passed 后台验证，这里跳过不等于免了那一次。
  warn "C4b 找不到绑定 ${ENTRY_COMMIT:0:12}／${ENTRY_BRANCH}／该 bundle 的 passed 后台验证结论，但 SKIP_BGV_CHECK=1，按要求继续（入口门禁会对该提交重跑全集）"
else fail "C4b 找不到绑定 ${ENTRY_COMMIT:0:12}／${ENTRY_BRANCH}／该 bundle 的 passed 后台验证结论"; fi

# C5 根盘停线：已用 ≤69% 且可用 ≥ ${MIN_FREE_GIB}G（入口门禁全集约 +8.5G；超线先跑 entry-dryrun.sh housekeeping）
USED=$(df / | awk 'NR==2{gsub("%","",$5); print $5}'); AVAIL=$(df -BG / | awk 'NR==2{gsub("G","",$4); print $4}')
if [ "${USED}" -le 69 ] && [ "${AVAIL}" -ge "${MIN_FREE_GIB}" ]; then pass "C5 根盘 ${USED}% 已用，可用 ${AVAIL}G（线：≤69% 且 ≥${MIN_FREE_GIB}G）"; else fail "C5 根盘 ${USED}% 已用，可用 ${AVAIL}G，超线（≤69% 且 ≥${MIN_FREE_GIB}G）：先 housekeeping/腾盘"; fi

# C6 采集账号与 Key（只读 SELECT；采集账号平时关闭，起跑时由本脚本打开）：
#    账号：存在、未删除、openai oauth、状态是 inactive（关着，待本脚本打开）或 active（已打开）、有访问令牌与刷新令牌、
#          访问令牌离过期 ≥ 60 分钟（capture-account-on.sh 同一道门槛，不够会拒绝打开，免得刷新服务轮换令牌让 DMIT 上同一登录的令牌失效）；
#    Key：存在、active、未删除、挂配置的采集分组 ${CAP_GROUP}、属于采集专用用户 ${CAP_EMAIL}，且分组里含该账号。
#    账号 ID／Key ID 会被预检 plan 冻结进 Campaign 配置，VC-5 候选采集走“Key→分组→账号”，所以这里必须对。
DBQ() { ( set -a; . "${COMPOSE_ENV}"; set +a; docker exec sub2apiplus-postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -qAtc "$1" ); }
# 账号一行：id|名称|平台|类型|状态|可调度|已删除|有访问令牌|有刷新令牌|令牌剩余分钟（布尔值经字符串拼接后是 true／false）
ACC=$(DBQ "select id||'|'||name||'|'||platform||'|'||type||'|'||status||'|'||schedulable||'|'||(deleted_at is not null)||'|'||(credentials ? 'access_token')||'|'||(credentials ? 'refresh_token')||'|'||coalesce(floor(extract(epoch from ((credentials->>'expires_at')::timestamptz - now()))/60)::text, 'null') from accounts where id = ${CAP_ID}" 2>&1 | head -1)
KEY=$(DBQ "select k.id||'|'||k.name||'|group='||coalesce(k.group_id::text,'null')||'|'||k.status||'|deleted='||(k.deleted_at is not null)||'|'||u.email from api_keys k join users u on u.id = k.user_id where k.id = ${API_KEY_ID}" 2>&1 | head -1)
INGRP=$(DBQ "select count(*) from account_groups ag join api_keys k on k.group_id = ag.group_id where k.id = ${API_KEY_ID} and ag.account_id = ${CAP_ID}" 2>&1 | head -1)
echo "      账号 ${CAP_ID}：${ACC:-查不到}"
echo "      Key ${API_KEY_ID}：${KEY:-查不到}；Key 所在分组含该账号：${INGRP:-?}"
ACC_OK=0
IFS='|' read -r A_ID A_NAME A_PLAT A_TYPE A_STATUS A_SCHED A_DEL A_HAS_ACC A_HAS_REF A_LEFT <<< "${ACC}"
if [ "${A_PLAT:-}" = openai ] && [ "${A_TYPE:-}" = oauth ] && [ "${A_DEL:-}" = false ] && { [ "${A_STATUS:-}" = inactive ] || [ "${A_STATUS:-}" = active ]; } \
   && [ "${A_HAS_ACC:-}" = true ] && [ "${A_HAS_REF:-}" = true ] && [ "${A_LEFT:-null}" != null ] && [ "${A_LEFT:-0}" -ge 60 ] 2>/dev/null; then ACC_OK=1; fi
KEY_OK=0; case "${KEY}" in *"|group=${CAP_GROUP}|active|deleted=false|${CAP_EMAIL}") KEY_OK=1 ;; esac
if [ "${ACC_OK}" = 1 ] && [ "${KEY_OK}" = 1 ] && [ "${INGRP:-0}" -ge 1 ] 2>/dev/null; then
  pass "C6 采集账号 ${CAP_ID}（${A_NAME}，现 status=${A_STATUS} schedulable=${A_SCHED}，令牌剩 ${A_LEFT} 分钟）与采集专用 Key ${API_KEY_ID}（分组 ${CAP_GROUP}）可用"
elif [ "${SKIP_CAPTURE_ACCOUNT_CHECK:-0}" = 1 ]; then
  warn "C6 采集账号／Key 不符合要求，但 SKIP_CAPTURE_ACCOUNT_CHECK=1，按要求继续"
else
  fail "C6 采集账号 ${CAP_ID}／Key ${API_KEY_ID} 不符合要求（账号需 openai oauth、未删除、inactive 或 active、两种令牌俱全且访问令牌剩 ≥60 分钟；Key 需 active、未删除、挂采集分组、属采集专用用户，分组含该账号）"
fi

# C7 开工检查的环境变量齐全（source 后核对；密钥文件由本脚本起跑时现生成，这里不检查它是否存在）
OPEN=$( ( source "${PREP}/opening-env.sh" >/dev/null 2>&1; echo "${MODEL_PROBE_URL:-}|${MODEL_PROBE_KEY_FILE:-}|${MODEL_PROBE_MINIMAL:-}|${SECURITY_SCAN_REPO:-}|${OPENING_CHECKS_SKIP:-}|$([ -n "${OPENING_CHECKS_SKIP_REASON:-}" ] && echo reason || echo noreason)" ) )
IFS='|' read -r O_URL O_KEY O_MIN O_REPO O_SKIP O_REASON <<< "${OPEN}"
if [ -n "${O_URL}" ] && [ "${O_KEY}" = "${PROBE_KEY}" ] && [ -n "${O_MIN}" ] && [ -n "${O_REPO}" ] && { [ -z "${O_SKIP}" ] || [ "${O_REASON}" = reason ]; }; then
  pass "C7 opening-env.sh：探测地址 ${O_URL}（真实网关，含最小请求），Security Scan 跳过项=[${O_SKIP}]（带理由）"
else
  fail "C7 opening-env.sh 配置不全或指向了别处：${OPEN}"
fi
case "${O_URL}" in http://127.0.0.1:18765*) fail "C7 探测地址还是本机模拟服务 18765，不是真实链路" ;; esac

# C8 Formal ID 状态：已存在说明是续跑（重新执行同一条命令按步骤记录续跑），不是错误，但要让操作员知道
if [ -e "${D}/evidence/campaigns/${NEW}" ]; then warn "C8 Formal Campaign 目录已存在（${NEW}）：这是续跑，不是首次入口"; else pass "C8 Formal Campaign ${NEW} 尚未建立（首次入口）"; fi
if [ -d "${RUNROOT}/entry-steps" ] && [ -n "$(ls -A "${RUNROOT}/entry-steps" 2>/dev/null)" ]; then warn "C8 ${RUNROOT}/entry-steps 已有步骤记录：入口会按记录沿用／续跑"; fi

# C9 官方采集登录态（capture-cli 的 auth.json）访问令牌剩余有效期：必须覆盖整个 VC-1（预计时长 + 1 小时，PIPE_TOKEN_MIN_MINUTES，缺省 150）。
#    客户端在令牌过期前约 5 分钟自己刷新，走抓包中继会得到 403；所以不足时先直连刷新一次（preflight-refresh.sh --ensure，真起跑时才执行）。
#    --check 只读：不足只报 WARN 并说明起跑时会自动刷新；读不出剩余有效期按 FAIL（不猜）。
TOKEN_MIN=${PIPE_TOKEN_MIN_MINUTES:-150}
if TOKEN_LEFT=$(bash "${PIPE_HOME}/preflight-refresh.sh" --remaining 2>/dev/null); then
  if [ "${TOKEN_LEFT}" -ge "${TOKEN_MIN}" ]; then pass "C9 官方采集登录态访问令牌剩余 ${TOKEN_LEFT} 分钟 ≥ 阈值 ${TOKEN_MIN} 分钟"
  else warn "C9 官方采集登录态访问令牌只剩 ${TOKEN_LEFT} 分钟 < 阈值 ${TOKEN_MIN} 分钟：起跑时将先直连刷新一次（preflight-refresh.sh --ensure）；刷新失败则不起跑"; fi
else
  fail "C9 读不出官方采集登录态访问令牌的剩余有效期（preflight-refresh.sh --remaining 失败）"
fi

echo "----"
if [ "${FAILS}" -gt 0 ]; then echo "自检未通过：${FAILS} 项 FAIL，不起跑"; exit 1; fi
if [ "${MODE}" = check ]; then echo "自检全部通过（--check 不起跑）"; exit 0; fi

# -------------------------------------------------------------------------------------------------
# 起跑：现生成探测密钥文件（0600；运行体退出时删除）→ setsid -f 起运行体 → 等第一行状态
# -------------------------------------------------------------------------------------------------
# 先确保官方采集登录态令牌够用：不足就直连刷新，失败则明确报错、不起跑（此时还没有任何副作用：没生成密钥文件、没开采集账号）
if ! bash "${PIPE_HOME}/preflight-refresh.sh" --ensure --min "${TOKEN_MIN}"; then
  echo "官方采集登录态令牌剩余有效期不足且直连刷新失败，不起跑（详情见上方输出与 ${RUNROOT}/preflight-refresh-*/）"; exit 1
fi
KEY_TMP=${PROBE_KEY}.tmp
DBQ "select key from api_keys where id = ${API_KEY_ID} and status = 'active' and deleted_at is null" > "${KEY_TMP}" 2>/dev/null
if [ ! -s "${KEY_TMP}" ]; then rm -f "${KEY_TMP}"; echo "取不到 Key ${API_KEY_ID} 的密钥，不起跑"; exit 1; fi
chmod 600 "${KEY_TMP}" && mv -f "${KEY_TMP}" "${PROBE_KEY}"
# 采集账号平时关闭，起跑前自动打开（capture-account-on.sh 幂等，动手前打印并写 ${PREP}/logs/capture-account.log）；打不开就不起跑。
# 升级结束后必须执行 capture-account-off.sh 关回去——本脚本和运行体都不会替你关（VC-5 之后还要用它）。
if [ -n "${CAP_ID}" ]; then
  if ! bash "${PIPE_HOME}/capture-account-on.sh"; then rm -f "${PROBE_KEY}"; echo "capture-account-on.sh 失败，不起跑（状态见 ${PREP}/logs/capture-account.log）"; exit 1; fi
  ON_STATE=$(DBQ "select status||'/'||schedulable from accounts where id = ${CAP_ID}" 2>&1 | head -1)
  if [ "${ON_STATE}" != "active/true" ]; then rm -f "${PROBE_KEY}"; echo "打开后采集账号状态是 ${ON_STATE}，不是 active/true，不起跑"; exit 1; fi
  echo "已打开采集账号 ${CAP_ID}（${ON_STATE}）；升级结束后记得执行 bash ${PIPE_HOME}/capture-account-off.sh"
else
  warn "本轮参数文件没有采集账号号，本脚本不负责打开账号"
fi
log "START 起跑脚本自检通过，提交 ${ENTRY_COMMIT:0:9}，账号 ${CAP_ID}／Key ${API_KEY_ID}，采集账号已打开，参数 ENTRY_ARGS=[${ENTRY_ARGS:-}]，SKIP_BGV_CHECK=${SKIP_BGV_CHECK:-0}"
BEFORE=$(wc -l < "${STATUS}")
ENTRY_ARGS="${ENTRY_ARGS:-}" setsid -f bash "${PIPE_HOME}/start-entry.sh" --run >> "${P}/entry-start.out" 2>&1 < /dev/null
for _ in $(seq 1 30); do [ "$(wc -l < "${STATUS}")" -ge $(( BEFORE + 2 )) ] && break; sleep 1; done
tail -n 3 "${STATUS}"
echo "已后台起跑；状态：${STATUS}；入口输出：${RUNROOT}/entry.out"
