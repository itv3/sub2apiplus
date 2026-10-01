#!/bin/bash
# 入口便宜检查（E1-02）：排在 pre-A3 认证（约 1 小时）与建计时账本之前，把几秒到一两分钟就能查出的错误一次查全。
#   各项互相独立，一项失败其余照查，最后汇总；全部通过退出 0，任一失败退出 1。
#   零请求：不建账本、不建 Campaign、不签收据；环境探针只在临时目录试采，结束即删除，不落正式收据。
# 检查项：
#   required-parameters  后续阶段要用、参数文件里可缺省的身份参数已填写（不是空值或 REPLACE_ 占位）；
#   guard-pre-plan       驱动安装复验、指定出口实时准入、磁盘余量（与 stage1 建账本前同一守卫）；
#   deployment-identity  最新受监督部署收据的策略与五摘要等于当前受管工具树；
#   target-client        目标客户端两处安装：宿主机与采集容器内同一路径，摘要与版本等于本轮登记值；
#   official-package     官方包摘要等于登记值，基线与目标源码树就位；
#   plan-audit           plan --audit-only：参数、官方包、源码、规则与场景清单、作业 covers、覆盖计划、基线证据、执行副本；
#   environment-probe    ARM64 环境收据在临时目录试采并封存（采集器与出口、磁盘、TLS 探针当下可用）。
# 用法：ARM64_VC_ENV=$RUNROOT/env.sh bash entry-preflight.sh（由 pre-a3.sh 与 stage1.sh 开头调用，也可单独运行）
set -Euo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
OUT="$RUNROOT/entry-preflight/$(date -u +%Y%m%dt%H%M%Sz)"
install -d -m 700 "$RUNROOT/entry-preflight" "$OUT"
FAILED=()
check() { # <名称> <函数>：各项互不依赖，一项失败不中断其余
  local name=$1 rc=0 start
  start=$(date +%s)
  echo "== ${name} $(utc_now)"
  "$2" > "$OUT/$name.log" 2>&1 || rc=$?
  tail -n 6 "$OUT/$name.log" | cut -c1-300
  if [ "$rc" = 0 ]; then
    echo "通过 ${name}（$(( $(date +%s) - start )) 秒）"
  else
    echo "未通过 ${name}（rc=${rc}，$(( $(date +%s) - start )) 秒，日志 $OUT/$name.log）"
    FAILED+=("$name")
  fi
}

required_parameters() {
  local key missing=()
  for key in TARGET_CODE_MODE_HOST_SHA256 CAPTURE_RUNTIME_IMAGE CODEX_BIN CODEX_BIN_SHA256 OFFICIAL_ASSET_SHA256 \
             TARGET_PACKAGE TARGET_SOURCE BASELINE_SOURCE ACTIVE_PROFILE MAIN_MODEL LITE_MODEL CODEX_ACCOUNT_ID API_KEY_ID \
             COMPOSE_DIR PROJECT_DEADLINE_UTC STAGE_BUDGETS POLICY_COMPAT_RECEIPT POLICY_ACTIVATION PRE_A3_CERTIFICATION; do
    if [ -z "${!key:-}" ] || [[ ${!key} == REPLACE_* ]] || [[ ${!key} == */REPLACE_* ]]; then missing+=("$key"); fi
  done
  if [ "${#missing[@]}" != 0 ]; then echo "参数文件缺少或仍是占位：${missing[*]}"; return 1; fi
  echo "后续阶段要用的身份参数均已填写"
}

guard_pre_plan() { bash "$DRV/guard.sh" pre-plan; }

latest_deploy() {
  python3 -B - "$DRV/../install.py" "$D" <<'PY'
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("installed_driver", sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
print(module.latest_deploy_receipt(Path(sys.argv[2]) / "control")[0])
PY
}

deployment_identity() {
  local deploy
  deploy=$(latest_deploy) || return 1
  python3 -B - "$deploy" <<'PY'
import sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade_policy_certification as certification
identity = certification.current_identity()
receipt = certification.load_deployment_receipt(Path(sys.argv[1]), expected_identity=identity)
print("最新部署收据与当前受管工具树一致：", sys.argv[1], receipt.get("created_at_utc"), identity["tool_files_sha256"][:12])
PY
}

target_client() {
  local host container version
  host=$(sha256sum "$CODEX_BIN" | cut -c1-64) || return 1
  container=$(docker exec capture-cli sha256sum "$CODEX_BIN" | cut -c1-64) || return 1
  version=$("$CODEX_BIN" --version 2>&1 | head -n 1) || return 1
  echo "宿主机 ${host}；采集容器 ${container}；登记 ${CODEX_BIN_SHA256}；版本 ${version}"
  [ "$host" = "$CODEX_BIN_SHA256" ] && [ "$container" = "$CODEX_BIN_SHA256" ] && [[ $version == *"$TARGET_VERSION"* ]]
}

official_package() {
  local actual
  actual=$(sha256sum "$TARGET_PACKAGE" | cut -c1-64) || return 1
  echo "官方包 ${actual}；登记 ${OFFICIAL_ASSET_SHA256}"
  [ "$actual" = "$OFFICIAL_ASSET_SHA256" ] || return 1
  test -d "$BASELINE_SOURCE" && test ! -L "$BASELINE_SOURCE" && test -d "$TARGET_SOURCE" && test ! -L "$TARGET_SOURCE" && echo "基线与目标源码树就位"
}

plan_audit() {
  # 拟定路径只作作业坐标与输出路径校验，不会被创建；审计不读账本与环境收据（建账本之后才有）。
  python3 -m tools.official_client_capture.codex_upgrade plan --audit-only \
    --campaign-dir "$D/staging/entry-preflight-$(date -u +%Y%m%dt%H%M%Sz)/campaign" --campaign-id "${CAMPAIGN_PREFIX}-plan-audit" \
    --baseline-version "$BASELINE_VERSION" --target-version "$TARGET_VERSION" \
    --campaign-mode preflight_only --campaign-purpose production_replacement \
    --baseline-source "$BASELINE_SOURCE" --target-source "$TARGET_SOURCE" --baseline-evidence "$ACTIVE_PROFILE" \
    --target-sha256 "$CODEX_BIN_SHA256" --target-package "$TARGET_PACKAGE" --target-package-sha256 "$OFFICIAL_ASSET_SHA256" \
    --target-code-mode-host-sha256 "$TARGET_CODE_MODE_HOST_SHA256" --runtime-image "$CAPTURE_RUNTIME_IMAGE" \
    --rule-manifest "$BASELINE_RULES_JSON" --scenario-manifest "$BASELINE_SCENARIOS_JSON" --target-scenario-manifest "$SCENARIOS_JSON" \
    --capture-codex-bin "$CODEX_BIN" --relay-codex-bin "$CODEX_BIN" --suite full --model "$MAIN_MODEL" --lite-model "$LITE_MODEL" \
    --codex-account-id "$CODEX_ACCOUNT_ID" --api-key-id "$API_KEY_ID" --live-attestation-compose-dir "$COMPOSE_DIR" \
    --live-attestation-compose-files "$COMPOSE_DIR/docker-compose.yml" > "$OUT/plan-audit.json"
  local rc=$?
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
  python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$probe" --output facts.json --phase p0 --subject-id "$UP-entry-preflight" --rust-tls-codex-version "$TARGET_VERSION" | cut -c1-160 || rc=$?
  if [ "$rc" = 0 ]; then
    python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$probe" --facts facts.json --output receipt.json | cut -c1-160 || rc=$?
  fi
  rm -rf "$probe"
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
if [ "${#FAILED[@]}" = 0 ]; then
  echo "ENTRY_PREFLIGHT_PASSED $(utc_now)"
  exit 0
fi
echo "ENTRY_PREFLIGHT_FAILED $(utc_now) 未通过：${FAILED[*]}"
exit 1
