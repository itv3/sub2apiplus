#!/bin/bash
# VC-6 §4.6.8 私有归档：打包本轮 Campaign（原始候选抓包、AcceptanceFact、promotion、post-promotion、activation、RemovalReceipt、
# 首次交付收据），在另一位置解包、逐文件复算摘要并在解包副本上重放关键收据，再生成 private_archive 统一收据。
set -Eeuo pipefail; umask 077
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
CID=$NEW
N=$D/evidence/campaigns/$CID
V6=$RUNROOT/vc6
R=$(cat "$V6/vc6-retire-root.txt"); AR=$(cat "$V6/vc6-root.txt")
test -s "$N/delivery/$CAND/receipt.json" || { echo "首次交付尚未完成"; exit 2; }
test ! -e "$N/control/vc/receipts/$CAND/private-archive.json" || { echo "private_archive 收据已存在，拒绝覆盖"; exit 2; }
ARCH_STAMP=$(date -u +%Y%m%dt%H%M%Sz)   # 不叫 STAMP，避免盖掉 env.sh 的 STAMP
ARCHDIR=${VC6_ARCHIVE_DIR:-/root/private-archives}; mkdir -p "$ARCHDIR"; chmod 700 "$ARCHDIR"
ARCH=$ARCHDIR/codex-${TARGET_VERSION}-$CID-$ARCH_STAMP.tar.gz
RESTORE=/root/restore-verify-$ARCH_STAMP; mkdir -p "$RESTORE"; chmod 700 "$RESTORE"
mkdir -p "$R/archive"; chmod 700 "$R/archive"
echo "=== 打包 $ARCH"
tar -czf "$ARCH" -C "$D/evidence/campaigns" "$CID"; chmod 600 "$ARCH"; ls -la "$ARCH"
echo "=== 解包到另一位置并逐文件复算"
tar -xzf "$ARCH" -C "$RESTORE"
python3 - "$N" "$RESTORE/$CID" "$ARCH" "$R" "$CID" <<'PY'
import hashlib, json, sys
from pathlib import Path
src, restored, arch, R, cid = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
def walk(root: Path):
    return [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p)}
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()]
source_rows, restored_rows = walk(src), walk(restored)
mismatch = [a["path"] for a, b in zip(source_rows, restored_rows) if a != b]
inventory = {"schema_version": "vc6-private-archive-inventory/v1", "campaign_id": cid, "archive_uri": str(arch),
             "archive_sha256": digest(arch), "archive_bytes": arch.stat().st_size, "restore_location": str(restored),
             "file_count": len(source_rows), "total_bytes": sum(r["bytes"] for r in source_rows), "source_root": str(src), "files": source_rows}
out = R / "archive" / "archive-inventory.json"
out.write_text(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); out.chmod(0o600)
summary = {"source_file_count": len(source_rows), "restored_file_count": len(restored_rows),
           "digest_mismatch_count": len(mismatch) + abs(len(source_rows) - len(restored_rows)),
           "inventory_sha256": digest(out), "archive_sha256": inventory["archive_sha256"], "archive_bytes": inventory["archive_bytes"],
           "total_bytes": inventory["total_bytes"]}
(R / "archive" / "restore-summary.partial.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print(json.dumps(summary, ensure_ascii=False))
assert summary["digest_mismatch_count"] == 0, "解包副本与源 Campaign 摘要不一致"
PY
echo "=== 在解包副本上重放关键收据"
RC=$RESTORE/$CID
python3 tools/official_client_capture/production_activation_receipt.py replay --evidence-root "$RC/control/$(basename "$AR")" --receipt activation-receipt.json > "$R/archive/replay-activation.json"
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py replay --evidence-root "$RC" --receipt "control/vc/receipts/$CAND/vc5-completion.json" > "$R/archive/replay-vc5-completion.json"
python3 - "$RC" "$R" "$(basename "$R")" <<'PY'
import json, hashlib, os, sys
from pathlib import Path
RV = os.environ["RETIRE_VERSION"]
RC, R, RETIRE = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
removal_path = RC / "control" / RETIRE / "removal-receipt.json"
removal = json.loads(removal_path.read_text())
checkpoint = json.loads(sorted((RC / "canonical" / "checkpoints").glob("*.json"))[-1].read_text())
items = {item["item_id"]: item for item in checkpoint["items"]}
facts = {
    "removal_receipt_fields_ok": (removal["schema_version"] == "codex-runtime-profile-removal/v1" and removal["status"] == "complete"
        and removal["removed_version"] == RV
        and removal["consumer_scan"] == {"catalog_references": 0, "selector_references": 0, "unknown_references": 0}
        and removal["runtime_catalog_removed"] is True and removal["historical_evidence_preserved"] is True),
    "canonical_execute_remaining": checkpoint["plan"]["execute_item_ids"],
    "canonical_vc6_items": sorted(k for k in items if k in {"production-activation", "rollback-verification", f"retire-{RV}"}),
    "removal_receipt_sha256_in_checkpoint": items.get(f"retire-{RV}", {}).get("source", {}).get("sha256"),
    "removal_receipt_sha256_recomputed": hashlib.sha256(removal_path.read_bytes()).hexdigest(),
}
assert facts["removal_receipt_fields_ok"] and facts["canonical_execute_remaining"] == [] and len(facts["canonical_vc6_items"]) == 3
assert facts["removal_receipt_sha256_in_checkpoint"] == facts["removal_receipt_sha256_recomputed"]
out = R / "archive" / "replay-removal-and-canonical.json"
out.write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); out.chmod(0o600)
print(json.dumps(facts, ensure_ascii=False))
PY
for f in "$R/archive"/replay-*.json; do echo "--- $(basename $f)"; head -c 260 "$f"; echo; done
chmod 600 "$R/archive"/*
echo "=== private_archive 统一收据"
python3 - "$N" "$R" "$CAND" "$CID" <<'PY'
import json, hashlib, sys
from pathlib import Path
N, R, CAND, CID = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
A = R / "archive"
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
inventory = json.loads((A / "archive-inventory.json").read_text()); partial = json.loads((A / "restore-summary.partial.json").read_text())
replay = {"schema_version": "vc6-private-archive-restore-replay/v1", "campaign_id": CID, "archive_uri": inventory["archive_uri"],
          "archive_sha256": inventory["archive_sha256"], "restore_location": inventory["restore_location"],
          "unpack": {k: partial[k] for k in ("source_file_count", "restored_file_count", "digest_mismatch_count")},
          "critical_receipts": {"production_activation": json.loads((A / "replay-activation.json").read_text()),
                                "vc5_completion": json.loads((A / "replay-vc5-completion.json").read_text()),
                                "removal_and_canonical": json.loads((A / "replay-removal-and-canonical.json").read_text())},
          "replayed_on": "解包副本（与源 Campaign 目录不同的路径），工具读的是副本内的相对路径；激活收据以副本内的激活证据根重放"}
out = A / "restore-replay.json"
out.write_text(json.dumps(replay, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); out.chmod(0o600)
subject = json.loads((N / "control/vc/receipts" / CAND / "vc5-completion.json").read_text())["subject"]
facts = {"schema_version": "codex-upgrade-vc-receipt-facts/v1", "kind": "private_archive", "subject": subject,
         "assertions": {"archive_uri": inventory["archive_uri"], "restore_location": inventory["restore_location"],
                        "inventory_sha256": sha(A / "archive-inventory.json"), "file_count": inventory["file_count"],
                        "total_bytes": inventory["total_bytes"], "unpack_verified": True, "digest_verified": True,
                        "critical_receipts_replayed": True},
         "evidence": [{"role": "archive_inventory", "path": (A / "archive-inventory.json").relative_to(N).as_posix()},
                      {"role": "restore_replay", "path": out.relative_to(N).as_posix()}]}
fp = N / "control/vc/receipts" / CAND / "private-archive.facts.json"
fp.write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); fp.chmod(0o600)
print("subject：", subject)
PY
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py finalize --evidence-root "$N" --facts "control/vc/receipts/$CAND/private-archive.facts.json" --output "control/vc/receipts/$CAND/private-archive.json" | cut -c1-300
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py replay --evidence-root "$N" --receipt "control/vc/receipts/$CAND/private-archive.json" | cut -c1-300
echo "$RESTORE" > "$V6/vc6-restore-dir.txt"
echo "PRIVATE_ARCHIVE_DONE archive=$ARCH restore=$RESTORE"
