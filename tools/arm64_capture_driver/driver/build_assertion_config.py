#!/usr/bin/env python3
"""从 seal / compare / classify 结果生成逐规则断言配置（在 ARM64 数据根下以 PYTHONPATH=. 运行）。
用法：build_assertion_config.py <campaign_dir> <candidate_id> <official_campaign_dir> <输出 config.json>
official_campaign_dir 是官方证据所在（reuse 导入的原始）Campaign 目录。"""
import json, sys, pathlib
sys.dont_write_bytecode = True
from driver_config import load_config
from phase_context import resolve
CONFIG = load_config()
CONTEXT = resolve(CONFIG, mode="attempt")
from tools.official_client_capture import codex_upgrade as cu
campaign_dir, cand, official_dir, out = sys.argv[1:]
C = pathlib.Path(campaign_dir); O = pathlib.Path(official_dir)
if (str(C), cand) != (CONFIG["NEWDIR"], CONFIG["CAND"]):
    raise SystemExit("断言配置的 Campaign 或 Candidate 与本轮不一致")
if pathlib.Path(out) != pathlib.Path(CONTEXT["parameters"]["ASSERTION_CONFIG_DIR"]) / "config.json":
    raise SystemExit("断言配置必须写入当前评估基线的控制目录")
if not CONTEXT["parameters"]["ASSERTIONS_WRITE"]:
    raise SystemExit("当前断言来源只读，禁止写入新配置")
classification = cu._load_stage_result(C, "classify")
# 第三批 R5：按当前评估基线生效的批准画像投影（approval-revision 基线下取修订画像，其余批准包身份不变）。
classification = cu._effective_classification_view(C, cand, classification)
candidate = cu._load_stage_result(C, "capture-candidate", cand)
comparison = cu._load_stage_result(C, "compare", cand)
official = cu._load_stage_result(O, "capture-official")
approved = C / "classification" / "approved"
_tr = json.load(open(approved / "target-rules.json")); rules = _tr.get("rules") or _tr.get("required_rules")
rule_ids = [row["rule"] if isinstance(row, dict) and "rule" in row else (row["id"] if isinstance(row, dict) else row) for row in rules]
profile_id, profile_digest = cu._profile_binding_from_manifest(C, classification)
ctx_o = official["assertion_context"]; ctx_c = candidate["assertion_context"]
config = {
    "campaign_dir": str(C), "candidate_id": cand,
    "assertion_profile": str(C / classification["assertion_profile_manifest"]["path"]), "rule_manifest": str(approved / "target-rules.json"),
    "expected_profile_sha256": classification["assertion_profile_manifest"]["sha256"],
    "official_evidence_root": ctx_o["evidence_root"], "candidate_evidence_root": ctx_c["evidence_root"],
    "official_capture_manifest": ctx_o["capture_manifest_path"], "candidate_capture_manifest": ctx_c["capture_manifest_path"],
    "official_evidence_prefix": ctx_o["evidence_prefix"], "candidate_evidence_prefix": ctx_c["evidence_prefix"],
    "target_version": cu.load_campaign_manifest(C)["target_version"], "rules": rule_ids, "profile_id": profile_id, "profile_digest": profile_digest,
    "official_package_digest": comparison["official_package_digest"], "candidate_package_digest": comparison["candidate_package_digest"],
    "comparison_package_digest": comparison["package_digest"],
    "official_authority": {"assertion_profile_sha256": classification["assertion_profile_manifest"]["sha256"], "classification_package_digest": classification["package_digest"], "review_sha256": classification["joint_manifest_sha256"]},
}
pathlib.Path(out).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({k: (v if not isinstance(v, list) else f"<{len(v)} 条>") for k, v in config.items()}, ensure_ascii=False, indent=1)[:2000])
