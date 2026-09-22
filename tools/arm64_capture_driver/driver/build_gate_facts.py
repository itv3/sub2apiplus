#!/usr/bin/env python3
"""组装候选外部门禁 facts（codex-upgrade-external-gate-facts/v4）：evidence 指向 gate.json、不带 test_id。
用法：build_gate_facts.py <gate_root> <attempt_id> <campaign.json> <candidates/<cid>/result.json> <build-receipt.json> <本地门禁目录> <目标平台门禁记录目录>
本地门禁目录含 check-egress-spec.{gate.json,stdout.log,stderr.log} 与 full-regression.*；目标平台目录含 target-platform.*。
输出 <gate_root>/candidate-gates.facts.json，并把日志按 <gate_root>/logs/<gate>.stdout.log 登记为 evidence。"""
import json, sys, pathlib, hashlib, shutil
root, attempt, campaign_path, seal_path, build_path, local_dir, target_dir = [pathlib.Path(p) if i not in (1,) else p for i, p in enumerate(sys.argv[1:])]
root = pathlib.Path(sys.argv[1]); attempt = sys.argv[2]
campaign = json.load(open(sys.argv[3])); seal = json.load(open(sys.argv[4])); build = json.load(open(sys.argv[5]))
local_dir = pathlib.Path(sys.argv[6]); target_dir = pathlib.Path(sys.argv[7])
def sha(p): return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
logs = root / "logs"; logs.mkdir(parents=True, exist_ok=True)
gates = []
for gate_id, src in (("check-egress-spec", local_dir), ("full-regression", local_dir), ("target-platform", target_dir)):
    meta = json.load(open(src / f"{gate_id}.gate.json"))
    # 收据合同（codex_upgrade_gate_receipt）：evidence 必须是 JSON 文件（这里用 gate.json），
    # stdout/stderr 只以 *_sha256 绑定；candidate_external 阶段的 gate 不得带 test_id。
    digests = {}
    for kind in ("stdout", "stderr"):
        dst = logs / f"{gate_id}.{kind}.log"
        if not dst.exists():
            shutil.copyfile(src / f"{gate_id}.{kind}.log", dst); dst.chmod(0o600)
        digests[kind] = sha(dst)
    gate_copy = logs / f"{gate_id}.gate.json"
    if not gate_copy.exists():
        shutil.copyfile(src / f"{gate_id}.gate.json", gate_copy); gate_copy.chmod(0o600)
    evidence = [{"path": f"logs/{gate_id}.gate.json", "sha256": sha(gate_copy)}]
    assert meta["exit_code"] == 0, f"{gate_id} 退出码 {meta['exit_code']}"
    gates.append({
        "gate_id": gate_id, "command": meta["command"], "working_directory": meta["working_directory"],
        "host": meta["host"], "architecture": meta["architecture"], "started_at_utc": meta["started_at_utc"], "completed_at_utc": meta["completed_at_utc"],
        "exit_code": 0, "status": "passed", "passed_count": 1, "failed_count": 0, "skipped_count": 0,
        "stdout_sha256": digests["stdout"], "stderr_sha256": digests["stderr"], "evidence": evidence,
    })
gates.sort(key=lambda g: g["gate_id"])
identity = seal["identity"]
subject = {
    "campaign_id": campaign["campaign_id"], "campaign_mode": campaign["campaign_mode"], "campaign_purpose": campaign["campaign_purpose"],
    "candidate_id": seal["candidate_id"], "candidate_purpose": seal["candidate_purpose"], "target_version": campaign["target_version"],
    "target_architecture": build["target_architecture"], "profile_id": identity["profile_id"], "profile_digest": identity["profile_digest"],
    "candidate_package_digest": seal["package_digest"], "candidate_source_tree_sha256": identity["source_tree_sha256"],
    "candidate_image_id": identity["image_id"], "candidate_image_reference": identity["image_reference"],
    "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None,
}
env = {}
for role in ("before", "after"):
    p = root / "environment" / f"{attempt}-{role}.json"
    env[role] = {"path": f"environment/{attempt}-{role}.json", "sha256": sha(p)}
facts = {"schema_version": "codex-upgrade-external-gate-facts/v4", "phase": "candidate_external", "attempt": {"attempt_id": attempt, "root_cause_id": None, "previous_receipt": None}, "subject": subject, "inputs": [], "gate_plan": None, "environment": env, "gates": gates}
out = root / "candidate-gates.facts.json"; out.write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); out.chmod(0o600)
print("facts ->", out, "| gates:", [(g["gate_id"], g["host"], g["architecture"]) for g in gates])
