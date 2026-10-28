#!/bin/bash
# VC-6 §4.6.1～§4.6.2：在 VC-5 已验收的候选提交 C 上建受限 production tree，执行 egresscatalogpromote，
# 按晋升清单把文件放进 production tree，并建立本轮 VC-6 证据根与 post-promotion attempt ID。
#   - production tree 只允许清单内的 Catalog／contract 变化（历史先例：清单 8 项、实际变化 3 项）；
#   - 晋升前核对候选 Catalog 为 Active＝回滚版本（本轮基线）、Previous＝目标版本；
#   - 输出目录与 production tree 都必须尚不存在，重跑先人工归档。
set -Eeuo pipefail; umask 022
export PATH=/usr/local/go/bin:$PATH PYTHONDONTWRITEBYTECODE=1
D=${D:-/root/docker/capture-cli/data}
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
CID=$NEW
N=$D/evidence/campaigns/$CID
B=$D/candidates/$CAND
P=$B/production-tree
O=$B/production-catalog-$STAMP
W=$V6
# C（已验收候选提交）、TARGET_DIGEST、ROLLBACK_DIGEST 由 vc6-env.sh 从 env.sh 现取
# 验收结果取当前评估基线的规范读源（与 vc5-completion.json 的 acceptance_fact 同一份；本轮 approval-revision 后在 b2）。
ACC=$(cd "$D" && PYTHONPATH="$D" PYTHONDONTWRITEBYTECODE=1 python3 -B -c "import sys; from pathlib import Path; from tools.official_client_capture import codex_upgrade as cu; c = Path(sys.argv[1]); b, _ = cu._current_evaluation_baseline(c, sys.argv[2]); print(cu._stage_read_source(c, sys.argv[2], b, 'accept')['path'])" "$N" "$CAND")
mkdir -p "$W"; chmod 700 "$W"
test -s "$ACC"; test ! -e "$P"; test ! -e "$O"
echo "=== production tree（已验收候选提交 ${C:0:9}）$(date -u +%H:%M:%SZ)"
git clone -q --no-checkout "$B/source" "$P"
git -C "$P" checkout -q --detach "$C"
test "$(git -C "$P" rev-parse HEAD)" = "$C"
test -z "$(git -C "$P" status --porcelain --untracked-files=all)"
git config --global --add safe.directory "$P" 2>/dev/null || true
echo "=== 晋升前核对候选 Catalog：Active=${ROLLBACK_VERSION}、Previous=${TARGET_VERSION}"
python3 - "$P" <<'PY'
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1]) / "backend/internal/officialegress"
selector = json.loads((root / "catalogdata/runtime/release-catalog.json").read_text())
graph = json.loads((root / selector["release_graph"]["path"]).read_text())
modes = {}
for node in graph["nodes"]:
    modes.setdefault(node["mode"], set()).add(node["build"]["version"])
print("候选 release graph 槽位：", {k: sorted(v) for k, v in modes.items()})
assert modes == {"active": {os.environ["ROLLBACK_VERSION"]}, "previous": {os.environ["TARGET_VERSION"]}}, modes
PY
ACC_SHA=$(sha256sum "$ACC" | cut -c1-64); echo "acceptance_sha256=$ACC_SHA"
echo "=== egresscatalogpromote $(date -u +%H:%M:%SZ)"
( cd "$P/backend" && go run ./cmd/egresscatalogpromote -campaign-id "$CID" -acceptance-sha256 "$ACC_SHA" \
    -target-version "$TARGET_VERSION" -target-profile-digest "$TARGET_DIGEST" \
    -rollback-version "$ROLLBACK_VERSION" -rollback-profile-digest "$ROLLBACK_DIGEST" -output "$O" ) > "$W/promote.out" 2> "$W/promote.err"
python3 - "$W/promote.out" "$O" "$P" <<'PY'
import hashlib, json, shutil, sys
from pathlib import Path
receipt = json.loads(Path(sys.argv[1]).read_text())
out, tree = Path(sys.argv[2]), Path(sys.argv[3])
base = tree / "backend/internal/officialegress"
print({k: receipt.get(k) for k in ("schema_version", "target_version", "rollback_version", "production_selector_changed",
                                   "promoted_active_release_digest", "promoted_previous_release_digest", "inventory_sha256")})
changed = []
for item in receipt["inventory"]:
    src = out / item["path"]; dst = base / item["path"]
    data = src.read_bytes()
    assert hashlib.sha256(data).hexdigest() == item["sha256"] and len(data) == item["size"], item["path"]
    if not dst.exists() or dst.read_bytes() != data:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        changed.append(item["path"])
print("清单", len(receipt["inventory"]), "项，实际变化", len(changed), "项：", changed)
PY
echo "=== production tree 差异（只允许清单内 Catalog／contract）"
git -C "$P" status --porcelain --untracked-files=all
python3 - "$P" <<'PY'
import subprocess, sys
lines = subprocess.run(["git", "-C", sys.argv[1], "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True, check=True).stdout.splitlines()
allowed = ("backend/internal/officialegress/catalogdata/runtime/", "backend/internal/officialegress/releasecontract/testdata/")
bad = [line for line in lines if not line[3:].startswith(allowed)]
assert not bad, bad
print("差异全部在清单允许范围内：", len(lines), "项")
PY
cd "$D" && PYTHONPATH=. python3 -B - "$P" > "$W/vc6-tree.txt" <<'PY'
import sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
print(cu._directory_tree_digest(Path(sys.argv[1])))
PY
echo "production_tree_sha256=$(cat "$W/vc6-tree.txt")"; chmod 600 "$W/vc6-tree.txt"
TS=$(date -u +%Y%m%dt%H%M%Sz)
R=$N/control/vc6-arm64-$TS; install -d -m 700 "$R"; echo "$R" > "$W/vc6-root.txt"
echo "vc6-post-promotion-$TS" > "$W/vc6-gates-attempt.txt"
chmod 600 "$W/vc6-root.txt" "$W/vc6-gates-attempt.txt" "$W/promote.out" "$W/promote.err"
echo "VC6_ROOT=$R ATT=$(cat "$W/vc6-gates-attempt.txt")"
echo "PROMOTE_DONE $(date -u +%H:%M:%SZ)"
