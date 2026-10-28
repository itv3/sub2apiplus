#!/bin/bash
# 把数据根 backend（$D/backend）换成集成分支 ENTRY_COMMIT 的 backend。采集主机侧脚本，随驱动安装在 <驱动目录>/pipeline/。
#
# 为什么要换：VC-2 的 prepare-profile 与 VC-3 的 stage-profile 用 `go run ./cmd/egresscatalogstage`，工作目录是数据根 backend
# （先例：换成候选预备提交的 backend，旧的留成 backend.pre-<提交>-<日期>）。数据根 backend 若是旧快照，画像目录里没有基线（Active）版本，
# VC-2 第一个批次就会失败。流水线 VC2 阶段起跑前会检查并 STOPPED，但不会自己换。
#
# 【只能在 VC-0 收口后、VC-2 开始之前执行】：脚本起跑前自动检查——入口编排器与各类门禁/验证没有在跑、Formal Campaign 已建且 VC-0 已收口、
# VC-2 尚未开始；任一项不满足就拒绝。需要显式给出模式：
#   --dry-run          只做起跑前检查并打印计划（轻量只读：不解包 bundle、不编译、不写任何文件）。现在只能用这个演练。
#   --execute          检查通过后真正换入（解包 → 修权限 → 逐文件核 git blob → 旧目录改名 backend.superseded-<UTC 时间> → 新目录就位 → 编译冒烟）。
#   --rollback <时间>   把 backend.superseded-<时间> 改回 backend（换入后冒烟失败或要退回时用；当前 backend 改名为 backend.rejected-<时间>，不删除）。
# 旧目录一律只改名不删除；权限照数据根惯例：目录 775、普通文件 664、带可执行位的文件 775，属主 root:root。
#
# 测试用环境变量（仅在 ARM64 之外或演练根目录里验证脚本逻辑时用）：
#   SYNC_NO_ENV=1                不读 env.sh，改读环境里预设的 D／ENTRY_BUNDLE／ENTRY_BRANCH／ENTRY_COMMIT／BASELINE_VERSION／TARGET_VERSION／NEWDIR／RUNROOT
#   SYNC_WORK=<目录>             临时工作目录（缺省 ${PREP}/backend-sync）
#   SYNC_SKIP_PROC_CHECK=1       跳过“有无编排在跑”的进程检查（只在演练根目录里用）
#   SYNC_SKIP_GO=1               跳过 go 编译冒烟
set -uo pipefail
export PYTHONDONTWRITEBYTECODE=1
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
MODE=""; ROLLBACK_TS=""
case "${1:-}" in
  --dry-run) MODE=dry ;;
  --execute) MODE=exec ;;
  --rollback) MODE=rollback; ROLLBACK_TS=${2:-} ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac

utc() { date -u +%FT%TZ; }
OKN=0; BAD=0
ck() { # $1 OK|不通过|提示 $2 文本
  case "$1" in OK) OKN=$((OKN + 1)) ;; 不通过) BAD=$((BAD + 1)) ;; esac
  printf '  [%s] %s\n' "$1" "$2"
}
die() { echo "FATAL: $*"; exit 1; }

# ---- 本轮参数
if [ "${SYNC_NO_ENV:-0}" = 1 ]; then
  : "${D:?}" "${ENTRY_BUNDLE:?}" "${ENTRY_BRANCH:?}" "${ENTRY_COMMIT:?}" "${BASELINE_VERSION:?}" "${TARGET_VERSION:?}" "${NEWDIR:?}" "${RUNROOT:?}"
else
  pl_load_round_env || die "参数文件解析失败：${ARM64_VC_ENV:-未设置}"
  pl_defaults
fi
BK=${D}/backend
PREP=${PREP:-${PIPE_PREP:-/root/pipeline-prep/sync}}
WORK=${SYNC_WORK:-${PREP}/backend-sync}

# ---- 回退模式
if [ "${MODE}" = rollback ]; then
  [ -n "${ROLLBACK_TS}" ] || die "用法：--rollback <UTC 时间戳，如 20261027t150000z>"
  OLD=${D}/backend.superseded-${ROLLBACK_TS}
  [ -d "${OLD}" ] || die "没有 ${OLD}"
  [ -d "${BK}" ] || die "当前没有 ${BK}"
  mv "${BK}" "${D}/backend.rejected-${ROLLBACK_TS}" && mv "${OLD}" "${BK}" || die "改名失败，请手工核对 ${D} 下的 backend*"
  echo "已退回：${BK} <- ${OLD}；被退回的新目录留作 ${D}/backend.rejected-${ROLLBACK_TS}"
  exit 0
fi

# ================================================================ 起跑前检查
echo "sync-data-root-backend  $(utc)  模式=${MODE}  目标提交 ${ENTRY_COMMIT:0:9}（${ENTRY_BRANCH}）"
echo "== 起跑前检查（任一项不通过即拒绝）"
if [ "$(id -u)" = 0 ]; then ck OK "以 root 运行"; elif [ "${SYNC_NO_ENV:-0}" = 1 ]; then ck 提示 "测试模式（SYNC_NO_ENV=1）不要求 root"; else ck 不通过 "需要 root（目录属主 root:root）"; fi
# 1) 没有入口与各类门禁/验证在跑（与流水线 others_running 同一套模式，另加 VC-2 的 vc23.sh；VC-1 的首批批次允许在跑，它不读 backend）
if [ "${SYNC_SKIP_PROC_CHECK:-0}" = 1 ]; then
  ck 提示 "已跳过进程检查（SYNC_SKIP_PROC_CHECK=1，只在演练根目录里用）"
else
  RUNNING=$(pgrep -af '[e]ntry_orchestrator.py|[b]ackground_validation.py|[e]ntry-gates.sh|[c]hain-rehearsal|[a]rm64-vc4-gates.sh|[v]c4-all.sh|[v]c5-all.sh|[v]c23(-seq)?\.sh|[e]ntry-dryrun.sh start|[s]tart-entry.sh|[b]ackground-validate.sh start' | grep -v 'sync-data-root-backend' || true)
  [ -z "${RUNNING}" ] && ck OK "没有入口编排器／门禁／后台验证／VC-2 以后阶段在跑" \
    || ck 不通过 "有编排或门禁在跑：$(echo "${RUNNING}" | head -2 | cut -c1-140 | tr '\n' ';')"
fi
# 2) VC-0 已收口（Formal Campaign 已建，且收口记录在）
if [ -d "${NEWDIR}" ] && { [ -e "${NEWDIR}/control/vc/vc-0-checkpoint.json" ] || [ -e "${RUNROOT}/entry-steps/vc0-closeout.json" ]; }; then
  ck OK "VC-0 已收口（Formal Campaign ${NEWDIR##*/} 在，收口记录在）"
else
  ck 不通过 "VC-0 尚未收口（Formal Campaign 目录或 vc0-closeout 记录不在）"
fi
# 3) VC-2 尚未开始
if ls "${NEWDIR}"/control/vc/vc-2-checkpoint.json >/dev/null 2>&1; then
  ck 不通过 "VC-2 已经封存（vc-2-checkpoint.json 在），不能再换 backend"
else
  ck OK "VC-2 尚未封存（无 vc-2-checkpoint.json）"
fi
# 4) 流水线若在跑，只允许停在 VC1（VC2 起会读这个目录）
if [ -d "${PREP}/vc-pipeline.lock" ] && grep -q 'STAGE VC[2-6]\|STAGE BGV' "${PREP}/pipeline/vc-status.txt" 2>/dev/null; then
  ck 不通过 "流水线已进入 VC2 或之后的阶段（见 pipeline/vc-status.txt）"
else
  ck OK "流水线未进入 VC2 及之后的阶段"
fi
# 5) 目录与磁盘
[ -d "${BK}" ] && ck OK "现有 ${BK} 存在（$(du -sh "${BK}" 2>/dev/null | cut -f1)）" || ck 不通过 "没有现有 ${BK}"
FREE_KB=$(df -Pk "${D}" | awk 'NR==2{print $4}')
[ "${FREE_KB:-0}" -ge $((3 * 1024 * 1024)) ] && ck OK "磁盘可用 $((FREE_KB / 1024 / 1024)) GiB（≥3 GiB）" || ck 不通过 "磁盘可用不足 3 GiB"
ls -d "${D}"/backend.new-* >/dev/null 2>&1 && ck 不通过 "残留 backend.new-*（上次中断？人工确认后处理）" || ck OK "没有残留的 backend.new-*"
# 6) bundle 与目标提交（轻量：只读 bundle 头部，不解包）
if [ -f "${ENTRY_BUNDLE}" ]; then
  HEADS=$(git bundle list-heads "${ENTRY_BUNDLE}" "refs/heads/${ENTRY_BRANCH}" 2>/dev/null | awk '{print $1}')
  [ "${HEADS}" = "${ENTRY_COMMIT}" ] && ck OK "bundle 头 ${ENTRY_BRANCH} = ENTRY_COMMIT ${ENTRY_COMMIT:0:9}" \
    || ck 不通过 "bundle 里 ${ENTRY_BRANCH} 的头（${HEADS:-空}）不等于 ENTRY_COMMIT"
else
  ck 不通过 "ENTRY_BUNDLE 不存在：${ENTRY_BUNDLE}"
fi
# 7) 工具链（只报告）
if command -v go >/dev/null 2>&1 || [ -x /usr/local/go/bin/go ]; then
  export PATH=/usr/local/go/bin:$PATH; ck OK "宿主 $(go version | awk '{print $3}')"
elif [ "${SYNC_SKIP_GO:-0}" = 1 ]; then ck 提示 "无 go，已按 SYNC_SKIP_GO=1 跳过"
else ck 不通过 "找不到 go"; fi
echo "== 检查汇总：通过 ${OKN} 项，不通过 ${BAD} 项"
[ "${BAD}" = 0 ] || { echo "拒绝：请先处理上面“不通过”的项（本脚本没有改任何东西）"; exit 1; }
TS=$(date -u +%Y%m%dt%H%M%Sz)
echo "== 计划：解包 ${ENTRY_COMMIT:0:9} 的 backend 到 ${D}/backend.new-${TS} → 核 git blob → ${BK} 改名 backend.superseded-${TS} → 新目录就位 → go build 冒烟 + go run -h"
if [ "${MODE}" = dry ]; then
  echo "dry-run 结束：没有写任何文件。真正执行：bash $0 --execute（须在 VC-0 收口后、VC-2 之前）"
  exit 0
fi

# ================================================================ 真正执行
RUNDIR=${WORK}/run-${TS}
SRC=${RUNDIR}/src.git
NEW=${D}/backend.new-${TS}
mkdir -p "${RUNDIR}" || die "建不了 ${RUNDIR}"
echo "$(utc) 取 bundle 的 ${ENTRY_BRANCH}"
git init -q --bare "${SRC}" || die "git init 失败"
git -C "${SRC}" fetch -q "${ENTRY_BUNDLE}" "+refs/heads/${ENTRY_BRANCH}:refs/heads/entry" || die "从 bundle 取分支失败"
[ "$(git -C "${SRC}" rev-parse refs/heads/entry)" = "${ENTRY_COMMIT}" ] || die "取回的分支头不等于 ENTRY_COMMIT"
echo "$(utc) 解包 backend → ${NEW}"
mkdir -p "${NEW}" || die "建不了 ${NEW}"
git -C "${SRC}" archive --format=tar "${ENTRY_COMMIT}" backend | tar -x -C "${NEW}" --strip-components=1 --no-same-owner || die "解包失败"
# 权限照数据根惯例：目录 775，普通文件 664，带可执行位的 775
find "${NEW}" -type d -exec chmod 775 {} + && find "${NEW}" -type f -perm -100 -exec chmod 775 {} + && find "${NEW}" -type f ! -perm -100 -exec chmod 664 {} + \
  || die "修权限失败"
chown -R root:root "${NEW}" 2>/dev/null || true
echo "$(utc) 逐文件核 git blob"
git -C "${SRC}" -c core.quotePath=false ls-tree -r "${ENTRY_COMMIT}" backend/ | sed -E 's#^[0-9]+ [a-z]+ ([0-9a-f]+)\t(backend/)?#\1\t#' | LC_ALL=C sort -t "$(printf '\t')" -k2,2 > "${RUNDIR}/expect.tsv"
( cd "${NEW}" && find . -type f | sed 's#^\./##' | LC_ALL=C sort > "${RUNDIR}/files.txt" )
# 在新目录里用 --stdin-paths 逐文件算 blob id（路径相对新目录；GIT_DIR 指向临时裸库只是为了让 git 有个仓库上下文）
( cd "${NEW}" && GIT_DIR="${SRC}" git hash-object --no-filters --stdin-paths < "${RUNDIR}/files.txt" > "${RUNDIR}/ids.txt" ) || die "算 blob id 失败"
paste "${RUNDIR}/ids.txt" "${RUNDIR}/files.txt" | LC_ALL=C sort -t "$(printf '\t')" -k2,2 > "${RUNDIR}/actual.tsv"
if ! cmp -s "${RUNDIR}/expect.tsv" "${RUNDIR}/actual.tsv"; then
  diff "${RUNDIR}/expect.tsv" "${RUNDIR}/actual.tsv" | head -10
  die "解包结果与 git 树不一致（文件集合或内容有差），${NEW} 保留供检查，未改动 ${BK}"
fi
echo "  一致：$(wc -l < "${RUNDIR}/actual.tsv") 个文件"
# 内容与工具链核对：基线（Active）画像在、release graph 的 Active＝基线版本、go.mod 要求不高于宿主 Go
python3 -B - "${NEW}" "${BASELINE_VERSION}" "${TARGET_VERSION}" <<'PY' || die "内容核对不通过，${NEW} 保留供检查，未改动 ${BK}"
import json, re, subprocess, sys
from pathlib import Path
new, base, target = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
rt = new / "internal/officialegress/catalogdata/runtime"
profiles = sorted(p.name for p in (rt / "profiles").iterdir())
print("  画像目录版本：", profiles)
assert base in profiles and any((rt / "profiles" / base).glob("*.json")), f"缺基线（Active）{base} 画像"
sel = json.loads((rt / "release-catalog.json").read_text())
graph = json.loads((new / "internal/officialegress" / sel["release_graph"]["path"]).read_text())
slots = sorted({(n["mode"], n["snapshot"]["version"]) for n in graph["nodes"]})
print("  release graph 槽位：", slots)
assert ("active", base) in slots, f"release graph 的 Active 不是 {base}"
print(f"  目标 {target} 画像：" + ("已在 backend" if target in profiles else "不在（预期：目标版本画像要到 VC-3 暂存、VC-4 候选链才进仓库，本步骤不要求）"))
need = re.search(r"^go (\d+)\.(\d+)(?:\.(\d+))?", (new / "go.mod").read_text(), re.M)
have = re.search(r"go(\d+)\.(\d+)(?:\.(\d+))?", subprocess.run(["go", "version"], capture_output=True, text=True).stdout)
import os
if need and have and os.environ.get("SYNC_SKIP_GO") != "1":
    t = lambda m: tuple(int(x or 0) for x in m.groups())
    print("  go.mod 要求", ".".join(x or "0" for x in need.groups()), "宿主", ".".join(x or "0" for x in have.groups()))
    assert t(have) >= t(need), "宿主 Go 低于 go.mod 要求"
PY
echo "$(utc) 换入：${BK} → backend.superseded-${TS}"
mv "${BK}" "${D}/backend.superseded-${TS}" || die "旧目录改名失败（未改动任何内容）"
if ! mv "${NEW}" "${BK}"; then
  mv "${D}/backend.superseded-${TS}" "${BK}" && die "新目录就位失败，已把旧目录改回" || die "新目录就位失败且旧目录改回也失败！请立刻手工处理：${D}/backend.superseded-${TS}"
fi
echo "  已就位；旧目录保留为 ${D}/backend.superseded-${TS}（不删除）"
# 编译冒烟（先例：go build -buildvcs=false；数据根没有 .git，不加该参数会因 VCS 状态 exit 128，而 go run 不受影响）
SMOKE=skipped
if [ "${SYNC_SKIP_GO:-0}" != 1 ]; then
  echo "$(utc) 编译冒烟（nice 19，单进程）"
  if ( cd "${BK}" && nice -n 19 go build -buildvcs=false -p 1 -o /dev/null ./cmd/egresscatalogstage ) \
     && ( cd "${BK}" && go run ./cmd/egresscatalogstage -h >/dev/null 2>&1 ); then
    SMOKE=passed; echo "  go build 与 go run 都通过"
  else
    SMOKE=failed; echo "  编译冒烟失败！退回命令：bash $0 --rollback ${TS}（旧目录还在）；先看是不是缺模块缓存（go.sum 变了需要联网）"
  fi
fi
# 收据（留档在 ${WORK}，不写数据根）
python3 -B - "${WORK}/receipt-${TS}.json" "${TS}" "${ENTRY_COMMIT}" "${D}" "${SMOKE}" "${RUNDIR}/actual.tsv" <<'PY'
import hashlib, json, sys
from pathlib import Path
out, ts, commit, d, smoke, tsv = sys.argv[1:]
rows = Path(tsv).read_text().splitlines()
Path(out).write_text(json.dumps({"timestamp": ts, "entry_commit": commit, "new_backend": f"{d}/backend",
    "superseded": f"{d}/backend.superseded-{ts}", "file_count": len(rows), "tree_manifest_sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest(),
    "go_smoke": smoke}, ensure_ascii=False, indent=2) + "\n")
print("收据：", out)
PY
# 清临时工作目录（Python 里删，路径限定在 ${WORK} 之下）
python3 -B - "${WORK}" "${RUNDIR}" <<'PY'
import shutil, sys
from pathlib import Path
work, run = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
assert run.parent == work and run.name.startswith("run-"), run
shutil.rmtree(run)
print("已清理临时目录", run)
PY
[ "${SMOKE}" = failed ] && exit 1
echo "SYNC_BACKEND_DONE ts=${TS} commit=${ENTRY_COMMIT:0:9} smoke=${SMOKE}"
