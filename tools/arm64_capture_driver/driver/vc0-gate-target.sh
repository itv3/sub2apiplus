#!/bin/bash
# VC-0 预跑目标平台门禁（只作预检，不是 VC-5 accept 的门禁收据）。
#
# 背景：目标平台门禁＝在 ARM64 上对候选测试树隔离执行 make test（每次 40～70 分钟），原本只在 VC-5 accept 之前由
# vc5-accept.sh 调用 vc5-gate-target.sh 执行。上一轮升级时门禁自身的问题（字节码缓存前缀让监督器
# 计时用例确定性失败）直到 accept 前才暴露，两次失败加修复耗掉 6.1 小时。本入口在 VC-0 用当时的候选源码（本轮受管
# 工具部署所在的提交）把同一门禁先跑一遍，门禁自身、ARM64 环境或源码在目标平台上的问题在 VC-0 就修掉。
#
# 做法（与 VC-5 同一套，不另写平行实现）：
#   1. 测试树：lib.sh 的 clone_test_tree（与 gates.sh prepare 同一函数：从 $HISTORY_TEST_TREE 克隆完整历史 → 从 bundle
#      取分支 → 检出提交 → 断言完整历史、不含 vendor），树放在 $RUNROOT/vc0-preflight/test-tree；前端 node_modules
#      取前序候选测试树里的一份（或第 4 个参数给出的前端依赖目录），其 pnpm-lock.yaml 必须与本树逐字相同，否则拒绝；
#   2. 门禁（E2-04 起）：gate_before 环境收据 → 入口门禁 entry-gates.sh（组合 preflight＝make test 的组成，统一调度执行器
#      一次运行、全部单元并行，隔离方式与目标平台门禁相同）→ gate_after 环境收据；主体标识 vc0-preflight-<UTC 时间戳>，
#      门禁根 $RUNROOT/vc0-preflight/<主体标识>，字节码缓存 $RUNROOT/vc0-preflight/pycache-target-platform（与 VC-5 的缓存分开）。
#      入口门禁（组合 entry）的一次运行已经产出同形状的预跑记录（preflight.json），升级入口不必在建账本之后再跑本命令；
#      本命令留作单独预跑的入口；
#   3. 结论：门禁根下写预检摘要 preflight.json（用途、源码坐标、退出码与记录位置）与 logs/target-platform.gate.json
#      （make test 的组成全部通过与否）。通过即删掉测试树与缓存（数 GB），未通过保留测试树供排查（下次预跑会重建）。
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
mkdir -m 0700 "$OUT" "$OUT/environment"
echo "=== VC-0 预跑目标平台门禁 ${SUBJECT}（只作预检，不是 accept 门禁收据；入口门禁一次运行）$(utc_now)"
echo "bundle=${PBUNDLE} 分支=${PBRANCH} 提交=${PCOMMIT} 前端依赖=${NM_DIR}"
environment_receipt() {  # <gate_before|gate_after> <文件名前缀>
  python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$OUT" --output "environment/$SUBJECT-$2-facts.json" --phase "$1" --subject-id "$SUBJECT" --rust-tls-codex-version "$TARGET_VERSION" | cut -c1-160
  python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$OUT" --facts "environment/$SUBJECT-$2-facts.json" --output "environment/$SUBJECT-$2.json" | cut -c1-160
}
environment_receipt gate_before before
trap - ERR
RC=0
bash "$DRV/entry-gates.sh" --profile preflight --out "$OUT" --work "$PRE" --pycache "$PYC" "$PBUNDLE" "$PBRANCH" "$PCOMMIT" "$NM_DIR" || RC=$?
if [ "$RC" != 0 ] && [ "$RC" != 1 ]; then
  echo "VC0_GATE_TARGET_ABORTED：入口门禁没有给出结论（rc=${RC}），原因见上方输出；主体目录 ${OUT}"
  exit 3
fi
trap 'on_error $LINENO' ERR
cd "$D"; environment_receipt gate_after after
GATE_JSON="$OUT/logs/target-platform.gate.json"
# 预跑记录沿用原形状与位置：门禁记录 target-platform（make test 的组成）、测试树是否已删、执行器日志位置。
python3 - "$OUT" "$RC" "$([ -d "$TREE" ] && echo false || echo true)" <<'PY'
import json, sys
from pathlib import Path

out, rc, removed = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "true"
regression = json.loads((out / "logs" / "full-regression.gate.json").read_text(encoding="utf-8"))
target = {**regression, "gate_id": "target-platform", "exit_code": regression["exit_code"] if rc == 0 else max(1, regression["exit_code"])}
(out / "logs" / "target-platform.gate.json").write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
preflight = json.loads((out / "preflight.json").read_text(encoding="utf-8"))
preflight["status"] = "passed" if rc == 0 else "failed"
preflight["gate"].update(exit_code=target["exit_code"], gate_json="logs/target-platform.gate.json",
                         stdout_log="executor.log", stderr_log="executor.log", entry_gates_summary="entry-gates.json")
preflight["test_tree_removed"] = removed
(out / "preflight.json").write_text(json.dumps(preflight, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
chmod 600 "$OUT/preflight.json" "$GATE_JSON" "$OUT"/environment/*
trap - ERR
if [ "$RC" = 0 ]; then
  echo "VC-0 预跑通过：门禁记录 ${GATE_JSON}，预检摘要 ${OUT}/preflight.json；测试树与字节码缓存已删除"
  echo "VC0_GATE_TARGET_DONE rc=0 subject=${SUBJECT} out=${OUT}"
  exit 0
fi
echo "VC-0 预跑未通过（入口门禁 rc=${RC}）：按普通 VC-0 失败处理，修好后重跑本命令（新主体标识，本次结果留档）"
echo "  门禁记录 ${GATE_JSON}（各门禁项 ${OUT}/logs/）"
echo "  执行器日志 ${OUT}/executor.log"
echo "  预检摘要 ${OUT}/preflight.json"
echo "  测试树保留在 ${TREE}（排查用，下次预跑会重建）"
grep -E "^(单元未通过|测试组 .*全集核对失败)" "$OUT/executor.log" 2>/dev/null | head -n 10 | cut -c1-240 || true
echo "VC0_GATE_TARGET_FAILED rc=${RC} subject=${SUBJECT} out=${OUT}"
exit 1
