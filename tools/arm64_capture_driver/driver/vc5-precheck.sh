#!/bin/bash
# VC-5 统一准入；dry-run 在公共前导前执行，保证不创建目录、不续签、不发请求。
# 用法：--dry-run [--skip-db-prerequisites] | --apply --approval <批准件> | --consume | --renew --approver <批准人及依据>；
#   切换后镜像核对保留 <image_id> <build_id>。
#   --dry-run：只读。除准入各检查项外还输出（1）管理令牌与批准件的剩余有效期（ttl；令牌富余不足 2 小时会警告）、（2）采集账号 model_mapping 须空、
#   采集分组 allow_live 须开、分组内只许采集 Key 一把活跃三项库表前置（candidate_prerequisites；不满足时顶层 status=blocked，每项写明为什么要求、
#   怎么临时改、VC-6 之后怎么恢复）。人读摘要写标准错误，标准输出仍是 JSON。--skip-db-prerequisites 只看准入与有效期、不查库。
#   --renew：只续管理令牌（必须带 --approver）：准入已有收据、绑定未变、待办动作恰是 renew_token 才执行，其余一律拒绝；
#   vc5-all.sh 在 canonical 前设了 VC5_RENEW_APPROVER 时自动调用它。
set -Eeuo pipefail
case "${1:-}" in
  --dry-run|--apply|--consume|--renew) exec python3 -B "$(dirname "${BASH_SOURCE[0]}")/vc5_admission.py" "$@" ;;
esac
# 准入三种模式两客户端都已接入（B5b-01）；下面的镜像预检属于 Codex 的 VC-5 启动（vc5-start.sh），Claude 的预检在
# vc5-claude-start.sh（只读调候选 run 的正式入口门），lib.sh 在任何副作用之前拒绝别的客户端（B5b-01b）。
CLIENT_STEP_REQUIRED=(CT_VC5_START_SCRIPT vc5-start.sh)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
IMAGE_ID="$1"; BUILD_ID="$2"
vc5_require_admission
python3 - "$NEWDIR" "$CAND" "$B/source" "$IMAGE_ID" "$BUILD_ID" "$PROFILE_ID" "$PROFILE_DIGEST" "$CT_ORCHESTRATOR_MODULE" <<'PY'
import argparse, importlib, json, sys, os
from pathlib import Path
# 编排器模块取客户端表（Claude 升级 B5a-06；Codex 即 codex_upgrade，与改造前相同）。
NEW, CAND, SRC, IMAGE_ID, BUILD_ID, PROFILE_ID, PROFILE_DIGEST, ORCHESTRATOR = sys.argv[1:]
cu = importlib.import_module(ORCHESTRATOR)
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
tool = cu._tool_identity(include_git=False); impact = cu._cheap_capture_tool_impact(manifest, list(jobs), tool, campaign_dir=campaign_dir); print("工具影响（相对有效身份）:", {k: impact.get(k) for k in ("kind","changed_components","affected_job_ids")})
PY
echo "PRECHECK_DONE"
