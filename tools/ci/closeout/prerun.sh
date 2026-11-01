#!/bin/bash
# 收尾 b13：CI 等价预跑——在候选提交的工作树上按“短的在前、长的在后”串行跑完整套 CI 门禁，红了不停，最后汇总。
#
# 做什么：每一步都经重负载锁 HEAVY_RUN（本机 16 GiB 一次只跑一路），go test 一律 -p 3。步骤（对应 CI 作业）：
#   shard   capture-tools 分片闭合自检（make test-capture-tools-shard-check，几秒）
#   zerodiff 后端、前端相对基点必须零改动（只在设了 PRERUN_BASE 时才有意义；有改动则本步失败并提示不能省 Go 全包与 lint）
#   gounit  go test -tags=unit -p 3 -count=1 ./...         （CI test 作业 Unit tests；含全部 Go 冻结类测试）
#   gointeg go test -tags=integration -p 3 -count=1 ./...  （CI test 作业 Integration tests；本机无 Docker 时只证明编译）
#   egress  make check-egress-spec                          （CI egress-spec-gates 的超集：多一项本地源码引用）
#   lint    golangci-lint 三遍：无标签、unit、integration，均 --timeout=30m --max-issues-per-linter=0 --max-same-issues=0
#   capture 本机完整 make test-capture-tools，经 local-capture-tools-receipt.sh 写本机收据（含全部 test_claude_rule_expectations_*）
# 输入（环境变量；一般由 closeout.sh 的 b13-prerun 导出）：
#   PRERUN_REPO（必填）  候选提交的工作树
#   PRERUN_MAIN（必填）  主检出（找重负载锁、golangci-lint、Codex 源码树）
#   HEAVY_RUN            重负载锁脚本，缺省取 <PRERUN_MAIN>/local-analysis/opt-*/heavy-run.sh 里排最后的一个
#   GOLANGCI_LINT        golangci-lint 可执行文件，缺省取 PATH 里的，再退到 <PRERUN_MAIN>/local-analysis/tools/golangci-lint-*/golangci-lint
#   CODEX_SPEC_SOURCE_ROOT、CAPTURE_TYPESCRIPT_MODULE  check-egress-spec／capture-tools 需要，缺了对应步骤会失败并写进日志
#   PRERUN_SKIP="gointeg,lint"（逗号分隔要跳过的步骤，缺省全跑）；PRERUN_ONLY="capture"（只跑这些）；PRERUN_BASE（zerodiff 的基点提交）
#   PRERUN_CAPTURE_PAR（capture 的并行度，缺省 0＝取执行器默认）
# 用法（脱离会话跑，别在前台等）：python3 -B detach.py <日志> "PRERUN_REPO=<树> PRERUN_MAIN=<主检出> bash prerun.sh <日志目录>"
# 输出：<日志目录>/<序号>-<步骤>.log（首行 START…head=，末两行 END…、EXIT=rc）与 SUMMARY.txt（每步 rc、用时），全部结束写 ALLDONE。
# 何时用：候选提交取回之后（b12 之后）；后端有 Catalog 数据与测试改动时不能省 Go 全包与 lint。
set -u
OUT="${1:?缺少日志目录}"; mkdir -p "${OUT}"
REPO="${PRERUN_REPO:?必须设置 PRERUN_REPO（候选工作树）}"
MAIN="${PRERUN_MAIN:?必须设置 PRERUN_MAIN（主检出）}"
HEAVY="${HEAVY_RUN:-$(ls "${MAIN}"/local-analysis/opt-*/heavy-run.sh 2>/dev/null | tail -1)}"
[ -n "${HEAVY}" ] || { echo "找不到重负载锁脚本：设 HEAVY_RUN" >&2; exit 2; }
LINT="${GOLANGCI_LINT:-$(command -v golangci-lint || ls "${MAIN}"/local-analysis/tools/golangci-lint-*/golangci-lint 2>/dev/null | tail -1)}"
export HEAVY_EXCLUSIVE=1 HEAVY_MAX_SLOTS=2 PYTHONDONTWRITEBYTECODE=1
cd "${REPO}" || exit 99
HEAD_SHA="$(git rev-parse HEAD)"
: > "${OUT}/SUMMARY.txt"
command -v caffeinate > /dev/null 2>&1 && { caffeinate -i -w $$ & }   # macOS 防休眠（长任务）；别的系统没有就算了

want() { # 该步骤是否要跑
  local name="$1"
  if [ -n "${PRERUN_ONLY:-}" ]; then case ",${PRERUN_ONLY}," in *",${name},"*) return 0 ;; *) return 1 ;; esac; fi
  case ",${PRERUN_SKIP:-}," in *",${name},"*) return 1 ;; esac
  return 0
}
step() { # step <序号> <名称> <命令…>
  local idx="$1" name="$2"; shift 2
  want "${name}" || { echo "${idx}-${name} SKIP" >> "${OUT}/SUMMARY.txt"; return 0; }
  local log="${OUT}/${idx}-${name}.log" t0 t1
  t0=$(date +%s)
  echo "START ${name} $(date '+%F %T') epoch=${t0} head=$(git rev-parse HEAD)" > "${log}"
  "$@" >> "${log}" 2>&1; local rc=$?
  t1=$(date +%s)
  echo "END ${name} $(date '+%F %T') epoch=${t1}" >> "${log}"; echo "EXIT=${rc}" >> "${log}"
  echo "${idx}-${name} rc=${rc} $((t1 - t0))s" >> "${OUT}/SUMMARY.txt"
  return 0
}

zerodiff() { # 后端、前端相对基点零改动才允许省 Go 全包与 lint
  [ -n "${PRERUN_BASE:-}" ] || { echo "没设 PRERUN_BASE，无法判断零改动"; return 1; }
  local out; out="$(git -C "${REPO}" diff --stat "${PRERUN_BASE}..HEAD" -- backend frontend)"
  if [ -n "${out}" ]; then echo "后端／前端相对基点有改动，不能省 Go 全包与 lint："; echo "${out}"; return 1; fi
  echo "git diff --stat ${PRERUN_BASE:0:10}..HEAD -- backend frontend：空（零改动）"
}
lint_all() {
  local rc=0 tags
  [ -n "${LINT}" ] || { echo "找不到 golangci-lint：设 GOLANGCI_LINT"; return 1; }
  cd "${REPO}/backend" || return 99
  for tags in "" unit integration; do
    echo "### lint tags=[${tags}] $(date '+%F %T')"
    if [ -n "${tags}" ]; then
      bash "${HEAVY}" "${LINT}" run --timeout=30m --max-issues-per-linter=0 --max-same-issues=0 --build-tags="${tags}" ./... || rc=1
    else
      bash "${HEAVY}" "${LINT}" run --timeout=30m --max-issues-per-linter=0 --max-same-issues=0 ./... || rc=1
    fi
  done
  return ${rc}
}
step 0 shard make test-capture-tools-shard-check CAPTURE_TEST_SHARDS=4
step 0 zerodiff zerodiff
step 1 gounit bash -c "cd '${REPO}/backend' && bash '${HEAVY}' go test -tags=unit -p 3 -count=1 ./..."
step 2 gointeg bash -c "cd '${REPO}/backend' && bash '${HEAVY}' go test -tags=integration -p 3 -count=1 ./..."
step 3 egress bash "${HEAVY}" make check-egress-spec ${CODEX_SPEC_SOURCE_ROOT:+CODEX_SPEC_SOURCE_ROOT="${CODEX_SPEC_SOURCE_ROOT}"}
step 4 lint lint_all
# 并行度：缺省走 unit_executor.json 的默认；PRERUN_CAPTURE_PAR=3 可降到简报建议值。先回到仓库根：lint 步骤把当前目录留在 backend/，跳过时 capture 会找不到脚本
step 6 capture bash -c "cd '${REPO}' && CAPTURE_TEST_PARALLELISM='${PRERUN_CAPTURE_PAR:-0}' bash '${HEAVY}' bash tools/arm64_capture_driver/driver/local/local-capture-tools-receipt.sh --out-dir '${OUT}/receipts'"
echo "ALLDONE $(date '+%F %T') head=${HEAD_SHA}" > "${OUT}/ALLDONE"
