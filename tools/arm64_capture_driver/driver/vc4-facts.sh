#!/bin/bash
# 生成 VC-4 实现测试 facts 并 finalize/replay 收据：bash vc4-facts.sh <evidence_root> <tree_sha256>
# gates：四个公共门禁 + affected-spec-ep-019（kind=affected，与 VC-3 需求的 affected 闭集一致）+ 本机 check-egress-spec。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
E="$1"; TREE="$2"
python3 - "$E" "$C" "$TREE" "$UP" "$NEW" "$CAND" <<'PY'
import json, sys, re, pathlib
E, C, TREE, UPGRADE_ID, CAMPAIGN_ID, CANDIDATE_ID = sys.argv[1:]
root = pathlib.Path(E)
impl = (root / "logs/implementation.log").read_text(encoding="utf-8")
spec = (root / "logs/check-egress-spec.log").read_text(encoding="utf-8")
def exit_code(text, marker):
    seg = text.split(marker, 1)[1]
    m = re.search(r"^exit_code=(\d+)$", seg, re.M)
    return int(m.group(1))
gates = []
for gate_id, kind, wd, cmd, marker in [
    ("affected-spec-ep-019", "affected", "backend", ["go","test","./internal/service","-run","^TestCodexWhamUsageLunaReserveReplaysApprovedSemanticsOnTargetRelease$","-count=1"], "## gate affected-spec-ep-019"),
    ("catalog-projection", "public", "backend", ["go","test","./internal/service","-run","^TestOfficialCodexProjectionUsesFormalReleaseCatalog$","-count=1"], "## gate catalog-projection"),
    ("official-egress-version-leak-ast", "public", "backend", ["go","test","./internal/service","-run","^TestOfficialEgressVersionLeakAST$","-count=1"], "## gate official-egress-version-leak-ast"),
    ("version-leak", "public", ".", ["python3","tools/check_version_leak.py"], "## gate version-leak (working_directory=.)"),
    ("version-leak-self-test", "public", ".", ["python3","tools/check_version_leak.py","--self-test"], "## gate version-leak-self-test"),
]:
    rc = exit_code(impl, marker)
    gates.append({"gate_id": gate_id, "kind": kind, "command": cmd, "exit_code": rc, "passed": 1 if rc == 0 else 0, "failed": 0 if rc == 0 else 1, "approved_skip": 0, "unexpected_skip": 0})
rc_spec = exit_code(spec, "## make check-egress-spec")
gates.append({"gate_id": "check-egress-spec", "kind": "public", "command": ["make","check-egress-spec"], "exit_code": rc_spec, "passed": 1 if rc_spec == 0 else 0, "failed": 0 if rc_spec == 0 else 1, "approved_skip": 0, "unexpected_skip": 0})
gates.sort(key=lambda g: g["gate_id"])
facts = {
  "schema_version": "codex-upgrade-vc-receipt-facts/v1",
  "kind": "implementation_tests",
  "subject": {
    "upgrade_id": UPGRADE_ID,
    "campaign_id": CAMPAIGN_ID,
    "campaign_purpose": "production_replacement",
    "baseline_version": "0.151.0",
    "target_version": "0.154.0",
    "candidate_id": CANDIDATE_ID,
    "attempt_id": None,
  },
  "assertions": {"git_commit": C, "source_tree_sha256": TREE, "target_architecture": "linux/arm64", "gates": gates},
  "evidence": [
    {"role": "check_egress_spec", "path": "logs/check-egress-spec.log"},
    {"role": "implementation_tests", "path": "logs/implementation.log"},
  ],
}
(root / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("gates:", [(g["gate_id"], g["kind"], g["exit_code"]) for g in gates])
PY
chmod 600 "$E/facts.json"
python3 -m tools.official_client_capture.codex_upgrade_vc_receipt finalize --evidence-root "$E" --facts facts.json --output receipt.json
python3 -m tools.official_client_capture.codex_upgrade_vc_receipt replay --evidence-root "$E" --receipt receipt.json | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('replay:', d.get('kind'), d.get('status'), d.get('receipt_digest'))"
