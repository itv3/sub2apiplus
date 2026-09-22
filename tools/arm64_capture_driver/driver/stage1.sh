#!/bin/bash
# 前阶段 1：派发前检查（pre-plan）→ 零请求 smoke → 新账本（总预算按项目总账绝对截止设上限，留 5 分钟余量）
#   → ARM64 环境收据（p0）→ 账本 checkpoint → preflight plan → Job 演练收据 → atomic-double 收据。
# 全部参数来自 $ARM64_VC_ENV；产物坐标写入 $RUNROOT/stage1.env 供 stage2 使用。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
echo "ROUND=$ROUND STAMP=$STAMP"
bash "$DRV/guard.sh" pre-plan
DEPLOY=$(python3 -c "import glob,os; print(max(glob.glob('$D/control/codex-0154-supervisor-enable-*.json'), key=os.path.getmtime))")
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
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger create --ledger-dir "$L" --upgrade-id "$UP" --baseline-version 0.151.0 --target-version 0.154.0 --campaign-purpose production_replacement --evidence-decision reuse --project-ledger-dir "$D/evidence/campaigns/upgrade-project-ledger" --total-budget-minutes "$TOTAL" "${BUDGET_ARGS[@]}" | cut -c1-160
python3 -c "import json; p=json.load(open(\"$L/ledger.json\")); print(\"budgets:\", p[\"total_budget_minutes\"], p[\"stage_budgets_minutes\"]); print(\"binding:\", p.get(\"project_ledger_binding\",{}).get(\"absolute_deadline_utc\"))"
ENV="$D/environment/$UP-p0"
mkdir -m 0700 "$ENV"
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt collect --evidence-root "$ENV" --output facts.json --phase p0 --subject-id "$UP" | cut -c1-120
python3 -m tools.official_client_capture.codex_upgrade_arm64_environment_receipt finalize --evidence-root "$ENV" --facts facts.json --output receipt.json | cut -c1-120
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger checkpoint --ledger-dir "$L" --output "receipts/vc0-input-$ROUND-preflight-$STAMP.json" | cut -c1-120
HEADSHA=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[\"tool_files_sha256\"][:9])" "$DEPLOY")
PRECID="c0154-preflight-vc5-$ROUND-$HEADSHA-$STAMP"
PRE="$D/evidence/campaigns/$PRECID"
OFF154=$D/official/codex-0.154.0-20260911T170500Z; OFF151=$D/official/codex-0.151.0-20260830T130555Z
python3 -m tools.official_client_capture.codex_upgrade plan --campaign-dir "$PRE" --campaign-id "$PRECID" --baseline-version 0.151.0 --target-version 0.154.0 --campaign-mode preflight_only --campaign-purpose production_replacement --timing-ledger-dir "$L" --timing-receipt "receipts/vc0-input-$ROUND-preflight-$STAMP.json" --arm64-environment-root "$ENV" --arm64-environment-receipt receipt.json --baseline-source $OFF151/source-rust-v0.151.0/codex-rs --target-source $OFF154/source-rust-v0.154.0/codex-rs --baseline-evidence $D/promotions/c0151-formal-rule-correction-20260905t0033z-c0151-v10-c1-production/catalogdata/runtime/profiles/0.151.0/dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39.json --target-sha256 9b7c1c7abdc26fc3c4f47c77656a8e9121def5483dbae830ef1ee561758448a9 --target-package $OFF154/assets/codex-package-aarch64-unknown-linux-musl.tar.gz --target-package-sha256 97d93e11df72d3c26772db019e6ea8bb72c246500d46b98c760839f3240355e6 --target-code-mode-host-sha256 f31e1c5ffbbca7884aff2f0f8795d3da197f4aafb114033a399dfc17a5119031 --runtime-image oauth-egress-capture-capture-cli@sha256:4a7de52bce3e934c53fabbff35c10f503aff32bb9ccaec6cb2d3c4abfa47c41c --rule-manifest $TOOLS/codex_upgrade_rules_0_151_0.json --scenario-manifest $TOOLS/codex_upgrade_scenarios_0_151_0.json --target-scenario-manifest $TOOLS/codex_upgrade_scenarios_0_154_0.json --suite full --model gpt-5.5 --lite-model gpt-6-astra --codex-account-id 22 --api-key-id 4 --live-attestation-compose-dir "$COMPOSE_DIR" --live-attestation-compose-files "$COMPOSE_DIR/docker-compose.yml" 2>&1 | tail -1 | cut -c1-160
JR="$D/control/c0154-job-rehearsal-vc5-$ROUND-$STAMP"
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
