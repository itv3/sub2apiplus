#!/bin/bash
# 对指定提交做“受监督部署”（或只校验不部署）。采集主机侧脚本，随驱动安装在 <驱动目录>/pipeline/deploy-commit.sh，
# 由本机 local/upload-and-deploy.sh 上传暂存包后远程执行；不要在本机运行。第 4 步会重装驱动（含本目录的流水线脚本），所以部署前不得有流水线在跑。
#
# 用法：
#   bash deploy-commit.sh <40 位提交> <本机算出的 tool_digest(64 位)>                 # 真部署
#   bash deploy-commit.sh <40 位提交> <本机算出的 tool_digest(64 位)> --verify-only   # 只校验、不动数据根
# 输入：<PREP>/stage/stage-<提交前12位>.tgz 与同名 .sha256（本机 git archive docs tools 后上传）。
# 环境变量：STAGE_NAME 暂存树目录名（缺省 codex-<TARGET_VERSION>-managed-tools，位于数据根 staging 下）。
#
# --verify-only 做什么（部署器没有 dry-run，所以自己拼）：
#   1. 解到 <PREP>/verify-stage/ 下的独立目录（不动数据根，不改名旧暂存树）；
#   2. 收紧权限、建 docs/repository-docs，和真部署完全同一套；
#   3. 用被部署提交自带的 arm64_supervised_deploy.py 复现 _preflight 里“只依赖暂存树内容”的全部核对：
#      受管工具摘要、监督器摘要、assertion 入口摘要、两个受管根的属主/写权限/符号链接、四份活动文档与归档副本一致、
#      运行时依赖文档、场景清单与活动规格第二章摘要、pre-A3 场景入口、Codex/Claude 工具身份计算、驱动清单 --check；
#   不覆盖的只有依赖现场的项（docker inspect 容器网络、出口守护实时状态）——那两项真部署时才查。
# 真部署做什么：同上 1～3（换暂存树，旧的改名 .superseded-<UTC>），再受监督部署两个客户端（--client codex claude，
#   --managed-roots 两个受管根），核对生产树/收据，重装驱动并 verify。
# 不删除任何文件；失败即停（set -e）。
set -Eeuo pipefail
umask 077
# 第 4 步会用暂存树重装驱动（包括本脚本所在的目录），而 bash 是边读边执行脚本的：先把自己复制到临时位置再从副本运行，避免读到错位内容。
if [ -z "${DEPLOY_COMMIT_COPY:-}" ]; then
  SELF_DIR=$(cd "$(dirname "$0")" && pwd)
  COPY=$(mktemp /var/tmp/deploy-commit.XXXXXX)
  cp "$0" "${COPY}"
  DEPLOY_COMMIT_COPY=1 DEPLOY_COMMIT_HOME=${SELF_DIR} exec bash "${COPY}" "$@"
fi
source "${DEPLOY_COMMIT_HOME}/pipeline-lib.sh"
# 一轮的第一次部署发生在本轮参数文件定稿之前：参数文件存在就用它，不存在则退回 pipeline.env 里的 PIPE_PREP／PIPE_DATA_ROOT，并要求显式给 STAGE_NAME
if [ -n "${ARM64_VC_ENV:-}" ] && [ -f "${ARM64_VC_ENV}" ]; then
  pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV}"; exit 2; }
  pl_defaults
else
  : "${PIPE_PREP:?本轮参数文件尚不存在时，必须在 pipeline.env 里给 PIPE_PREP}"
  : "${STAGE_NAME:?本轮参数文件尚不存在时，必须用环境变量 STAGE_NAME 指定暂存树目录名}"
  PREP=${PIPE_PREP}; D=${PIPE_DATA_ROOT:-/root/docker/capture-cli/data}
fi

C=${1:?"用法：deploy-commit.sh <40位提交> <tool_digest> [--verify-only]"}
EXPECT=${2:?"缺少本机算出的 tool_digest"}
MODE=${3:-deploy}
[[ ${C} =~ ^[0-9a-f]{40}$ ]] || { echo "提交必须是 40 位小写十六进制"; exit 2; }
[[ ${EXPECT} =~ ^[0-9a-f]{64}$ ]] || { echo "tool_digest 必须是 64 位小写十六进制"; exit 2; }
case "${MODE}" in deploy|--verify-only) ;; *) echo "第三个参数只能是 --verify-only"; exit 2 ;; esac

P=${PREP}
DRIVER_INSTALL_ROOT=$(dirname "${PIPE_DRIVER_DIR}")
STAGE_NAME=${STAGE_NAME:-codex-${TARGET_VERSION:?}-managed-tools}
TGZ=${P}/stage/stage-${C:0:12}.tgz
STAMP=$(date -u +%Y%m%dt%H%M%Sz)
LOGDIR=${P}/logs
mkdir -p "${LOGDIR}"
LOG=${LOGDIR}/deploy-${C:0:12}-${STAMP}-${MODE#--}.log
exec > >(tee -a "${LOG}") 2>&1
echo "== 开始 ${STAMP} 提交=${C} 模式=${MODE} 日志=${LOG}"

# ---------- 0. 前置 ----------
[ -f "${TGZ}" ] && [ -f "${TGZ}.sha256" ] || { echo "缺少 ${TGZ} 或其 .sha256"; exit 3; }
( cd "${P}/stage" && sha256sum -c "stage-${C:0:12}.tgz.sha256" )
if pgrep -af 'unit_executor|entry-gates|background_validation|entry_orchestrator|entry-dryrun|vc-pipeline.sh|start-entry.sh|evolve-and-resume.sh' | grep -v pgrep >/dev/null; then
  if [ "${MODE}" = deploy ]; then echo "有在跑的验证，停止（真部署会改动执行器看到的受管工具）"; exit 3; fi
  echo "提示：有验证在跑；--verify-only 不改数据根，继续"
fi
AVAIL_KB=$(df --output=avail -k / | tail -1 | tr -d ' ')
echo "根盘可用 $((AVAIL_KB/1024/1024)) GiB"
if [ "${MODE}" = deploy ] && [ "${AVAIL_KB}" -lt $((5*1024*1024)) ]; then echo "根盘可用不足 5 GiB，停止"; exit 3; fi

# ---------- 1. 展开暂存树 ----------
if [ "${MODE}" = deploy ]; then
  STAGE=${D}/staging/${STAGE_NAME}
  if [ -e "${STAGE}" ]; then
    [ ! -e "${STAGE}.superseded-${STAMP}" ]
    echo "== 1. 旧暂存树改名为 ${STAGE}.superseded-${STAMP}"
    mv "${STAGE}" "${STAGE}.superseded-${STAMP}"
  fi
else
  mkdir -p -m 700 "${P}/verify-stage"
  STAGE=${P}/verify-stage/${C:0:12}-${STAMP}
  echo "== 1. 校验用独立暂存目录 ${STAGE}（不动数据根）"
fi
mkdir "${STAGE}" && chmod 700 "${STAGE}"
tar -xzf "${TGZ}" --no-same-owner -C "${STAGE}"
# 四份活动文档的归档副本：部署器预检要求 docs/repository-docs 存在且与运行副本逐字相同（官方做法见驱动 fix-and-continue.sh）
( umask 022 && install -d -m 755 "${STAGE}/docs/repository-docs" \
  && for doc in OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md CODEX_CLI_CLIENT_EMULATION_GUIDE.md CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md; do
       cp -p "${STAGE}/docs/${doc}" "${STAGE}/docs/repository-docs/${doc}"; done \
  && chmod 644 "${STAGE}"/docs/repository-docs/*.md )
# git archive 默认 tar.umask=002 会存成 664/775，部署器 reject_untrusted_tree 拒收：解包后必须收紧
chmod -R go-w "${STAGE}"; chown -R root:root "${STAGE}"; chmod 700 "${STAGE}"
UNSAFE=$(find "${STAGE}" \( ! -user root -o ! -group root -o -perm /022 \) | wc -l | tr -d ' ')
LINKS=$(find "${STAGE}/tools" "${STAGE}/docs" -type l | wc -l | tr -d ' ')
PYC=$(find "${STAGE}" -name __pycache__ | wc -l | tr -d ' ')
echo "不安全项=${UNSAFE}（应 0） 符号链接=${LINKS}（应 0） __pycache__=${PYC}（应 0） 可执行位=$(find "${STAGE}/tools/official_client_capture" -type f -perm /111 | wc -l | tr -d ' ')"
[ "${UNSAFE}" = 0 ] && [ "${LINKS}" = 0 ] && [ "${PYC}" = 0 ] || { echo "暂存树不合格，停止"; exit 3; }

# ---------- 2. 摘要与内容预检（部署器自带函数，只读） ----------
echo "== 2. 摘要核对与暂存树内容预检"
PYTHONDONTWRITEBYTECODE=1 python3 -B - "${STAGE}" "${EXPECT}" <<'PY'
import importlib.util, sys
from pathlib import Path

sys.dont_write_bytecode = True
stage, expect = Path(sys.argv[1]), sys.argv[2]
spec = importlib.util.spec_from_file_location("dep_under_check", stage / "tools" / "arm64_supervised_deploy.py")
m = importlib.util.module_from_spec(spec)
sys.modules["dep_under_check"] = m
spec.loader.exec_module(m)

def step(label, value=""):
    print(f"  [通过] {label} {value}".rstrip())

tool = stage / "tools" / "official_client_capture"
control = stage / "tools" / "official_client_control"
digest, count = m.tool_digest(tool)
if digest != expect:
    raise SystemExit(f"摘要与本机算出的不一致（暂存 {digest}，期望 {expect}），停止")
step("受管工具摘要 = 本机算出值", f"{digest[:16]}… 文件 {count}")
if m.file_sha256(tool / "codex_upgrade_supervisor.py") != m.DEFAULT_SUPERVISOR_DIGEST:
    raise SystemExit("暂存监督器摘要与部署器默认值不符")
step("监督器摘要", m.DEFAULT_SUPERVISOR_DIGEST[:16] + "…")
n = m.reject_untrusted_tree(tool); step("official_client_capture 属主/写权限/符号链接", f"{n} 项")
n = m.reject_untrusted_tree(control); step("official_client_control 属主/写权限/符号链接", f"{n} 项")
prep = stage / "tools" / m.MANAGED_ASSERTION_PREPARER
m.reject_untrusted_file(prep, label="暂存 assertion bundle 入口")
if m.file_sha256(prep) != m.DEFAULT_ASSERTION_PREPARER_DIGEST:
    raise SystemExit("暂存 assertion bundle 入口摘要不符")
step("assertion bundle 入口摘要")
documents, runtime_documents = m.transaction_documents(["codex", "claude"])
archive = stage / "docs" / "repository-docs"
if archive.is_symlink() or not archive.is_dir():
    raise SystemExit("docs/repository-docs 不存在或不可信")
for name in documents:
    run, arc = stage / "docs" / name, archive / name
    m.reject_untrusted_file(run, label="暂存活动文档"); m.reject_untrusted_file(arc, label="暂存归档文档")
    if m.file_sha256(run) != m.file_sha256(arc):
        raise SystemExit(f"运行副本与归档副本不一致：{name}")
step("活动文档运行副本 = 归档副本", f"{len(documents)} 份")
for name in runtime_documents:
    path = m.managed_document_path(stage / "docs", name, label="暂存运行时依赖文档", allow_missing=False)
    m.reject_untrusted_file(path, label="暂存运行时依赖文档")
step("运行时依赖文档齐全", f"{len(runtime_documents)} 份")
spec_info = m.verify_scenario_source_spec(stage, tool)
step("目标场景清单 source_spec 摘要 = 活动规格第二章", spec_info["source_spec_sha256"][:16] + "…")
pre = m.verify_pre_a3_scenario_entries(tool)
step("pre-A3 场景入口齐全", str({k: (len(v) if hasattr(v, "__len__") else v) for k, v in pre.items()})[:160])
step("目标 Codex 版本（取自目标场景清单）", m.read_target_codex_version(tool))
ident = m.staging_identity_v2(tool)
step("Codex 工具身份（四摘要）", ident["policy_sha256"][:16] + "…")
claude = m.claude_identity(stage)
step("Claude 工具身份", claude["tool_files_sha256"][:16] + "…")
print("PRECHECK_OK")
PY
echo "== 2b. 驱动清单与源码目录一致（--check）"
python3 -B "${STAGE}/tools/arm64_capture_driver/install.py" build-manifest --root "${STAGE}/tools/arm64_capture_driver" --check
[ "$(find "${STAGE}" -name __pycache__ | wc -l | tr -d ' ')" = 0 ] || { echo "预检在暂存树里留下了 __pycache__，停止"; exit 3; }

if [ "${MODE}" = "--verify-only" ]; then
  echo "== 只校验模式结束：数据根未改动；校验目录 ${STAGE} 可手工清理（约 100 MB）"
  echo VERIFY_ONLY_OK
  exit 0
fi

# ---------- 3. 受监督部署（两个客户端、两个受管根） ----------
echo "== 3. 受监督部署"
MARK=${LOGDIR}/.mark-${STAMP}
touch "${MARK}"
OLD_DIGEST=$(PYTHONDONTWRITEBYTECODE=1 python3 -B - "${STAGE}" "${D}/tools/official_client_capture" <<'PY'
import importlib.util, sys
from pathlib import Path
sys.dont_write_bytecode = True
stage = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("dep_old", stage / "tools" / "arm64_supervised_deploy.py")
m = importlib.util.module_from_spec(spec); sys.modules["dep_old"] = m; spec.loader.exec_module(m)
print(m.tool_digest(Path(sys.argv[2]))[0])
PY
)
echo "部署前生产树摘要 ${OLD_DIGEST}"
cd "${STAGE}"
python3 -B tools/arm64_supervised_deploy.py --staging-root "${STAGE}" \
  --client codex claude --managed-roots tools/official_client_capture tools/official_client_control \
  --expected-tool-digest "${EXPECT}" | cut -c1-400

# ---------- 3b. 部署后核对 ----------
echo "== 3b. 部署后核对"
PYTHONDONTWRITEBYTECODE=1 python3 -B - "${STAGE}" "${D}" "${EXPECT}" "${MARK}" <<'PY'
import importlib.util, json, os, sys
from pathlib import Path
sys.dont_write_bytecode = True
stage, data, expect, mark = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
spec = importlib.util.spec_from_file_location("dep_post", stage / "tools" / "arm64_supervised_deploy.py")
m = importlib.util.module_from_spec(spec); sys.modules["dep_post"] = m; spec.loader.exec_module(m)
digest, count = m.tool_digest(data / "tools" / "official_client_capture")
assert digest == expect, f"生产树摘要 {digest} != 期望 {expect}"
print(f"  [通过] 生产树 tool_digest = 期望值 文件 {count}")
for root in ("official_client_capture", "official_client_control"):
    a = m.tree_content_sha256(stage / "tools" / root)
    b = m.tree_content_sha256(data / "tools" / root)
    assert a == b, f"{root} 生产树与暂存树整树内容（含 tests）不一致"
    print(f"  [通过] {root} 生产树 = 暂存树（含 tests 目录）")
ctrl = data / "control"
fresh = lambda pat: sorted(p for p in ctrl.glob(pat) if p.stat().st_mtime >= mark.stat().st_mtime)
codex, claude = fresh("codex-*-supervisor-enable-*.json"), fresh("claude-managed-deploy-*.json")
assert len(codex) == 1 and len(claude) == 1, f"新收据数量异常 codex={len(codex)} claude={len(claude)}"
cj, lj = json.loads(codex[0].read_text()), json.loads(claude[0].read_text())
assert cj["status"] == "passed" and cj["tool_files_sha256"] == expect, "Codex 收据状态或摘要不符"
assert lj["status"] == "passed", "Claude 收据状态不符"
print("  [通过] 新收据", codex[0].name, claude[0].name)
for doc in m.transaction_documents(["codex", "claude"])[0]:
    assert m.file_sha256(stage / "docs" / doc) == m.file_sha256(data / "docs" / doc), f"生产文档与暂存不一致：{doc}"
print("  [通过] 生产活动文档 = 暂存文档")
print("POSTCHECK_OK")
PY
echo "生产树可执行位=$(find "${D}/tools/official_client_capture" -type f -perm /111 | wc -l | tr -d ' ')（暂存树 $(find "${STAGE}/tools/official_client_capture" -type f -perm /111 | wc -l | tr -d ' ')）"
echo "生产树 __pycache__=$(find "${D}/tools" -name __pycache__ | wc -l | tr -d ' ')（应 0）"

# ---------- 4. 驱动重装并复验 ----------
echo "== 4. 驱动重装并复验"
python3 -B "${STAGE}/tools/arm64_capture_driver/install.py" install --source "${STAGE}/tools/arm64_capture_driver" --target "${DRIVER_INSTALL_ROOT}" --data-root "${D}" | cut -c1-500
python3 -B "${DRIVER_INSTALL_ROOT}/install.py" verify --target "${DRIVER_INSTALL_ROOT}" --data-root "${D}" | cut -c1-500
echo "驱动 __pycache__=$(find "${DRIVER_INSTALL_ROOT}" -name __pycache__ | wc -l | tr -d ' ')（应 0）"
# ---------- 5. 同步数据根的部署脚本副本（起跑前置检查 redeploy 项要求它与本提交逐字相同；PIPE 路 10-26 补）----------
echo "== 5. 同步 ${D}/tools/arm64_supervised_deploy.py"
if ! cmp -s "${STAGE}/tools/arm64_supervised_deploy.py" "${D}/tools/arm64_supervised_deploy.py"; then
  cp -p "${D}/tools/arm64_supervised_deploy.py" "${D}/tools/arm64_supervised_deploy.py.bak-before-${C:0:9}"
  install -m 700 -o root -g root "${STAGE}/tools/arm64_supervised_deploy.py" "${D}/tools/arm64_supervised_deploy.py"
  echo "已同步（旧副本备份为 .bak-before-${C:0:9}）"
else
  echo "副本已与本提交相同"
fi
df -h / | tail -1
echo DEPLOY_OK
