#!/usr/bin/env python3
"""VC-6 退休步骤的 Catalog 裁剪（退休版本由参数给出；算法已被历次升级的先例逐字节验证过）。
用法：retire-apply.py <officialegress 目录> <campaign_id> <退休版本> [--dry-run]
动作：读取 selector 指向的 active 快照目录 → 去掉退休版本的条目 → 紧凑 JSON＋尾部换行落成内容寻址的新文件（旧文件原地保留）
      → selector 指向新快照目录，source 记为 campaign:<id>/retirement:codex-legacy-profile（同样紧凑 JSON＋尾部换行）。
画像字节、release graph、历史快照一律不动。输出新旧摘要，供 RemovalReceipt 引用。"""
import hashlib, json, sys
from pathlib import Path

root, campaign_id, version = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
dry = "--dry-run" in sys.argv
selector_path = root / "catalogdata/runtime/release-catalog.json"
def ser(value): return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
raw = selector_path.read_bytes()
selector = json.loads(raw)
assert ser(selector) == raw, "selector 不是规范序列化（紧凑 JSON＋尾部换行）"
old_ref = selector["snapshot_catalog"]
old_path = root / old_ref["path"]
old_raw = old_path.read_bytes()
old = json.loads(old_raw)
assert ser(old) == old_raw and hashlib.sha256(old_raw).hexdigest() == old_ref["sha256"], "旧快照目录不是规范序列化或摘要不符"
kept = [row for row in old["snapshots"] if row["version"] != version]
removed = [row for row in old["snapshots"] if row["version"] == version]
assert removed and len(kept) + len(removed) == len(old["snapshots"]), "退休版本不在 active 快照目录里"
new = {**old, "snapshots": kept}
new_raw = ser(new)
new_digest = hashlib.sha256(new_raw).hexdigest()
new_rel = f"catalogdata/runtime/snapshot-catalogs/{new_digest}.json"
new_selector = {**selector, "snapshot_catalog": {"path": new_rel, "sha256": new_digest},
                "source": f"campaign:{campaign_id}/retirement:codex-legacy-profile"}
print(json.dumps({"retire_version": version, "old_snapshot_catalog": old_ref["sha256"], "new_snapshot_catalog": new_digest,
                  "removed_entries": [{"digest": r["digest"], "blob_sha256": r["blob_sha256"], "file": r["file"]} for r in removed],
                  "kept_versions": [r["version"] for r in kept], "release_graph_sha256": selector["release_graph"]["sha256"]}, ensure_ascii=False, indent=2))
if dry:
    sys.exit(0)
new_path = root / new_rel
assert not new_path.exists(), "新快照目录已存在"
new_path.write_bytes(new_raw)
selector_path.write_bytes(ser(new_selector))
print("已写入", new_rel, "并更新 selector")
