#!/bin/bash
# 前阶段 1：派发前检查（pre-plan）→ 零请求 smoke → 新账本（总预算按项目总账绝对截止设上限，留 5 分钟余量）
#   → ARM64 环境收据（p0）→ 账本 checkpoint → preflight plan → Job 演练收据 → atomic-double 收据。
# 全部参数来自 $ARM64_VC_ENV；产物坐标写入 $RUNROOT/stage1.env 供 stage2 使用。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
echo "ROUND=$ROUND STAMP=$STAMP"
bash "$DRV/guard.sh" pre-plan
DEPLOY=$(python3 -B - "$DRV/../install.py" "$D" <<'PYDEPLOY'
import importlib.util, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('installed_driver', sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
print(module.latest_deploy_receipt(Path(sys.argv[2])/'control')[0])
PYDEPLOY
)
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print({k:d.get(k) for k in (\"status\",\"policy_version\",\"tool_files_sha256\",\"control_sha256\",\"wire_producer_sha256\",\"evidence_semantics_sha256\")})" "$DEPLOY"
python3 -m tools.official_client_capture.codex_upgrade_zero_request_smoke --staging-root "$D/staging/zero-request-smoke-$STAMP" --output "$D/audit/zero-request-smoke-$STAMP.json" | cut -c1-200
# 总预算按项目总账绝对截止设上限（v14 教训：360 分钟到期停线；叫停也计时），留 5 分钟余量
TOTAL=$(python3 -c "
import sys
from datetime import datetime, timezone
now = datetime.now(timezone.utc); dl = datetime.strptime(sys.argv[1], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
print(int((dl - now).total_seconds() // 60) - 5)" "$PROJECT_DEADLINE_UTC")
echo "TOTAL_BUDGET_MINUTES=$TOTAL"
BUDGET_ARGS=(); for kv in $STAGE_BUDGETS; do BUDGET_ARGS+=(--stage-budget-minutes "$kv"); done
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger create --ledger-dir "$L" --upgrade-id "$UP" --baseline-version "$BASELINE_VERSION" --target-version "$TARGET_VERSION" --campaign-purpose production_replacement --evidence-decision "$EVIDENCE_DECISION" --project-ledger-dir "$D/evidence/campaigns/upgrade-project-ledger" --total-budget-minutes "$TOTAL" "${BUDGET_ARGS[@]}" | cut -c1-160
python3 -c "import json; p=json.load(open(\"$L/ledger.json\")); print(\"budgets:\", p[\"total_budget_minutes\"], p[\"stage_budgets_minutes\"]); print(\"binding:\", p.get(\"project_ledger_binding\",{}).get(\"absolute_deadline_utc\"))"
ENV="$D/environment/$UP-p0"
mkdir -m 0700 "$ENV"
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$ENV" --output facts.json --phase p0 --subject-id "$UP" --rust-tls-codex-version "$TARGET_VERSION" | cut -c1-120
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$ENV" --facts facts.json --output receipt.json | cut -c1-120
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger checkpoint --ledger-dir "$L" --output "receipts/vc0-input-$ROUND-preflight-$STAMP.json" | cut -c1-120
HEADSHA=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[\"tool_files_sha256\"][:9])" "$DEPLOY")
PRECID="${CAMPAIGN_PREFIX}-preflight-vc5-$ROUND-$HEADSHA-$STAMP"
PRE="$D/evidence/campaigns/$PRECID"
: "${TARGET_CODE_MODE_HOST_SHA256:?需填写目标 code-mode-host 的审核摘要}"
: "${CAPTURE_RUNTIME_IMAGE:?需填写固定 digest 的采集镜像}"
python3 -m tools.official_client_capture.codex_upgrade plan \
  --campaign-dir "$PRE" --campaign-id "$PRECID" --baseline-version "$BASELINE_VERSION" --target-version "$TARGET_VERSION" \
  --campaign-mode preflight_only --campaign-purpose production_replacement --timing-ledger-dir "$L" \
  --timing-receipt "receipts/vc0-input-$ROUND-preflight-$STAMP.json" --arm64-environment-root "$ENV" --arm64-environment-receipt receipt.json \
  --baseline-source "$BASELINE_SOURCE" --target-source "$TARGET_SOURCE" --baseline-evidence "$ACTIVE_PROFILE" \
  --target-sha256 "$CODEX_BIN_SHA256" --target-package "$TARGET_PACKAGE" --target-package-sha256 "$OFFICIAL_ASSET_SHA256" \
  --target-code-mode-host-sha256 "$TARGET_CODE_MODE_HOST_SHA256" --runtime-image "$CAPTURE_RUNTIME_IMAGE" \
  --rule-manifest "$BASELINE_RULES_JSON" --scenario-manifest "$BASELINE_SCENARIOS_JSON" --target-scenario-manifest "$SCENARIOS_JSON" \
  --capture-codex-bin "$CODEX_BIN" --relay-codex-bin "$CODEX_BIN" --suite full --model "$MAIN_MODEL" --lite-model "$LITE_MODEL" \
  --codex-account-id "$CODEX_ACCOUNT_ID" --api-key-id "$API_KEY_ID" --live-attestation-compose-dir "$COMPOSE_DIR" \
  --live-attestation-compose-files "$COMPOSE_DIR/docker-compose.yml" 2>&1 | tail -1 | cut -c1-160
JR="$D/control/${CAMPAIGN_PREFIX}-job-rehearsal-vc5-$ROUND-$STAMP"
mkdir -m 0700 "$JR"
python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt collect --campaign-dir "$PRE" --evidence-root "$JR" --output facts.json | cut -c1-160
python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt finalize --evidence-root "$JR" --facts facts.json --output receipt.json | cut -c1-160
AT="codex-atomic-vc0-vc1-$ROUND-$STAMP"
mkdir -m 0700 "$D/staging/$AT"
docker exec --env PYTHONPATH=/capture --workdir /capture capture-cli python3 -m tools.official_client_capture.codex_upgrade_campaign_run_rehearsal_receipt atomic-double-collect --evidence-root "/capture/staging/$AT" --output receipt.json | cut -c1-160
cat > "$RUNROOT/stage1.env" <<ENV
DEPLOY=$DEPLOY
ENV=$ENV
PRECID=$PRECID
PRE=$PRE
JR=$JR
AT=$AT
ENV
chmod 600 "$RUNROOT/stage1.env"
echo "STAGE1_DONE $RUNROOT/stage1.env"
