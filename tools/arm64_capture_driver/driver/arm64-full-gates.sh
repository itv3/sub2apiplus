#!/bin/bash
# ARM64 全量门禁：受管工具每轮修复的部署前提、版本登记变更集与升级收尾的验证，全部在采集主机的隔离测试树上执行，
# 不在本机跑测试。门禁项与 CI（backend-ci.yml）逐项对齐，全部跑完再下结论：
#   1. full-regression：make test 的组成（后端 go test 与不带标签的 golangci-lint、前端三项、采集工具全量、
#      test-official-client-control、check-egress-spec 的全部子检查）；
#   2. backend-unit、backend-integration：go test -tags=unit／integration ./... -count=1（采集主机有 Docker，集成测试真实执行，
#      带 CI=true，没有 Docker 时失败而不是静默跳过）；
#   3. lint-unit、lint-integration：golangci-lint run --timeout=30m --build-tags=unit／integration；
#   4. deploy-scripts：CI shell 作业与 test 作业里的部署脚本测试（从测试树的 backend-ci.yml 逐行取出，不在此写死），每条单独
#      执行、一条失败其余照跑；macOS 专用的 Apple container 部署脚本测试在 Linux 上记为不执行（CI 在 macos-15 上照常执行）。
# E2-04 起由入口门禁 entry-gates.sh（组合 full-gates）一次运行：全部单元交给统一调度执行器在整机额度内并行，隔离方式与
# 目标平台门禁相同（私有挂载命名空间里遮住 /root/oauth-capture 别名，树外只读字节码缓存）。测试树用 lib.sh 的
# clone_test_tree（完整历史、不含 vendor），前端依赖取 lockfile 相同的一份。
#
# 结论只写 $RUNROOT/full-gates/<主体标识>/（summary.json、entry-gates.json、logs/ 下各门禁项的 gate.json 与执行器记录），是部署
# 前提与验证记录，不是 Campaign 收据：不写候选门禁目录、候选目录、时间账本与 Campaign，零模型请求。必须单独运行，不与
# 目标平台门禁、VC-1／VC-5 采集并行（采集主机只有 4 核，资源争用会把计时用例拖红）。
#
# 用法（采集主机 root；make test 里有挂断检测用例，必须 setsid -f 启动，不能 nohup）：
#   ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash arm64-full-gates.sh <bundle> <分支> <40 位提交> [<前端依赖目录>] \
#     > $RUNROOT/full-gates.out 2>&1 < /dev/null
#   bundle：本机 git bundle create <文件> <BASE>..<分支>，BASE 必须在 $HISTORY_TEST_TREE 的历史里；
#   前端依赖目录：同时含 node_modules 与 pnpm-lock.yaml 的目录，默认 $HISTORY_TEST_TREE/frontend。
# 退出码：0 全部通过；1 至少一项门禁未通过（门禁结论，测试树保留供排查）；2 用法错误；3 准备或执行失败（bundle、
#   测试树、前端依赖、字节码缓存、部署脚本测试取不到、并发锁、执行器出错），没有门禁结论。
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
echo "=== ARM64 全量门禁 ${SUBJECT}（部署前提与验证记录，不是 Campaign 收据；入口门禁一次运行）$(utc_now)"
trap - ERR
RC=0
bash "$DRV/entry-gates.sh" --profile full-gates --out "$OUT" --work "$FG" --pycache "$PYC" "$PBUNDLE" "$PBRANCH" "$PCOMMIT" "$NM_DIR" || RC=$?
if [ "$RC" != 0 ] && [ "$RC" != 1 ]; then
  echo "FULL_GATES_ABORTED：入口门禁没有给出结论（rc=${RC}），原因见上方输出；主体目录 ${OUT}"
  exit 3
fi
AFTER_STATUS=""
if [ -d "$TREE" ]; then AFTER_STATUS=$(git -C "$TREE" status --porcelain --untracked-files=all 2>&1 | head -n 5 || true); fi
# summary.json 保持原形状（arm64-full-gates/v1）：门禁项结论取自入口门禁的全量门禁记录，再补门禁后测试树状态。
python3 - "$OUT" "$RC" "$AFTER_STATUS" "$([ -d "$TREE" ] && echo false || echo true)" <<'PY'
import json, sys
from pathlib import Path

out, rc, after_status, removed = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4] == "true"
summary = json.loads((out / "full-gates-summary.json").read_text(encoding="utf-8"))
summary.update(status="passed" if rc == 0 else "failed", tree_status_after=after_status, test_tree_removed=removed)
(out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(out / "summary.json").chmod(0o600)
PY
RESULTS=$(python3 -c "import json,sys; print(' '.join(f\"{g['gate_id']}={g['exit_code']}\" for g in json.load(open(sys.argv[1]))['gates']))" "$OUT/summary.json")
if [ "$RC" = 0 ]; then
  echo "ARM64 全量门禁通过：${RESULTS}；摘要 ${OUT}/summary.json；测试树与字节码缓存已删除"
  echo "FULL_GATES_DONE rc=0 subject=${SUBJECT} out=${OUT}"
  exit 0
fi
FAILED=$(python3 -c "import json,sys; print(' '.join(g['gate_id'] for g in json.load(open(sys.argv[1]))['gates'] if g['exit_code'] != 0))" "$OUT/summary.json")
echo "ARM64 全量门禁未通过：${RESULTS}"
if [ -n "$AFTER_STATUS" ]; then echo "门禁后测试树不干净：[${AFTER_STATUS}]"; fi
echo "  摘要 ${OUT}/summary.json；各门禁项记录 ${OUT}/logs/；测试树保留在 ${TREE}（排查用，下次运行会重建）"
echo "FULL_GATES_FAILED subject=${SUBJECT} out=${OUT} failed=${FAILED:-tree-dirty}"
exit 1
