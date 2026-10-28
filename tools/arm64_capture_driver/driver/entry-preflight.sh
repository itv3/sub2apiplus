#!/bin/bash
# 入口便宜检查（E1-02）：排在 pre-A3 认证（ARM64 4 核约 8 分钟）与建计时账本之前，把几秒到一两分钟就能查出的错误一次查全。
#   各项互相独立，一项失败其余照查，最后汇总；全部通过退出 0，任一失败退出 1。
#   零请求：不建账本、不建 Campaign、不签收据；环境探针只在临时目录试采，结束即删除，不落正式收据。
# 检查项：
#   required-parameters  后续阶段要用、参数文件里可缺省的身份参数已填写（不是空值或 REPLACE_ 占位）；回退依据
#                        P0_ROLLBACK_EVIDENCE 还要是已有的普通文件（P0 收据一步读它）；
#   guard-pre-plan       驱动安装复验、指定出口实时准入、磁盘余量（与 stage1 建账本前同一守卫）；
#   deployment-identity  最新受监督部署收据的策略与五摘要等于当前受管工具树；
#   target-client        目标客户端两处安装：宿主机与采集容器内同一路径，摘要与版本等于本轮登记值（Claude 另核断网合成容器
#                        capture-cli-synthetic 内同一路径的摘要）；
#   official-package     官方包摘要等于登记值，基线与目标源码树就位（Claude 没有源码树：按 B2-01 冻结合同复核目标版本的
#                        冻结目录）；
#   plan-audit           plan --audit-only：参数、官方包、源码、规则与场景清单、作业 covers、覆盖计划、基线证据、执行副本（Claude：
#                        参数、官方包、规则／场景清单／证据标签与 Job 展开、工具身份）；
#   environment-probe    ARM64 环境收据在临时目录试采并封存（采集器与出口、磁盘、TLS 探针当下可用）；
#   model-availability   模型可用性探测（优化第 14 项）：向中转拉一次模型列表，本轮的 MAIN_MODEL／LITE_MODEL 必须都在（模型被下线当场拦下）。探测地址与密钥文件取调用环境的
#                        MODEL_PROBE_URL（含 /v1）与 MODEL_PROBE_KEY_FILE（0600 文件）；MODEL_PROBE_MINIMAL 非空时另发最小请求；没配置也算未通过，
#                        确需跳过必须显式设 OPENING_CHECKS_SKIP=model-availability 并同时写 OPENING_CHECKS_SKIP_REASON（理由必填，缺了拒绝）；
#                        被跳过的项在 results.txt 与 summary.json 里记为 skipped（带理由），不是 passed；
#   main-security-scan   main 上的 Security Scan 先核绿（优化第 14 项）：main 当前头对应的运行必须已完成、success、不超过 36 小时；仓库取调用环境的
#                        SECURITY_SCAN_REPO（owner/name，本仓库的 gh 默认会查到上游，必须显式给），要有登录好的 gh；没配置同样算未通过，跳过同上。
#   local-capture-receipt   要进入入口门禁的修复提交（ENTRY_COMMIT），必须已有“本机完整 make test-capture-tools 通过”的本地收据（优化第 4 项；local/local-capture-tools-receipt.sh
#                        生成，收据放 $RUNROOT/local-receipts/）：提交号一致、命令是完整目标、退出码 0、工作树干净、封口对得上。上一轮第三轮入口门禁就红在没跑完整测试的修复提交上；
#   tui-prelaunch-receipt   新版二进制 TUI 零请求启动预探收据（优化第 5 项；tui-prelaunch-receipt.sh 生成）：版本等于本轮目标版本、二进制摘要等于登记值、必需场景
#                        （compact、ws-idle-close、daemon、guardian）齐全且全部通过、零真实请求。两项只对 Codex 做（Claude 记为 skipped）；跳过规矩同上两项（OPENING_CHECKS_SKIP + 理由）。
# 按客户端（Claude 升级 B5a-06、B5a-09）：必填参数、部署收据与五摘要、目标客户端的参数键与另核的容器、官方包的复核模块、
#   plan-audit 的参数模板、环境收据模块与探针版本参数取客户端表（lib.sh 给出 CT_ 变量）；Codex 的取值与改造前逐字相同。
# 用法：ARM64_VC_ENV=$RUNROOT/env.sh bash entry-preflight.sh（由 pre-a3.sh、stage1.sh 与入口编排器 entry.sh 开头调用，也可单独运行）
# 输出目录默认 $RUNROOT/entry-preflight/<UTC>，环境变量 ENTRY_PREFLIGHT_OUT 可指定（编排器用）；结束时写 summary.json（逐项状态，
# 编排器据此判断哪些步骤被阻塞，例如部署绑定没过时依赖它的认证与门禁）。
set -Euo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
OUT="${ENTRY_PREFLIGHT_OUT:-$RUNROOT/entry-preflight/$(date -u +%Y%m%dt%H%M%Sz)}"
install -d -m 700 "$RUNROOT/entry-preflight" "$OUT"
FAILED=()
# 字节码共享层（E2-02）：入口第一步，数据根受管树变了才重建；之后本脚本的检查项、pre-A3、建账本等子命令只读使用。
# 不放进管道里调用：它要在本 shell 里导出 PYTHONPYCACHEPREFIX。
echo "== bytecode-cache $(utc_now)"
prepare_managed_bytecode > "$OUT/bytecode-cache.log" 2>&1
tail -n 2 "$OUT/bytecode-cache.log" | cut -c1-300
check() { # <名称> <函数>：各项互不依赖，一项失败不中断其余
  local name=$1 rc=0 start
  start=$(date +%s)
  echo "== ${name} $(utc_now)"
  # 放进子 shell：某一项引用了参数文件里缺的变量（set -u）或中途 exit，只算这一项失败，其余照查、汇总照写（E2-06 验收发现）。
  rm -f "$OUT/$name.skipped"
  ( "$2" ) > "$OUT/$name.log" 2>&1 || rc=$?
  tail -n 6 "$OUT/$name.log" | cut -c1-300
  # 显式跳过（opening_skipped 写下 $name.skipped 并让这一项返回 0）单独记为 skipped：没做检查就不能写成 passed。
  local status=failed
  if [ "$rc" = 0 ]; then if [ -f "$OUT/$name.skipped" ]; then status=skipped; else status=passed; fi; fi
  printf '%s %s\n' "$name" "$status" >> "$OUT/results.txt"
  if [ "$status" = skipped ]; then
    echo "已跳过 ${name}（未做检查，不是通过；理由见 $OUT/$name.skipped）"
  elif [ "$rc" = 0 ]; then
    echo "通过 ${name}（$(( $(date +%s) - start )) 秒）"
  else
    echo "未通过 ${name}（rc=${rc}，$(( $(date +%s) - start )) 秒，日志 $OUT/$name.log）"
    FAILED+=("$name")
  fi
}

required_parameters() {
  local key missing=()
  # 逐个核对的键取客户端表（Codex 即改造前这一份：code-mode-host、采集镜像、CODEX_BIN 三项……P0_ROLLBACK_EVIDENCE）。
  for key in "${CT_PREFLIGHT_REQUIRED[@]}"; do
    if [ -z "${!key:-}" ] || [[ ${!key} == REPLACE_* ]] || [[ ${!key} == */REPLACE_* ]]; then missing+=("$key"); fi
  done
  if [ "${#missing[@]}" != 0 ]; then echo "参数文件缺少或仍是占位：${missing[*]}"; return 1; fi
  # 回退依据是 P0 收据一步要读的现成文件（上一版本可回退点的收据），不是后面才产出的坐标：缺了要在这里报，
  # 不能等到建账本之后的第 15 步才发现（E4-02 验收实测）。
  if [ ! -f "$P0_ROLLBACK_EVIDENCE" ] || [ -L "$P0_ROLLBACK_EVIDENCE" ]; then
    echo "回退依据（P0_ROLLBACK_EVIDENCE）不是已有的普通文件：$P0_ROLLBACK_EVIDENCE"; return 1
  fi
  echo "后续阶段要用的身份参数均已填写"
}

guard_pre_plan() { bash "$DRV/guard.sh" pre-plan; }

latest_deploy() {
  python3 -B - "$DRV/../install.py" "$D" "$CT_CLIENT" <<'PY'
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("installed_driver", sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
print(module.latest_deploy_receipt(Path(sys.argv[2]) / "control", sys.argv[3])[0])
PY
}

deployment_identity() {
  local deploy
  deploy=$(latest_deploy) || return 1
  python3 -B - "$deploy" ${CT_IDENTITY_ARGS[@]+"${CT_IDENTITY_ARGS[@]}"} <<'PY'
import sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade_policy_certification as certification
# 本轮客户端（Codex 不带参数：调用与改造前相同；Claude 按 v3 引擎算身份、按 Claude 部署收据合同核对）。
client = sys.argv[2:]
identity = certification.current_identity(*client)
receipt = certification.load_deployment_receipt(Path(sys.argv[1]), expected_identity=identity, **({"client": client[0]} if client else {}))
print("最新部署收据与当前受管工具树一致：", sys.argv[1], receipt.get("created_at_utc"), identity["tool_files_sha256"][:12])
PY
}

target_client() {
  # 二进制与摘要的参数键取客户端表（Codex：CODEX_BIN／CODEX_BIN_SHA256；Claude：CLIENT_BIN／CLIENT_BIN_SHA256）。
  local host container version binary=${!CT_BINARY_KEY} expected=${!CT_BINARY_SHA256_KEY} name extra mismatched=0
  host=$(sha256sum "$binary" | cut -c1-64) || return 1
  container=$(docker exec capture-cli sha256sum "$binary" | cut -c1-64) || return 1
  version=$("$binary" --version 2>&1 | head -n 1) || return 1
  echo "宿主机 ${host}；采集容器 ${container}；登记 ${expected}；版本 ${version}"
  # 客户端表登记的其余容器（Claude：合成 probe 跑在断网容器 capture-cli-synthetic 里，B5a-09）：容器内同一路径也要等于登记值。
  for name in ${CT_BINARY_EXTRA_CONTAINERS[@]+"${CT_BINARY_EXTRA_CONTAINERS[@]}"}; do
    extra=$(docker exec "$name" sha256sum "$binary" | cut -c1-64) || return 1
    echo "${name} ${extra}"
    [ "$extra" = "$expected" ] || mismatched=1
  done
  [ "$host" = "$expected" ] && [ "$container" = "$expected" ] && [[ $version == *"$TARGET_VERSION"* ]] && [ "$mismatched" = 0 ]
}

official_package() {
  local actual
  actual=$(sha256sum "$TARGET_PACKAGE" | cut -c1-64) || return 1
  echo "官方包 ${actual}；登记 ${OFFICIAL_ASSET_SHA256}"
  [ "$actual" = "$OFFICIAL_ASSET_SHA256" ] || return 1
  # 客户端表登记了官方包复核模块的客户端（Claude，B5a-09）：官方包是 B2-01 冻结收据，按冻结合同只读复核目标版本的冻结目录
  # （收据合同、目标版本、两渠道等价、二进制摘要等于本轮登记值、目录内每个登记文件的摘要）；Claude 没有官方源码树。
  if [ -n "$CT_OFFICIAL_PACKAGE_VERIFIER" ]; then
    python3 -m "$CT_OFFICIAL_PACKAGE_VERIFIER" verify --freeze "$TARGET_PACKAGE" --target-version "$TARGET_VERSION" \
      --binary-sha256 "${!CT_BINARY_SHA256_KEY}" --freeze-sha256 "$OFFICIAL_ASSET_SHA256"
    return
  fi
  test -d "$BASELINE_SOURCE" && test ! -L "$BASELINE_SOURCE" && test -d "$TARGET_SOURCE" && test ! -L "$TARGET_SOURCE" && echo "基线与目标源码树就位"
}

plan_audit_from_table() {
  # 客户端表的 plan --audit-only 参数模板（Claude，B5a-09）：以 $ 开头的项代入同名参数（缺了代入空值，由审计的参数项报出，
  # 其余各项照查），其余照抄；作业坐标与 Codex 同一形式。
  local item name argv=()
  for item in "${CT_PLAN_AUDIT_ARGV[@]}"; do
    if [[ $item == \$* ]]; then name=${item#\$}; argv+=("${!name:-}"); else argv+=("$item"); fi
  done
  python3 -m "$CT_ORCHESTRATOR_MODULE" plan --audit-only \
    --campaign-dir "$D/staging/entry-preflight-$(date -u +%Y%m%dt%H%M%Sz)/campaign" --campaign-id "${CAMPAIGN_PREFIX}-plan-audit" "${argv[@]}"
}

plan_audit() {
  local rc
  # 客户端表给出了参数模板的客户端（Claude）按模板展开调用编排器；Codex 的模板为空，走下面原来的参数形状。
  if [ "${#CT_PLAN_AUDIT_ARGV[@]}" != 0 ]; then
    plan_audit_from_table > "$OUT/plan-audit.json"; rc=$?
  else
  # 拟定路径只作作业坐标与输出路径校验，不会被创建；审计不读账本与环境收据（建账本之后才有）。
  # 参数文件可缺省的两个身份参数缺了时传空值：审计的参数项报出来，清单、源码等其余各项照查（不能因缺参数跳过 covers 等）。
  # 下面是 Codex 编排器 plan 的参数形状（编排器模块取客户端表）。
  python3 -m "$CT_ORCHESTRATOR_MODULE" plan --audit-only \
    --campaign-dir "$D/staging/entry-preflight-$(date -u +%Y%m%dt%H%M%Sz)/campaign" --campaign-id "${CAMPAIGN_PREFIX}-plan-audit" \
    --baseline-version "$BASELINE_VERSION" --target-version "$TARGET_VERSION" \
    --campaign-mode preflight_only --campaign-purpose production_replacement \
    --baseline-source "$BASELINE_SOURCE" --target-source "$TARGET_SOURCE" --baseline-evidence "$ACTIVE_PROFILE" \
    --target-sha256 "$CODEX_BIN_SHA256" --target-package "$TARGET_PACKAGE" --target-package-sha256 "$OFFICIAL_ASSET_SHA256" \
    --target-code-mode-host-sha256 "${TARGET_CODE_MODE_HOST_SHA256:-}" --runtime-image "${CAPTURE_RUNTIME_IMAGE:-}" \
    --rule-manifest "$BASELINE_RULES_JSON" --scenario-manifest "$BASELINE_SCENARIOS_JSON" --target-scenario-manifest "$SCENARIOS_JSON" \
    --capture-codex-bin "$CODEX_BIN" --relay-codex-bin "$CODEX_BIN" --suite full --model "$MAIN_MODEL" --lite-model "$LITE_MODEL" \
    --codex-account-id "$CODEX_ACCOUNT_ID" --api-key-id "$API_KEY_ID" --live-attestation-compose-dir "$COMPOSE_DIR" \
    --live-attestation-compose-files "$COMPOSE_DIR/docker-compose.yml" > "$OUT/plan-audit.json"
  rc=$?
  fi
  python3 -B - "$OUT/plan-audit.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
for item in result["checks"]:
    detail = item.get("error") or item.get("blocked_by") or item.get("detail") or ""
    print(f"{item['status']:8s} {item['name']}  {str(detail)[:240]}")
print("未校验（建账本之后由 plan 校验）：", "、".join(result["unchecked"]))
PY
  return "$rc"
}

environment_probe() {
  local probe rc=0
  probe=$(mktemp -d "$D/staging/entry-preflight-env-XXXXXX") || return 1
  # 采集器与探针的版本参数取客户端表（Codex：--rust-tls-codex-version；Claude：--claude-version），都交本轮 TARGET_VERSION。
  python3 -m "$CT_ENVIRONMENT_RECEIPT_MODULE" collect --evidence-root "$probe" --output facts.json --phase p0 --subject-id "$UP-entry-preflight" "$CT_ENVIRONMENT_PROBE_VERSION_OPTION" "$TARGET_VERSION" | cut -c1-160 || rc=$?
  if [ "$rc" = 0 ]; then
    python3 -m "$CT_ENVIRONMENT_RECEIPT_MODULE" finalize --evidence-root "$probe" --facts facts.json --output receipt.json | cut -c1-160 || rc=$?
  fi
  rm -rf "$probe"
  return "$rc"
}

# 开工检查补的两项外部事实检查（优化第 14 项，helper 是同目录的 opening_checks.py）：退出码 1＝检查没过，3＝没配置；两种都算未通过，要跳过必须显式写明。
# 跳过的规矩（验收修正必修 3）：OPENING_CHECKS_SKIP 列出项目名、同时写 OPENING_CHECKS_SKIP_REASON（理由必填）才算跳过，且记为 skipped 不是 passed；
# 列了项目却没写理由，拒绝跳过、这一项按未通过处理。返回 0＝已按声明跳过（写下 $OUT/<项目>.skipped，内容是理由）；1＝没有声明跳过；2＝声明了却没写理由。
opening_skipped() {
  case ",${OPENING_CHECKS_SKIP:-}," in *",$1,"*) ;; *) return 1 ;; esac
  local reason="${OPENING_CHECKS_SKIP_REASON:-}"
  if [ -z "${reason//[[:space:]]/}" ]; then
    echo "拒绝跳过 $1：OPENING_CHECKS_SKIP 里列了它，却没写 OPENING_CHECKS_SKIP_REASON（理由必填）；这一项按未通过处理"
    return 2
  fi
  printf '%s\n' "$reason" > "$OUT/$1.skipped"
  echo "已按 OPENING_CHECKS_SKIP 显式跳过 $1（未做检查，不是通过）；理由：$reason"
  return 0
}

model_availability() {
  local rc=0 skip=0
  opening_skipped model-availability || skip=$?
  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi
  python3 -B "$DRV/opening_checks.py" models --models "${MAIN_MODEL},${LITE_MODEL}" ${MODEL_PROBE_MINIMAL:+--minimal} || rc=$?
  if [ "$rc" = 3 ]; then echo "模型可用性探测未配置：设 MODEL_PROBE_URL 与 MODEL_PROBE_KEY_FILE 后重跑；确需跳过请显式 OPENING_CHECKS_SKIP=model-availability 并同时写 OPENING_CHECKS_SKIP_REASON"; fi
  return "$rc"
}

main_security_scan() {
  local rc=0 skip=0
  opening_skipped main-security-scan || skip=$?
  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi
  python3 -B "$DRV/opening_checks.py" security-scan ${SECURITY_SCAN_REPO:+--repo "$SECURITY_SCAN_REPO"} || rc=$?
  if [ "$rc" = 3 ]; then echo "Security Scan 核对未配置：设 SECURITY_SCAN_REPO=owner/name（并保证 gh 已登录）后重跑；确需跳过请显式 OPENING_CHECKS_SKIP=main-security-scan 并同时写 OPENING_CHECKS_SKIP_REASON"; fi
  return "$rc"
}

# 起跑前两张收据（优化第 4、5 项，helper 是同目录的 opening_checks.py；收据目录 $RUNROOT/local-receipts）。只对 Codex 做；Claude 没有这两条流程，记为 skipped（未做检查，不是通过）。
receipt_not_applicable() {
  if [ "$CT_CLIENT" = codex ]; then return 1; fi
  printf '%s\n' "本轮客户端 ${CT_CLIENT} 没有这条流程，未做检查" > "$OUT/$1.skipped"
  echo "跳过 $1：本轮客户端 ${CT_CLIENT} 不适用（未做检查，不是通过）"
  return 0
}

local_capture_receipt() {
  local rc=0 skip=0
  opening_skipped local-capture-receipt || skip=$?
  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi
  if receipt_not_applicable local-capture-receipt; then return 0; fi
  if [ -z "${ENTRY_COMMIT:-}" ]; then echo "参数文件没有 ENTRY_COMMIT（要进入入口门禁的提交），无法核对本地收据"; return 1; fi
  python3 -B "$DRV/opening_checks.py" capture-receipt-check --dir "$RUNROOT/local-receipts" --commit "$ENTRY_COMMIT" || rc=$?
  return "$rc"
}

tui_prelaunch_receipt() {
  local rc=0 skip=0
  opening_skipped tui-prelaunch-receipt || skip=$?
  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi
  if receipt_not_applicable tui-prelaunch-receipt; then return 0; fi
  python3 -B "$DRV/opening_checks.py" tui-receipt-check --dir "$RUNROOT/local-receipts" --version "$TARGET_VERSION" --binary-sha256 "${!CT_BINARY_SHA256_KEY}" || rc=$?
  return "$rc"
}

echo "ENTRY_PREFLIGHT_START $(utc_now) round=${ROUND} stamp=${STAMP} out=${OUT}"
check required-parameters required_parameters
check guard-pre-plan guard_pre_plan
check deployment-identity deployment_identity
check target-client target_client
check official-package official_package
check plan-audit plan_audit
check environment-probe environment_probe
check model-availability model_availability
check main-security-scan main_security_scan
check local-capture-receipt local_capture_receipt
check tui-prelaunch-receipt tui_prelaunch_receipt
python3 -B - "$OUT/results.txt" "$OUT/summary.json" "$OUT" <<'PY'
import json, os, sys
checks = dict(line.split() for line in open(sys.argv[1], encoding="utf-8") if line.strip())
# 状态只有三种：passed（做了检查且通过）、skipped（显式跳过，带理由，没做检查）、failed。跳过不算失败，但永远不写成 passed。
failed = [name for name, status in checks.items() if status not in ("passed", "skipped")]
skipped = {}
for name, status in checks.items():
    if status == "skipped":
        with open(os.path.join(sys.argv[3], name + ".skipped"), encoding="utf-8") as handle:
            skipped[name] = handle.read().strip()
summary = {"schema_version": "entry-preflight-summary/v1", "status": "failed" if failed else "passed",
           "checks": checks, "failed": failed, "skipped": skipped}
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(summary, handle, ensure_ascii=False, indent=2)
PY
chmod 600 "$OUT/summary.json"
if [ "${#FAILED[@]}" = 0 ]; then
  echo "ENTRY_PREFLIGHT_PASSED $(utc_now)"
  if grep -q ' skipped$' "$OUT/results.txt"; then echo "注意：有被显式跳过的检查项（未做检查，不是通过）：$(grep ' skipped$' "$OUT/results.txt" | cut -d' ' -f1 | tr '\n' ' ')"; fi
  exit 0
fi
echo "ENTRY_PREFLIGHT_FAILED $(utc_now) 未通过：${FAILED[*]}"
exit 1
