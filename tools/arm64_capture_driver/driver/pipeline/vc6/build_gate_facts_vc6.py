#!/usr/bin/env python3
"""VC-6 post-promotion 门禁 facts（codex-upgrade-external-gate-facts/v4）。
用法：build_gate_facts_vc6.py <evidence_root> <attempt_id> <production_tree> <acceptance.json> <promotion.json>
subject 的候选身份取自 acceptance.candidate_identity；production_tree_sha256 由受管工具复算。"""
import hashlib, json, os, sys
from pathlib import Path

sys.path.insert(0, os.environ.get("D") or "/root/docker/capture-cli/data")   # 数据根：优先取 vc6-env.sh 导出的 D
from tools.official_client_capture import codex_upgrade as cu

R, ATT, P, ACC, PROM = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5])
def sha(p: Path) -> str: return hashlib.sha256(p.read_bytes()).hexdigest()
acc = json.loads(ACC.read_bytes()); ci = acc["candidate_identity"]
prom = json.loads(PROM.read_bytes())
plan = json.loads((R / "gates/gate-plan.json").read_bytes())
tree_digest = cu._directory_tree_digest(P)
gates = []
for gate in plan["gates"]:
    gid = gate["gate_id"]
    meta = json.loads((R / "gates/logs" / f"{gid}.gate.json").read_bytes())
    assert meta["exit_code"] == 0, (gid, meta["exit_code"])
    gates.append({
        "gate_id": gid, "test_id": gate["test_id"], "command": list(gate["command"]),
        "working_directory": gate["working_directory"], "host": meta["host"], "architecture": meta["architecture"],
        "started_at_utc": meta["started_at_utc"], "completed_at_utc": meta["completed_at_utc"],
        "exit_code": 0, "status": "passed", "passed_count": 1, "failed_count": 0, "skipped_count": 0,
        "stdout_sha256": sha(R / "gates/logs" / f"{gid}.stdout.log"),
        "stderr_sha256": sha(R / "gates/logs" / f"{gid}.stderr.log"),
        "evidence": [{"path": f"gates/logs/{gid}.gate.json", "sha256": sha(R / "gates/logs" / f"{gid}.gate.json")}],
    })
gates.sort(key=lambda g: g["gate_id"])
facts = {
    "schema_version": "codex-upgrade-external-gate-facts/v4",
    "phase": "post_promotion",
    "attempt": {"attempt_id": ATT, "root_cause_id": None, "previous_receipt": None},
    "subject": {
        "campaign_id": acc["campaign_id"], "campaign_mode": acc["campaign_mode"], "campaign_purpose": acc["campaign_purpose"],
        "candidate_id": acc["candidate_id"], "candidate_purpose": acc["candidate_purpose"],
        "target_version": acc["target_version"], "target_architecture": "linux/arm64",
        "profile_id": acc["profile_id"], "profile_digest": acc["profile_digest"],
        "candidate_package_digest": acc["candidate_package_digest"],
        "candidate_source_tree_sha256": ci["source_tree_sha256"],
        "candidate_image_id": ci["image_id"], "candidate_image_reference": ci["image_reference"],
        "production_tree_sha256": tree_digest,
        "acceptance_sha256": sha(ACC), "promotion_receipt_sha256": sha(PROM),
    },
    "inputs": [
        {"role": "acceptance", "path": ACC.relative_to(R).as_posix(), "sha256": sha(ACC)},
        {"role": "promotion", "path": PROM.relative_to(R).as_posix(), "sha256": sha(PROM)},
    ],
    "environment": {
        "before": {"path": f"gates/environment/{ATT}-before.json", "sha256": sha(R / f"gates/environment/{ATT}-before.json")},
        "after": {"path": f"gates/environment/{ATT}-after.json", "sha256": sha(R / f"gates/environment/{ATT}-after.json")},
    },
    "gate_plan": {"path": "gates/gate-plan.json", "sha256": sha(R / "gates/gate-plan.json")},
    "gates": gates,
}
out = R / "post-promotion-gates.facts.json"
out.write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"); out.chmod(0o600)
print(json.dumps({"facts": str(out), "production_tree_sha256": tree_digest, "acceptance_sha256": facts["subject"]["acceptance_sha256"], "promotion_receipt_sha256": facts["subject"]["promotion_receipt_sha256"], "gates": [g["gate_id"] for g in gates]}, ensure_ascii=False))
