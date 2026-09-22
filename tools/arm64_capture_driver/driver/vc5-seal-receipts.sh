#!/bin/bash
# 候选 seal 第 2 步：在源码树外生成 observed-profile 与两份 Kilo 收据，并在 EvidenceManifest 生成之前完成权限收口。
# 用法：bash vc5-seal-receipts.sh <attempt_id> <client_checkpoint_at_utc>
# 幂等边界（2026-09-22 v14r4 教训）：attempt 的 evidence-manifest.json 已存在时进入只核对模式——不 mkdir、不 chmod、
# 不生成任何文件，只断言全部收据已存在；manifest 绑定的证据根从此不可触碰。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
ATT="$1"; CKPT="$2"
A="$NEWDIR/candidates/$CAND/attempts/$ATT"; EV="$A/evidence"
eval "$(python3 - "$A/attempt.json" <<'PY'
import json, sys, shlex
d = json.load(open(sys.argv[1])); i = d["identity"]
for k, v in {"CID": d["campaign_id"], "RUN_NONCE": d["run_nonce"], "STARTED": d["started_at_utc"], "IMAGE_ID": i["image_id"], "IMAGE_REF": i["image_reference"], "TREE": i["source_tree_sha256"], "BUILD_ID": i["build_id"], "DEPLOYED": i["deployed_version"], "PROFILE_ID": i["profile_id"], "PROFILE_DIGEST": i["profile_digest"], "SOURCE_ROOT": i["source_root"]}.items():
    print(f"{k}={shlex.quote(str(v))}")
PY
)"
echo "attempt=$ATT nonce=${RUN_NONCE:0:12} started=$STARTED ckpt=$CKPT"
RECEIPTS=("$EV/client/raw/profile-activation-fact.json" "$EV/client/generated/observed-profile-runtime-audit.json" "$EV/client/receipts/observed-profile-receipt.json" "$EV/client/generated/kilo/kilo-installation.json" "$EV/client/receipts/kilo-compatible-receipt.json" "$EV/client/receipts/kilo-responses-receipt.json")
if [ -e "$A/evidence-manifest.json" ] || [ -L "$A/evidence-manifest.json" ]; then
  echo "evidence-manifest.json 已存在：只核对收据齐全，不做任何写入或权限修改"
  for f in "${RECEIPTS[@]}"; do test -f "$f" || { echo "manifest 已存在但收据缺失：${f}（不可补写，需人工裁定）"; exit 3; }; done
  bash "$DRV/vc5-permission-closeout.sh" "$A"
  echo "SEAL_RECEIPTS_VERIFIED"; exit 0
fi
# ---- a. 目录（manifest 尚不存在，mkdir -p 幂等）----
mkdir -p "$EV/client/raw" "$EV/client/generated" "$EV/client/receipts"
# ---- b. observed-profile ----
FACT=$COMPOSE_DIR/data/$CAND-candidate-activation-fact.json
test -f "$FACT"; [ -f "$EV/client/raw/profile-activation-fact.json" ] || { cp "$FACT" "$EV/client/raw/profile-activation-fact.json"; chmod 600 "$EV/client/raw/profile-activation-fact.json"; }
ID_ARGS=(--campaign-id "$CID" --attempt-id "$ATT" --run-nonce "$RUN_NONCE" --candidate-id "$CAND" --target-version 0.154.0 --profile-id "$PROFILE_ID" --profile-digest "$PROFILE_DIGEST" --image-id "$IMAGE_ID" --image-reference "$IMAGE_REF" --source-tree-sha256 "$TREE" --build-id "$BUILD_ID" --deployed-version "$DEPLOYED")
[ -f "$EV/client/generated/observed-profile-runtime-audit.json" ] || python3 "$TOOLS/build_observed_profile_runtime_audit.py" --activation-fact "$EV/client/raw/profile-activation-fact.json" --output "$EV/client/generated/observed-profile-runtime-audit.json" "${ID_ARGS[@]}"
FIN_ID=(--campaign-id "$CID" --attempt-id "$ATT" --run-nonce "$RUN_NONCE" --attempt-started-at-utc "$STARTED" --client-checkpoint-at-utc "$CKPT" --candidate-id "$CAND" --target-version 0.154.0 --profile-id "$PROFILE_ID" --profile-digest "$PROFILE_DIGEST" --source-tree-sha256 "$TREE" --build-id "$BUILD_ID" --deployed-version "$DEPLOYED")
[ -f "$EV/client/receipts/observed-profile-receipt.json" ] || python3 "$TOOLS/codex_upgrade_receipt_finalizer.py" observed-profile --evidence-root "$EV" --output client/receipts/observed-profile-receipt.json "${FIN_ID[@]}" --image-id "$IMAGE_ID" --image-reference "$IMAGE_REF" --runtime-audit client/generated/observed-profile-runtime-audit.json | cut -c1-200
# ---- c. Kilo 收据 ----
test -f "$EV/client/raw/kilo-facts.json"
[ -d "$EV/client/generated/kilo" ] || python3 "$TOOLS/build_kilo_client_receipts.py" --facts "$EV/client/raw/kilo-facts.json" --output-dir "$EV/client/generated/kilo" | cut -c1-200
for c in kilo-compatible kilo-responses; do
  [ -f "$EV/client/receipts/$c-receipt.json" ] || python3 "$TOOLS/codex_upgrade_receipt_finalizer.py" kilo-binding --evidence-root "$EV" --output "client/receipts/$c-receipt.json" "${FIN_ID[@]}" --client-id "$c" --candidate-image-id "$IMAGE_ID" --model gpt-6-astra --installation client/generated/kilo/kilo-installation.json --ingress "client/generated/kilo/$c-ingress.json" --runtime-audit "client/generated/kilo/$c-runtime_audit.json" --response-witness "client/generated/kilo/$c-response.json" --usage-audit "client/generated/kilo/$c-usage.json" | cut -c1-200
done
# ---- d. 权限收口（EvidenceManifest 生成之前的最后一次写入；幂等，只改不合规条目）----
bash "$DRV/vc5-permission-closeout.sh" "$A"
ls -la "$EV/client/receipts"; echo "SEAL_RECEIPTS_DONE"
