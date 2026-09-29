#!/bin/bash
# VC-0 预跑目标平台门禁（只作预检，不是 VC-5 accept 的门禁收据）。
#
# 背景：目标平台门禁＝在 ARM64 上对候选测试树隔离执行 make test（每次 40～70 分钟），原本只在 VC-5 accept 之前由
# vc5-accept.sh 调用 vc5-gate-target.sh 执行。上一轮升级时门禁自身的问题（修好接着跑第 67 项：字节码缓存前缀让监督器
# 计时用例确定性失败）直到 accept 前才暴露，两次失败加修复耗掉 6.1 小时。本入口在 VC-0 用当时的候选源码（本轮受管
# 工具部署所在的提交）把同一门禁先跑一遍，门禁自身、ARM64 环境或源码在目标平台上的问题在 VC-0 就修掉。
#
# 做法（与 VC-5 同一套，不另写平行实现）：
#   1. 测试树：lib.sh 的 clone_test_tree（与 gates.sh prepare 同一函数：从 $HISTORY_TEST_TREE 克隆完整历史 → 从 bundle
#      取分支 → 检出提交 → 断言完整历史、不含 vendor），树放在 $RUNROOT/vc0-preflight/test-tree；前端 node_modules
#      取前序候选测试树里的一份（或第 4 个参数给出的前端依赖目录），其 pnpm-lock.yaml 必须与本树逐字相同，否则拒绝；
#   2. 门禁：直接调用 vc5-gate-target.sh（gate_before 环境收据 → 树外只读字节码缓存 → 私有挂载命名空间里 make test
#      → gate_after 环境收据 → gate.json）；主体标识 vc0-preflight-<UTC 时间戳>，门禁根 $RUNROOT/vc0-preflight/<主体标识>，
#      字节码缓存 $RUNROOT/vc0-preflight/pycache-target-platform（第 4 个参数，与 VC-5 的缓存分开）；
#   3. 结论：门禁根下写预检摘要 preflight.json（用途、源码坐标、退出码与日志位置）。通过即删掉测试树与缓存（数 GB），
#      未通过保留测试树供排查（下次预跑会重建）。
#
# 边界：绝不写候选门禁目录（<campaign>-candidate-gates）与候选目录，不写时间账本与 Campaign，不发模型请求。
# build_gate_facts.py／vc5-accept.sh 只读候选门禁目录里按 attempt ID 命名的产物，本入口的结果不会被当成 accept 的
# 门禁收据；VC-5 accept 前仍在候选门禁目录执行正式目标平台门禁。
# 必须单独运行（与 accept 前正式门禁同一安静条件）：不要与 stage1 各步或 VC-1 取证并行，资源争用会把计时用例拖红。
#
# 用法（采集主机 root；make test 里有挂断检测用例，必须 setsid -f 启动，不能 nohup）：
#   ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash vc0-gate-target.sh <bundle> <分支> <40 位提交> [<前端依赖目录>] \
#     > $RUNROOT/vc0-gate-target.out 2>&1 < /dev/null
#   bundle：本机 git bundle create <文件> <BASE>..<分支>，BASE 必须在 $HISTORY_TEST_TREE 的历史里（例如前序候选的 DC）；
#   前端依赖目录：同时含 node_modules 与 pnpm-lock.yaml 的目录，默认 $HISTORY_TEST_TREE/frontend。
# 退出码：0 预跑通过；1 make test 未通过（门禁结论）；2 用法错误；3 准备或执行失败（bundle、测试树、前端依赖、
#   字节码缓存、环境收据、并发锁），没有门禁结论。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
usage() { echo "用法：bash vc0-gate-target.sh <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }
if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then usage; exit 2; fi
PBUNDLE="$1"; PBRANCH="$2"; PCOMMIT="$3"; NM_DIR="${4:-$HISTORY_TEST_TREE/frontend}"
if [[ "$PBUNDLE" != /* ]] || [ -L "$PBUNDLE" ] || [ ! -f "$PBUNDLE" ]; then echo "bundle 必须是已存在的普通文件（绝对路径）：$PBUNDLE" >&2; exit 2; fi
if ! [[ "$PBRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then echo "分支名只允许字母、数字与 ._/-：$PBRANCH" >&2; exit 2; fi
if ! [[ "$PCOMMIT" =~ ^[0-9a-f]{40}$ ]]; then echo "提交必须是完整 40 位小写 sha1：$PCOMMIT" >&2; exit 2; fi
if [[ "$NM_DIR" != /* ]]; then echo "前端依赖目录必须是绝对路径：$NM_DIR" >&2; exit 2; fi
PRE="$RUNROOT/vc0-preflight"; TREE="$PRE/test-tree"; PYC="$PRE/pycache-target-platform"; OUT=""
# 参数校验之后任何意外失败都按“准备或执行失败”退出 3，与门禁结论（退出 1）区分开。
on_error() {
  local rc=$? line="$1"
  trap - ERR
  echo "VC0_GATE_TARGET_ABORTED：准备或执行失败（vc0-gate-target.sh 第 ${line} 行，rc=${rc}），没有门禁结论；原因见上方输出，主体目录 ${OUT:-（未建立）}"
  echo "常见原因：bundle 的前置提交不在 \$HISTORY_TEST_TREE 历史里、分支或提交不对、字节码缓存建不成、环境收据未通过（出口或根盘水位）"
  exit 3
}
trap 'on_error $LINENO' ERR
mkdir -p "$PRE"; chmod 700 "$PRE"
# 同一轮只允许一个预跑（共用测试树与缓存）：mkdir 原子锁，持有者已不在时回收陈旧锁（与 fix-and-continue.sh 同一做法）。
LOCK="$PRE/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then echo "VC0_GATE_TARGET_ABORTED：已有 VC-0 预跑在运行（PID ${HOLDER}），拒绝并发"; exit 3; fi
  rm -rf "$LOCK"; mkdir "$LOCK"
fi
printf '%s\n' "$$" > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT
SUBJECT="vc0-preflight-$(date -u +%Y%m%dt%H%M%Sz)"
if [ -e "$PRE/$SUBJECT" ]; then SUBJECT="$SUBJECT-$$"; fi
OUT="$PRE/$SUBJECT"
mkdir -m 0700 "$OUT"
echo "=== VC-0 预跑目标平台门禁 ${SUBJECT}（只作预检，不是 accept 门禁收据）$(utc_now)"
echo "bundle=${PBUNDLE} 分支=${PBRANCH} 提交=${PCOMMIT} 前端依赖=${NM_DIR}"
echo "=== 测试树（clone_test_tree，与 VC-5 的 gates.sh prepare 同一实现，umask 022 同 VC-5）$(utc_now)"
umask 022
clone_test_tree "$TREE" "$PBUNDLE" "$PBRANCH" "$PCOMMIT"
# 前端依赖：lockfile 不同的 node_modules 会让前端检查与 TypeScript 解析器摘要核对误报，直接拒绝，不带着错配的依赖跑一个小时。
if [ ! -d "$NM_DIR/node_modules" ] || ! cmp -s "$NM_DIR/pnpm-lock.yaml" "$TREE/frontend/pnpm-lock.yaml"; then
  echo "前端依赖不可用：${NM_DIR}/node_modules 不存在，或 ${NM_DIR}/pnpm-lock.yaml 与本树 frontend/pnpm-lock.yaml 不同"
  echo "本轮 lockfile 有变化时，先按 frontend.sh 同一方式（node:20 容器内 pnpm install --frozen-lockfile）在独立目录装好依赖，再把该目录作为第 4 个参数传入"
  echo "VC0_GATE_TARGET_ABORTED：前端依赖不可用，没有门禁结论；主体目录 ${OUT}"
  exit 3
fi
cp -a "$NM_DIR/node_modules" "$TREE/frontend/node_modules"
umask 077
TREE_HEAD=$(git -C "$TREE" rev-parse HEAD)
echo "test-tree HEAD=${TREE_HEAD} status=[$(git -C "$TREE" status --porcelain --untracked-files=all)]"
echo "=== 目标平台门禁（vc5-gate-target.sh 同一套执行方式；门禁根 ${OUT}，字节码缓存 ${PYC}）$(utc_now)"
bash "$DRV/vc5-gate-target.sh" "$SUBJECT" "$OUT" "$TREE" "$PYC"
GATE_JSON="$OUT/logs/target-platform.gate.json"
RC=$(python3 -c "import json,sys; print(int(json.load(open(sys.argv[1], encoding='utf-8'))['exit_code']))" "$GATE_JSON")
if [ "$RC" = 0 ]; then REMOVED=true; else REMOVED=false; fi
python3 - "$OUT/preflight.json" "$SUBJECT" "$ROUND" "$TARGET_VERSION" "$PBUNDLE" "$PBRANCH" "$PCOMMIT" "$TREE_HEAD" "$TREE" "$HISTORY_TEST_TREE" "$NM_DIR" "$PYC" "$GATE_JSON" "$REMOVED" <<'PY'
import json, sys

out, subject, round_id, target, bundle, branch, commit, head, tree, history, nm_dir, pyc, gate_json, removed = sys.argv[1:]
gate = json.load(open(gate_json, encoding="utf-8"))
exit_code = int(gate["exit_code"])
payload = {
    "schema_version": "arm64-vc0-target-gate-preflight/v1",
    "purpose": "vc0-preflight",
    "accept_gate_receipt": False,
    "statement": "只作 VC-0 预检，不是 VC-5 accept 的门禁收据；accept 前仍须在候选门禁目录执行正式目标平台门禁",
    "subject_id": subject,
    "round": round_id,
    "target_version": target,
    "status": "passed" if exit_code == 0 else "failed",
    "source": {
        "bundle": bundle,
        "branch": branch,
        "commit": commit,
        "tree_head": head,
        "history_test_tree": history,
        "node_modules_source": nm_dir + "/node_modules",
    },
    "gate": {
        "exit_code": exit_code,
        "started_at_utc": gate["started_at_utc"],
        "completed_at_utc": gate["completed_at_utc"],
        "gate_json": "logs/target-platform.gate.json",
        "stdout_log": "logs/target-platform.stdout.log",
        "stderr_log": "logs/target-platform.stderr.log",
        "test_tree": tree,
        "bytecode_cache": pyc,
    },
    "test_tree_removed": removed == "true",
}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
chmod 600 "$OUT/preflight.json"
trap - ERR
if [ "$RC" = 0 ]; then
  rm -rf "$TREE" "$PYC"
  echo "VC-0 预跑通过：门禁记录 ${GATE_JSON}，预检摘要 ${OUT}/preflight.json；测试树与字节码缓存已删除"
  echo "VC0_GATE_TARGET_DONE rc=0 subject=${SUBJECT} out=${OUT}"
  exit 0
fi
echo "VC-0 预跑未通过（make test rc=${RC}）：按普通 VC-0 失败处理，修好后重跑本命令（新主体标识，本次结果留档）"
echo "  门禁记录 ${GATE_JSON}"
echo "  标准输出 ${OUT}/logs/target-platform.stdout.log"
echo "  标准错误 ${OUT}/logs/target-platform.stderr.log"
echo "  预检摘要 ${OUT}/preflight.json"
echo "  测试树保留在 ${TREE}（排查用，下次预跑会重建）"
grep -hE "^(FAIL|ERROR)[: ]|^--- FAIL|^FAIL[[:space:]]|make: \*\*\*" "$OUT/logs/target-platform.stderr.log" "$OUT/logs/target-platform.stdout.log" 2>/dev/null | head -n 10 | cut -c1-240 || true
echo "VC0_GATE_TARGET_FAILED rc=${RC} subject=${SUBJECT} out=${OUT}"
exit 1
