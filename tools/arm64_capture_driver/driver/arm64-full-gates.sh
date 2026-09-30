#!/bin/bash
# ARM64 全量门禁：受管工具每轮修复的部署前提、版本登记变更集与升级收尾的验证，全部在采集主机的隔离测试树上执行，
# 不在本机跑测试。门禁项与 CI（backend-ci.yml）逐项对齐，依次执行、全部跑完再下结论：
#   1. full-regression：make test（后端 go test 与不带标签的 golangci-lint、前端检查、采集工具全量、check-egress-spec 等）；
#   2. backend-unit：backend 下 make test-unit；
#   3. backend-integration：backend 下 make test-integration（采集主机有 Docker，集成测试真实执行，不跳过）；
#   4. lint-unit、lint-integration：backend 下 golangci-lint run --timeout=30m --build-tags=unit／integration；
#   5. deploy-scripts：CI shell 作业与 test 作业里的部署脚本测试（从测试树的 backend-ci.yml 逐行取出，不在此写死）。
# 每项都用 lib.sh 的 isolated_run 执行（与目标平台门禁同一隔离方式：私有挂载命名空间里遮住 /root/oauth-capture 别名，
# 树外只读字节码缓存）。测试树用 lib.sh 的 clone_test_tree（完整历史、不含 vendor），前端依赖取 lockfile 相同的一份。
#
# 结论只写 $RUNROOT/full-gates/<主体标识>/（summary.json 与 logs/ 下各项的 stdout／stderr／gate.json），是部署前提与验证
# 记录，不是 Campaign 收据：不写候选门禁目录、候选目录、时间账本与 Campaign，零模型请求。必须单独运行，不与目标平台
# 门禁、VC-1／VC-5 采集并行（采集主机只有 4 核，资源争用会把计时用例拖红）。
#
# 用法（采集主机 root；make test 里有挂断检测用例，必须 setsid -f 启动，不能 nohup）：
#   ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash arm64-full-gates.sh <bundle> <分支> <40 位提交> [<前端依赖目录>] \
#     > $RUNROOT/full-gates.out 2>&1 < /dev/null
#   bundle：本机 git bundle create <文件> <BASE>..<分支>，BASE 必须在 $HISTORY_TEST_TREE 的历史里；
#   前端依赖目录：同时含 node_modules 与 pnpm-lock.yaml 的目录，默认 $HISTORY_TEST_TREE/frontend。
# 退出码：0 全部通过；1 至少一项门禁未通过（门禁结论，测试树保留供排查）；2 用法错误；3 准备或执行失败（bundle、
#   测试树、前端依赖、字节码缓存、并发锁），没有门禁结论。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
usage() { echo "用法：bash arm64-full-gates.sh <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }
if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then usage; exit 2; fi
PBUNDLE="$1"; PBRANCH="$2"; PCOMMIT="$3"; NM_DIR="${4:-$HISTORY_TEST_TREE/frontend}"
if [[ "$PBUNDLE" != /* ]] || [ -L "$PBUNDLE" ] || [ ! -f "$PBUNDLE" ]; then echo "bundle 必须是已存在的普通文件（绝对路径）：$PBUNDLE" >&2; exit 2; fi
if ! [[ "$PBRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then echo "分支名只允许字母、数字与 ._/-：$PBRANCH" >&2; exit 2; fi
if ! [[ "$PCOMMIT" =~ ^[0-9a-f]{40}$ ]]; then echo "提交必须是完整 40 位小写 sha1：$PCOMMIT" >&2; exit 2; fi
if [[ "$NM_DIR" != /* ]]; then echo "前端依赖目录必须是绝对路径：$NM_DIR" >&2; exit 2; fi
FG="$RUNROOT/full-gates"; TREE="$FG/test-tree"; PYC="$FG/pycache"; OUT=""
on_error() {
  local rc=$? line="$1"
  trap - ERR
  echo "FULL_GATES_ABORTED：准备或执行失败（arm64-full-gates.sh 第 ${line} 行，rc=${rc}），没有门禁结论；原因见上方输出，主体目录 ${OUT:-（未建立）}"
  exit 3
}
trap 'on_error $LINENO' ERR
mkdir -p "$FG"; chmod 700 "$FG"
# 同一轮只允许一个全量门禁（共用测试树与缓存）：mkdir 原子锁，持有者已不在时回收陈旧锁（与 vc0-gate-target.sh 同一做法）。
LOCK="$FG/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then echo "FULL_GATES_ABORTED：已有全量门禁在运行（PID ${HOLDER}），拒绝并发"; exit 3; fi
  rm -rf "$LOCK"; mkdir "$LOCK"
fi
printf '%s\n' "$$" > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT
SUBJECT="full-gates-$(date -u +%Y%m%dt%H%M%Sz)"
if [ -e "$FG/$SUBJECT" ]; then SUBJECT="$SUBJECT-$$"; fi
OUT="$FG/$SUBJECT"
mkdir -m 0700 "$OUT" "$OUT/logs"
echo "=== ARM64 全量门禁 ${SUBJECT}（部署前提与验证记录，不是 Campaign 收据）$(utc_now)"
echo "bundle=${PBUNDLE} 分支=${PBRANCH} 提交=${PCOMMIT} 前端依赖=${NM_DIR}"
echo "=== 测试树（clone_test_tree，与 VC-5／VC-0 预跑同一实现，umask 022）$(utc_now)"
umask 022
clone_test_tree "$TREE" "$PBUNDLE" "$PBRANCH" "$PCOMMIT"
if [ ! -d "$NM_DIR/node_modules" ] || ! cmp -s "$NM_DIR/pnpm-lock.yaml" "$TREE/frontend/pnpm-lock.yaml"; then
  echo "前端依赖不可用：${NM_DIR}/node_modules 不存在，或 ${NM_DIR}/pnpm-lock.yaml 与本树 frontend/pnpm-lock.yaml 不同"
  echo "本轮 lockfile 有变化时，先按 frontend.sh 同一方式（node:20 容器内 pnpm install --frozen-lockfile）在独立目录装好依赖，再把该目录作为第 4 个参数传入"
  echo "FULL_GATES_ABORTED：前端依赖不可用，没有门禁结论；主体目录 ${OUT}"
  exit 3
fi
cp -a "$NM_DIR/node_modules" "$TREE/frontend/node_modules"
umask 077
TREE_HEAD=$(git -C "$TREE" rev-parse HEAD)
# 命令替换里一律 `|| true`：set -E 会把 ERR 陷阱带进命令替换，head 截断触发的 SIGPIPE 不能误判为准备失败。
tree_status() { git -C "$TREE" status --porcelain --untracked-files=all 2>&1 | head -n "${1:-1000000}" || true; }
echo "test-tree HEAD=${TREE_HEAD} status=[$(tree_status)]"
# 部署脚本测试从测试树自己的 CI 定义逐行取出（shell 作业与 test 作业里以 /bin/sh 或 /bin/bash 执行 deploy/ 下脚本的行，
# 含与 `run:` 写在同一行的单行写法）。
# 进程替换里的 grep 必须以 `|| true` 收住：set -E 会把 ERR 陷阱带进进程替换，文件缺失时陷阱输出的中止信息会被当成
# 命令读进数组；读到的每一行再按同一正则逐条校验，校验不过即准备失败，绝不把非预期内容当命令执行。
mapfile -t DEPLOY_TESTS < <({ grep -oE '^[[:space:]]*(run:[[:space:]]*)?/bin/(ba)?sh( -n)? deploy/[A-Za-z0-9._/-]+' "$TREE/.github/workflows/backend-ci.yml" 2>/dev/null || true; } | sed -E 's/^[[:space:]]*(run:[[:space:]]*)?//')
[ "${#DEPLOY_TESTS[@]}" -gt 0 ] || { echo "backend-ci.yml 里没有取到部署脚本测试"; false; }
for cmd in "${DEPLOY_TESTS[@]}"; do
  [[ "$cmd" =~ ^/bin/(ba)?sh(\ -n)?\ deploy/[A-Za-z0-9._/-]+$ ]] || { echo "部署脚本测试命令不合法：${cmd}"; false; }
done
echo "=== 树外字节码缓存 ${PYC} $(utc_now)"
env -u PYTHONPATH python3 "$DRV/bytecode_cache.py" "$PYC" "$TREE/tools" | tail -n 1 | cut -c1-300
RESULTS=()
FAILED=()
run_gate() {  # <门禁 ID> <树内工作目录> <命令…>
  local id="$1" workdir="$2" start end rc=0
  shift 2
  start=$(utc_now)
  isolated_run "$TREE" "$workdir" "$PYC" "$OUT/logs/$id.stdout.log" "$OUT/logs/$id.stderr.log" "$@" || rc=$?
  end=$(utc_now)
  write_gate_json "$OUT/logs/$id.gate.json" "$id" "$start" "$end" "$rc" "$TREE" "$workdir" "$@"
  chmod 600 "$OUT/logs/$id.stdout.log" "$OUT/logs/$id.stderr.log"
  echo "门禁 ${id} rc=${rc} ${start} -> ${end}"
  RESULTS+=("$id=$rc")
  if [ "$rc" != 0 ]; then FAILED+=("$id"); fi
}
echo "=== 门禁（依次执行，全部跑完再下结论）$(utc_now)"
run_gate full-regression . make test
run_gate backend-unit backend make test-unit
run_gate backend-integration backend make test-integration
run_gate lint-unit backend golangci-lint run --timeout=30m --build-tags=unit
run_gate lint-integration backend golangci-lint run --timeout=30m --build-tags=integration
# 取出的命令只含 /bin/sh、/bin/bash、-n 与 deploy/ 下的安全路径字符（正则已限定），逐行回显后执行，任一失败即停。
DEPLOY_SCRIPT="set -e"
for cmd in "${DEPLOY_TESTS[@]}"; do DEPLOY_SCRIPT+=$'\n'"echo '\$ ${cmd}'"$'\n'"${cmd}"; done
run_gate deploy-scripts . bash -c "$DEPLOY_SCRIPT"
AFTER_STATUS=$(tree_status 5)
if [ "${#FAILED[@]}" = 0 ] && [ -z "$AFTER_STATUS" ]; then STATUS=passed; REMOVED=true; else STATUS=failed; REMOVED=false; fi
python3 - "$OUT/summary.json" "$SUBJECT" "$ROUND" "$STATUS" "$PBUNDLE" "$PBRANCH" "$PCOMMIT" "$TREE_HEAD" "$TREE" "$HISTORY_TEST_TREE" "$NM_DIR" "$PYC" "$REMOVED" "$AFTER_STATUS" "${RESULTS[@]}" <<'PY'
import json, sys

out, subject, round_id, status, bundle, branch, commit, head, tree, history, nm_dir, pyc, removed, after_status, *results = sys.argv[1:]
payload = {
    "schema_version": "arm64-full-gates/v1",
    "purpose": "deploy-precondition-and-verification",
    "campaign_receipt": False,
    "statement": "部署前提与验证记录，不是 Campaign 收据；门禁全部在 ARM64 隔离测试树执行",
    "subject_id": subject,
    "round": round_id,
    "status": status,
    "source": {"bundle": bundle, "branch": branch, "commit": commit, "tree_head": head,
               "history_test_tree": history, "node_modules_source": nm_dir + "/node_modules"},
    "gates": [{"gate_id": item.split("=", 1)[0], "exit_code": int(item.split("=", 1)[1]),
               "gate_json": f"logs/{item.split('=', 1)[0]}.gate.json"} for item in results],
    "tree_status_after": after_status,
    "test_tree": tree,
    "bytecode_cache": pyc,
    "test_tree_removed": removed == "true",
}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
chmod 600 "$OUT/summary.json"
trap - ERR
if [ "$STATUS" = passed ]; then
  rm -rf "$TREE" "$PYC"
  echo "ARM64 全量门禁通过：${RESULTS[*]}；摘要 ${OUT}/summary.json；测试树与字节码缓存已删除"
  echo "FULL_GATES_DONE rc=0 subject=${SUBJECT} out=${OUT}"
  exit 0
fi
echo "ARM64 全量门禁未通过：${RESULTS[*]}"
if [ -n "$AFTER_STATUS" ]; then echo "门禁后测试树不干净：[${AFTER_STATUS}]"; fi
for id in "${FAILED[@]}"; do
  echo "  ${id}：${OUT}/logs/${id}.stdout.log、${OUT}/logs/${id}.stderr.log"
  grep -hE "^(FAIL|ERROR)[: ]|^--- FAIL|^FAIL[[:space:]]|make: \*\*\*|^[^ ]+\.go:[0-9]+:[0-9]+: " "$OUT/logs/$id.stderr.log" "$OUT/logs/$id.stdout.log" 2>/dev/null | head -n 10 | cut -c1-240 || true
done
echo "  摘要 ${OUT}/summary.json；测试树保留在 ${TREE}（排查用，下次运行会重建）"
echo "FULL_GATES_FAILED subject=${SUBJECT} out=${OUT} failed=${FAILED[*]:-tree-dirty}"
exit 1
