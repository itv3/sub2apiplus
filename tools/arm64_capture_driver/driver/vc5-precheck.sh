#!/bin/bash
# VC-5 run 前只读预检（不创建 attempt、不发请求）。用法：bash vc5-precheck.sh <image_id> <build_id>
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
IMAGE_ID="$1"; BUILD_ID="$2"
export ADMIN_BEARER_TOKEN_FILE="$D/state/$UP/admin-token"
python3 - "$NEWDIR" "$CAND" "$B/source" "$IMAGE_ID" "$BUILD_ID" "$PROFILE_ID" "$PROFILE_DIGEST" <<'PY'
import argparse, json, sys, os
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
NEW, CAND, SRC, IMAGE_ID, BUILD_ID, PROFILE_ID, PROFILE_DIGEST = sys.argv[1:]
campaign_dir = Path(NEW); manifest = cu._require_formal_campaign(campaign_dir)
classification = cu._load_stage_result(campaign_dir, "classify"); print("classify:", classification.get("status"))
build_receipt, binding = cu._replay_candidate_build_receipt(campaign_dir, manifest, CAND, campaign_dir / "candidates" / CAND / "build-receipt.json"); print("build-receipt 重放通过:", build_receipt["receipt_digest"][:12])
cu._verify_execution_tree(Path(manifest["configuration"]["capture_root"])); print("执行副本树一致")
args = argparse.Namespace(campaign_dir=campaign_dir, candidate_id=CAND, runtime_image=f"{os.environ['CANDIDATE_IMAGE_REPOSITORY']}@{IMAGE_ID}", candidate_image_id=IMAGE_ID, candidate_source=Path(SRC), build_id=BUILD_ID, deployed_version=os.environ["TARGET_VERSION"], profile_id=PROFILE_ID, profile_digest=PROFILE_DIGEST, candidate_purpose="production_replacement")
identity = cu._candidate_identity_for_run(args, manifest, classification, verify_image=True); print("候选身份（含运行容器镜像校验）通过:", {k: identity[k][:16] if isinstance(identity[k], str) else identity[k] for k in ("image_id","source_tree_sha256","git_commit")})
identity = cu._bind_candidate_identity_to_build_receipt(args, identity, build_receipt, binding); print("身份已绑定 VC-4 收据")
jobs = cu._campaign_jobs(campaign_dir, manifest, "candidate", candidate_id=CAND, runtime_image=identity["image_reference"], profile_id=identity["profile_id"], profile_digest=identity["profile_digest"], build_id=identity["build_id"], deployed_version=identity["deployed_version"], candidate_image_id=identity["image_id"], source_tree_sha256=identity["source_tree_sha256"], candidate_purpose=identity["candidate_purpose"])
print("候选 Job:", [j.job_id for j in jobs])
cu._validate_candidate_admin_credential(jobs); print("管理凭据通过")
tool = cu._tool_identity(include_git=False); impact = cu._cheap_capture_tool_impact(manifest, list(jobs), tool); print("工具影响:", {k: impact.get(k) for k in ("kind","changed_components","affected_job_ids")})
PY
echo "PRECHECK_DONE"
