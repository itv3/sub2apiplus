#!/bin/bash
# ARM64 抓包驱动链公共前导（被各脚本 source，不可直接执行）：
#   1. DRV = 脚本所在目录（安装目标 /root/arm64-capture-driver/driver 或仓库内 tools/arm64_capture_driver/driver）
#   2. 以 parse_env.py 安全解析本轮参数文件 ${ARM64_VC_ENV}（绝不 source：只接受精确键集合的 KEY=VALUE，
#      值里只允许引用已定义键，任何 $(…)／反引号／; 等一律拒绝；2026-09-22 审核 P1）
#   3. 建立 ${RUNROOT}，导出 D／PYTHONPATH／PATH
# 用法：source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
if [ -z "${BASH_SOURCE[1]:-}" ]; then echo "lib.sh 只能被 source"; exit 2; fi
export DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
: "${ARM64_VC_ENV:?ARM64_VC_ENV 未设置（指向本轮 env.sh，见 env.example.sh）}"
ARM64_VC_ENV_EXPORTS=$(python3 "$DRV/parse_env.py" "$ARM64_VC_ENV") || { echo "参数文件拒绝加载：$ARM64_VC_ENV"; exit 2; }
eval "$ARM64_VC_ENV_EXPORTS"; unset ARM64_VC_ENV_EXPORTS
# 阶段入口先只读解析；错误必须先于目录、预约或网络副作用。
phase_require() {
  local exports
  exports=$(python3 -B "$DRV/phase_context.py" --env "$ARM64_VC_ENV" --shell "$@") || return 3
  eval "$exports"
}
if declare -p PHASE_CONTEXT_ARGS >/dev/null 2>&1; then phase_require "${PHASE_CONTEXT_ARGS[@]}" || exit 3; fi
[ -d "$RUNROOT" ] || mkdir -p "$RUNROOT"; [ "$(python3 -c "import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))" "$RUNROOT")" = 0o700 ] || chmod 700 "$RUNROOT"
export D PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 PATH=/usr/local/go/bin:/opt/node-v20/bin:$PATH
# 共享缓存（E2-02）：数据根受管树与标准库的字节码预编译在数据根之外（源码按内容摘要失效，见 bytecode_cache.py），
# 身份五摘要与评估器四项按整树摘要做键跨进程复用（codex_upgrade_tool_identity_policy）；入口各子命令只读使用，不再
# 每个进程都从源码重编 6.3 万行的编排器、重算一遍身份。两者都在数据根之外，容器看不到。字节码缓存还没建（新机器、
# 刚清理）时不导出前缀，行为与原来相同；由入口便宜检查（entry-preflight.sh）重建。
PYC_MANAGED=$(dirname "$D")/pycache-managed
export CODEX_UPGRADE_IDENTITY_MEMO
CODEX_UPGRADE_IDENTITY_MEMO=$(dirname "$D")/identity-memo
# 共享层存在时在调用方的 shell 里导出前缀：source 本文件时调一次；入口脚本跑完便宜检查（子进程里刚准备好共享层）再调一次。
use_managed_bytecode() { if [ -d "$PYC_MANAGED" ]; then export PYTHONPYCACHEPREFIX="$PYC_MANAGED"; fi; }
use_managed_bytecode
# 重建字节码共享层：数据根受管树的内容摘要与上次预编译时相同就沿用，否则清空重建（此时不应有别的进程在用它）。
# 失败只告警、不中断：缓存缺失时各子命令回落为从源码编译，正确性不受影响。
prepare_managed_bytecode() {
  local digest summary rc=0
  # 身份记忆化条目只增不改：入口时清掉 7 天前的（键随受管树变化，旧条目不会再命中）。
  if [ -d "$CODEX_UPGRADE_IDENTITY_MEMO" ]; then find "$CODEX_UPGRADE_IDENTITY_MEMO" -type f -mtime +7 -delete 2>/dev/null || true; fi
  digest=$(python3 -B - "$D/tools" <<'PY'
import hashlib, sys
from pathlib import Path
root, total = Path(sys.argv[1]), hashlib.sha256()
for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".json", ".sh"} and "__pycache__" not in p.parts):
    total.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
print(total.hexdigest())
PY
) || digest=""
  if [ -n "$digest" ] && [ -d "$PYC_MANAGED" ] && [ "$(cat "$PYC_MANAGED/.tools-digest" 2>/dev/null || true)" = "$digest" ]; then
    export PYTHONPYCACHEPREFIX="$PYC_MANAGED"
    echo "字节码共享层沿用（数据根受管树未变）：$PYC_MANAGED"
    return 0
  fi
  # 不经管道取退出码：调用方没开 pipefail 时，管道的退出码是 tail 的。
  summary=$(env -u PYTHONPATH -u PYTHONPYCACHEPREFIX python3 "$DRV/bytecode_cache.py" "$PYC_MANAGED" "$D/tools") || rc=$?
  summary=$(printf '%s\n' "$summary" | tail -n 1)
  if [ "$rc" -ne 0 ]; then
    unset PYTHONPYCACHEPREFIX
    # 不截断：失败原因在摘要的 failed_sources／leaked_pycache 里（10-01 数据根有两个旧 __pycache__，截断后看不到）。
    echo "字节码共享层重建失败（各子命令回落为从源码编译）：${summary}" >&2
    return 0
  fi
  printf '%s\n' "$digest" > "$PYC_MANAGED/.tools-digest"
  export PYTHONPYCACHEPREFIX="$PYC_MANAGED"
  echo "字节码共享层已重建：${summary:0:200}"
}
# 新签本轮 pre-A3 路径认证（pre-a3.sh 新跑与 stage2.sh 兜底共用）。E3-02 起交给入口门禁的 pre-a3 组合：在本提交的测试树
# 里算每个场景的输入、核对数据根部署的就是这棵树，统一调度执行器并行跑场景、单元执行记录入库（已通过且输入没变的场景
# 承接），再从本次运行清单与记录库组装 v2 认证（每个场景恰好一条通过的正式执行记录、网络计数为 0）。测试树取自参数文件
# 的 ENTRY_BUNDLE、ENTRY_BRANCH、ENTRY_COMMIT（入口编排器同一组参数），缺任何一个即停：没有测试树就算不出输入，签不出
# 「输入对得上当前树」的认证。没通过只写带时间后缀的旁路文件、正式路径只在通过时写，修好后同一 STAMP 直接重跑；
# entry-gates.sh 的退出码非 0 时调用方的 set -e 停线。用到调用方已定义的 POLICY_ACTIVATION、PRE_A3_CERTIFICATION。
issue_pre_a3_certification() {
  local name
  for name in ENTRY_BUNDLE ENTRY_BRANCH ENTRY_COMMIT; do
    if [ -z "${!name:-}" ]; then echo "单独签 pre-A3 需要参数文件里的 ${name}（入口门禁的测试树来源）" >&2; return 2; fi
  done
  bash "$DRV/entry-gates.sh" --profile pre-a3 --policy-activation "$POLICY_ACTIVATION" --pre-a3-certification "$PRE_A3_CERTIFICATION" \
    --pre-a3-mode run "$ENTRY_BUNDLE" "$ENTRY_BRANCH" "$ENTRY_COMMIT" < /dev/null
}
# 根坐标由 parse_env.py 统一导出，旧环境中的同名值不能覆盖本轮解析结果。
# 正式候选的身份仍须经 round_context.py 和各阶段原有收据合同重放。
# 下一批次序号：按 control/vc/batches 中已 COMMIT 的最大序号 +1
next_seq() { python3 -c "import glob,os,sys; xs=[int(os.path.basename(p).split('-')[0]) for p in glob.glob(sys.argv[1]+'/control/vc/batches/*.json')]; print(max(xs)+1 if xs else 1)" "$NEWDIR"; }
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# VC-5 入口只消费已批准准入；不再在只读预检、收尾或重跑入口隐式续签。
vc5_require_admission() {
  local parameters
  parameters=$(bash "$DRV/vc5-precheck.sh" --consume --export-parameters) || return 3
  eval "$parameters"
  export ADMIN_BEARER_TOKEN_FILE="$D/state/$UP/admin-token"
}
# start/recover 共享同一文件描述符锁；后台批次继承描述符，直到批次退出才释放。
# 锁文件已有权限或属主不合同时拒绝，绝不通过 chmod 修正陌生锁。
vc5_dispatch_lock() {
  local lock="$RUNROOT/vc5-dispatch.lock"
  python3 -B - "$lock" <<'PY' || return 3
import os, stat, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_absolute() or ".." in p.parts or any(q.is_symlink() for q in (p, *p.parents)):
    sys.exit("VC-5 派发锁路径不可信")
try:
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
except FileExistsError:
    info = p.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        sys.exit("VC-5 派发锁属主、模式或类型不合合同")
else:
    os.close(fd)
PY
  exec 9< "$lock" || return 3
  python3 -B - "$lock" <<'PY' || return 3
import fcntl, os, stat, sys
from pathlib import Path
p = Path(sys.argv[1]); info = os.fstat(9); actual = p.lstat()
if (any(q.is_symlink() for q in (p, *p.parents)) or not stat.S_ISREG(info.st_mode)
        or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)
        or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
    sys.exit("VC-5 派发锁发生路径、属主或模式漂移")
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit("已有 VC-5 派发或恢复批次持锁，禁止抢派")
PY
}
# 候选测试树（VC-5 目标平台门禁的 gates.sh prepare 与 VC-0 预跑 vc0-gate-target.sh 共用同一段实现）：
#   从完整历史测试树 $HISTORY_TEST_TREE 克隆 → 从 bundle 取分支 → 分离 HEAD 检出指定提交 → 断言。
#   测试树必须带完整 Git 历史（上游合并／历史漂移冻结测试要读基准提交），所以从完整历史测试树克隆，不能只 fetch bundle；
#   且不注入 vendor（版本泄漏 AST 门禁会扫描 backend/vendor，Go 依赖改走 GOMODCACHE，与 CI／本机一致）。
#   前端 node_modules 由调用方注入（VC-5 取本轮前端构建，VC-0 取前序测试树里 lockfile 相同的一份）。
# 用法：clone_test_tree <树目录（先删后建）> <bundle> <分支> <提交>；任一步失败返回 1（调用方在 set -e 下即停）。
verify_history_tree() {
  local tree="$1" commit="${2:-}"
  test "$(git -C "$tree" rev-parse --is-shallow-repository)" = false || { echo "拒绝浅克隆：$tree" >&2; return 1; }
  test "$(git -C "$tree" rev-list --count HEAD)" -gt 10000 || { echo "测试树不是完整历史（提交数须 >10000）：$tree" >&2; return 1; }
  if [ -n "$commit" ]; then
    test "$(git -C "$tree" rev-parse HEAD)" = "$commit" || { echo "树头与指定提交不一致：$tree" >&2; return 1; }
  fi
  test ! -e "$tree/backend/vendor" && test ! -L "$tree/backend/vendor" || { echo "测试树不得含 backend/vendor：$tree" >&2; return 1; }
}
clone_test_tree() {
  local tree="$1" bundle="$2" branch="$3" commit="$4"
  # 先拒绝有问题的历史来源，避免检查失败时删掉原测试树。
  verify_history_tree "$HISTORY_TEST_TREE" || return 1
  rm -rf "$tree" || return 1
  git clone -q --no-checkout "$HISTORY_TEST_TREE" "$tree" || return 1
  git -C "$tree" fetch -q "$bundle" "$branch" || return 1
  git -C "$tree" checkout -q --detach "$commit" || return 1
  verify_history_tree "$tree" "$commit" || return 1
}
# 隔离执行门禁命令（ARM64 全量门禁 arm64-full-gates.sh 与 ARM64 版 VC-4 门禁 arm64-vc4-gates.sh 共用）：
#   隔离方式与 vc5-gate-target.sh 的目标平台门禁逐字相同——在私有挂载命名空间里用只读空 tmpfs 遮住 /root/oauth-capture
#   （采集主机上受管工具树的 bind 别名，候选测试树会把它当执行副本比对），树外只读字节码缓存由调用方先用
#   bytecode_cache.py 重建后传入；历史门禁源码与 TypeScript 解析器路径同目标平台门禁。
#   标准输出、标准错误分别写入两个文件；返回命令退出码（调用方用 `|| RC=$?` 承接，不受 set -e 影响）。
# 用法：isolated_run <测试树> <树内相对工作目录> <字节码缓存目录> <标准输出文件> <标准错误文件> <命令…>
isolated_run() {
  local tree="$1" workdir="$2" pyc="$3" out="$4" err="$5" rc=0
  shift 5
  # 门禁跑的是测试树：字节码用调用方给的测试树前缀；身份记忆化不混用生产缓存，交给统一调度执行器在本次记录目录里新建。
  ( cd "$tree/$workdir" && unset CODEX_UPGRADE_IDENTITY_MEMO && export PYTHONPYCACHEPREFIX="$pyc" CODEX_0_149_1_SOURCE_ROOT="$HISTORICAL_SOURCE_ROOT" \
      CAPTURE_TYPESCRIPT_MODULE="$tree/frontend/node_modules/typescript/lib/typescript.js" \
      && unshare -m --propagation private bash -c 'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec "$@"' isolated-gate "$@" ) \
    > "$out" 2> "$err" || rc=$?
  return "$rc"
}
# 门禁记录（gate.json）：字段与本机 local-gate.sh／local-full-regression.sh 逐字段相同（build_gate_facts.py 只读
# command、working_directory、host、architecture、起止时间与退出码），另记测试树、树头提交与隔离方式。
# 用法：write_gate_json <输出文件> <门禁 ID> <起始 UTC> <结束 UTC> <退出码> <测试树> <工作目录> <命令…>
write_gate_json() {
  local out="$1" gate_id="$2" start="$3" end="$4" rc="$5" tree="$6" workdir="$7" head
  shift 7
  # 单独赋值而不是写进参数里的命令替换：set -E 下失败要走调用方的 ERR 陷阱，不能把陷阱输出当成树头提交写进记录。
  head=$(git -C "$tree" rev-parse HEAD)
  python3 - "$out" "$gate_id" "$start" "$end" "$rc" "$tree" "$workdir" "$head" "$@" <<'PY'
import json, platform, socket, sys
out, gate_id, start, end, rc, tree, workdir, head, *command = sys.argv[1:]
payload = {"gate_id": gate_id, "command": command, "working_directory": workdir,
           "host": socket.gethostname().split(".")[0], "architecture": f"{platform.system().lower()}/{platform.machine()}",
           "started_at_utc": start, "completed_at_utc": end, "exit_code": int(rc), "tree": tree, "tree_head": head,
           "isolation": "unshare -m --propagation private; tmpfs(ro) over /root/oauth-capture (host managed-tool alias hidden)"}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
PY
  chmod 600 "$out"
}
# 管理 token 自动续签（修好接着跑第 33 项）：候选采集用的 admin JWT 由 JWT_EXPIRE_HOUR（默认 24 小时）控制，VC-5 预检要求
# 剩余 ≥1800 秒，而 run／seal／accept／canonical 全程可能跨越十几个小时。剩余不足 ${ADMIN_TOKEN_MIN_SECONDS:-43200} 秒（12 小时）
# 或传入 force 时，按 vc23.sh 同一方式在服务容器内重签（旧 token 改名留档、只输出剩余分钟、绝不输出 token 本身）。
# 用法：ensure_admin_token [force]
ensure_admin_token() {
  local st="$D/state/$UP" min="${ADMIN_TOKEN_MIN_SECONDS:-43200}" remaining=0
  if [ "${1:-}" = "force" ]; then
    echo "管理 token：force 重签"
  elif [ ! -s "$st/admin-token" ]; then
    # 首次签发一直是 vc23.sh 的事；续签入口遇到缺失只提示，交给后续预检失败关闭（stub／只读场景不触碰签发环境）。
    echo "管理 token 缺失：本入口不签发（由 vc23.sh 首次签发，后续预检失败关闭）"; return 0
  else
    remaining=$(python3 -c "
import base64,json,sys,time
t=open(sys.argv[1]).read().strip(); p=t.split('.')[1]; p+='='*(-len(p)%4); d=json.loads(base64.urlsafe_b64decode(p)); print(max(0, int(d['exp'])-int(time.time())))" "$st/admin-token") || { echo "管理 token 无法解码，失败关闭"; return 3; }
    if [ "$remaining" -ge "$min" ]; then echo "管理 token 剩余 $((remaining/60)) 分钟（≥ $((min/60)) 分钟，不重签）"; return 0; fi
    echo "管理 token 剩余 $((remaining/60)) 分钟 < $((min/60)) 分钟，重签"
  fi
  mkdir -p "$st"; chmod 700 "$st"
  if [ -e "$st/admin-token" ]; then mv "$st/admin-token" "$st/admin-token.superseded-$(date -u +%Y%m%dt%H%M%Sz)"; fi
  ( cd "$COMPOSE_DIR" && set -a && . ./.env && set +a; DATABASE_HOST="$(docker inspect sub2apiplus-postgres --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")" DATABASE_PORT=5432 DATABASE_USER="$POSTGRES_USER" DATABASE_PASSWORD="$POSTGRES_PASSWORD" DATABASE_DBNAME="$POSTGRES_DB" DATABASE_SSLMODE=disable JWT_SECRET="$JWT_SECRET" JWT_EXPIRE_HOUR="${JWT_EXPIRE_HOUR:-24}" timeout 30 "$JWTGEN_BIN" -email "$ADMIN_EMAIL" 2>/dev/null | sed -n "s/^JWT=//p" | head -1 ) > "$st/admin-token.tmp"
  test -s "$st/admin-token.tmp"; printf "%s" "$(cat "$st/admin-token.tmp")" > "$st/admin-token"; rm -f "$st/admin-token.tmp"; chmod 400 "$st/admin-token"
  python3 -c "
import base64,json,sys,time
t=open(sys.argv[1]).read().strip(); p=t.split('.')[1]; p+='='*(-len(p)%4); d=json.loads(base64.urlsafe_b64decode(p)); print('token exp 剩余分钟:', (int(d['exp'])-int(time.time()))//60)" "$st/admin-token"
}
# 等待失败统一退出 3；只退出当前驱动，不改 Campaign／账本或猜测恢复分支。
# 用法：wait_for_marker <文件> <正则> <总秒数> [PID] [wait_state.py 选项…]
# PID 可选：第 4 个参数不以 -- 开头时才按 PID 取走（空串表示不绑定 PID）；省略 PID 直接跟 --log 等选项时，
# 选项原样交给 wait_state.py，不会被误当成 PID。非空 PID 必须是正整数，否则直接按等待失败退出 3。
wait_for_marker() {
  local file="$1" regex="$2" max_seconds="$3" pid=""
  shift 3
  if [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ]; then pid="$1"; shift; fi
  if [ -n "$pid" ] && ! [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
    echo "等待失败：PID 必须是正整数：$pid" >&2
    exit 3
  fi
  local args=(marker "$file" --regex "$regex" --max-seconds "$max_seconds")
  if [ -n "$pid" ]; then args+=(--pid "$pid"); fi
  python3 "$DRV/wait_state.py" "${args[@]}" "$@" || exit 3
}
wait_for_heartbeat() {
  local file="$1" stale_seconds="$2" max_seconds="$3"
  shift 3
  python3 "$DRV/wait_state.py" heartbeat "$file" --stale-seconds "$stale_seconds" --max-seconds "$max_seconds" "$@" || exit 3
}
# 沿用参数文件中的阶段预算，并在每次重起时仍受项目绝对截止约束。
wait_budget() {
  python3 - "$1" "$STAGE_BUDGETS" "$PROJECT_DEADLINE_UTC" <<'PY'
import datetime, sys, time
phase, budgets, deadline = sys.argv[1:]
minutes = dict(item.split('=', 1) for item in budgets.split())
remaining = datetime.datetime.fromisoformat(deadline.replace('Z', '+00:00')).timestamp() - time.time()
print(max(1, int(min(float(minutes.get(phase, '90'))*60, remaining))))
PY
}
