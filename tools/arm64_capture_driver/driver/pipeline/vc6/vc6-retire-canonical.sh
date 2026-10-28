#!/bin/bash
# VC-6 §4.6.7：生成退休版本（env 的 RETIRE_VERSION）RemovalReceipt（退休证据根内，0600），再以批次登记 canonical retire-<版本>。
# 与更早轮次的差别：按退休版本目录下实有的画像文件现场登记（退休版本可能只有一份画像）；消费者扫描、运行投影、历史归档计数全部由脚本现场复算（不再手写常量）。
set -Eeuo pipefail; umask 077
: "${ARM64_VC_ENV:?需要 ARM64_VC_ENV（本轮参数文件）}"
export ARM64_VC_ENV
PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# 不写死 VC_STATE_DIR，理由同 vc6-canonical-activation.sh
source ${PIPE_DRIVER_DIR}/lib.sh; cd "$D"
V6=$RUNROOT/vc6; R2=$(cat "$V6/vc6-retire-root.txt")
ATT=$(cat "$V6/vc5-att.txt")
# RETIRE_VERSION 直接用参数文件的值（lib.sh 已导出）
: "${RETIRE_VERSION:?env 缺 RETIRE_VERSION}"
RETIRE_COMMIT=$(cat "$V6/vc6-retire-pc.txt")
RT=$B/retire-build-tree          # 退休后的构建树（权威仓库退休提交的检出）
PT=$B/production-build-tree      # 退休前（晋升同步提交）的构建树，用来对照运行投影前后
test -s "$R2/stages/retire_switch/stage.json"
if [ ! -e "$R2/removal-receipt.json" ]; then
python3 - "$NEWDIR" "$R2" "$RT" "$PT" "$RETIRE_VERSION" "$RETIRE_COMMIT" <<'PY'
import glob, hashlib, json, subprocess, sys
from pathlib import Path
N, R2, RT, PT, VER, COMMIT = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], sys.argv[6]
RUNTIME = "backend/internal/officialegress/catalogdata/runtime"
def sha_file(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
checkpoint = json.load(open(sorted(glob.glob(str(N / "canonical/checkpoints/*.json")))[-1]))
items = {item["item_id"]: item for item in checkpoint["items"]}
activation_sha = items["production-activation"]["source"]["sha256"]
stage = json.load(open(R2 / "stages/retire_switch/stage.json"))
projection = (R2 / "stages/retire_switch/runtime-projection-files.txt").read_text().splitlines()
assert not any(f"profiles/{VER}/" in line for line in projection) and stage["status"] == "pass"
# 1) 退休画像字节（原地保留）：路径、文件摘要由现场复算
profiles = sorted((RT / RUNTIME / "profiles" / VER).glob("*.json"))
assert len(profiles) >= 1, "退休版本画像目录为空"
retired = [{"path": f"{RUNTIME}/profiles/{VER}/{p.name}", "sha256": sha_file(p),
            "removed_from_runtime_projection_in_commit": COMMIT[:9], "state": "retained_as_frozen_terminal_artifact"} for p in profiles]
digests = [p.stem for p in profiles]
# 2) 选择器、active ReleaseGraph 与 active SnapshotCatalog 中的引用数
selector_path = RT / RUNTIME / "release-catalog.json"
selector = json.loads(selector_path.read_text())
selector_refs = selector_path.read_text().count(VER)
graph_text = (RT / RUNTIME / selector["release_graph"]["path"].split("catalogdata/runtime/", 1)[1]).read_text()
snap_path = RT / RUNTIME / selector["snapshot_catalog"]["path"].split("catalogdata/runtime/", 1)[1]
snap_text = snap_path.read_text()
catalog_refs = graph_text.count(VER) + snap_text.count(VER) + sum(graph_text.count(d) + snap_text.count(d) for d in digests)
# 3) 未知消费者：backend/ 与 frontend/ 中对退休画像路径或摘要的引用，排除已知的历史制品与历史夹具
needles = [f"catalogdata/runtime/profiles/{VER}/"] + digests + [sha_file(p) for p in profiles]
hits = {}
for needle in needles:
    out = subprocess.run(["git", "-C", str(RT), "grep", "-l", "-F", needle, "--", "backend", "frontend"], capture_output=True, text=True).stdout.split()
    for path in out:
        hits.setdefault(path, set()).add(needle[:12])
def known_historical(path):
    return (path.startswith(f"{RUNTIME}/profiles/{VER}/")                      # 保留的画像字节本身
            or path.startswith(f"{RUNTIME}/snapshot-catalogs/")                 # 历史快照（含晋升中间快照）逐字保留
            or path.startswith(f"{RUNTIME}/release-graphs/")                    # 历史发布图逐字保留
            or path.startswith("backend/internal/officialegress/profilecontract/testdata/"))  # 版本专属测试快照与索引（历史夹具）
known_tests = sorted(p for p in hits if not known_historical(p) and p.endswith("_test.go"))
unknown = sorted(p for p in hits if not known_historical(p) and p not in known_tests)
assert not unknown, f"存在未解释的引用：{unknown}"
hist_snap = sum(1 for p in (RT / RUNTIME / "snapshot-catalogs").glob("*.json") if VER in p.read_text())
hist_graph = sum(1 for p in (RT / RUNTIME / "release-graphs").glob("*.json") if VER in p.read_text())
# 4) 运行投影前后（cmd/egressruntimedump 导出的闭集）
before_lines = [l for l in (R2 / "stages/retire_switch/runtime-projection-before-files.txt").read_text().splitlines() if l.strip()]
before_files = len(before_lines)
after_files = len(projection)
# 按路径比较退休前后两份闭集（行格式“<sha256>  ./<路径>”）：移出、新增、同路径内容变化分别列出，不用笼统的“降为”
before_map = {l.split()[-1]: l.split()[0] for l in before_lines}
after_map = {l.split()[-1]: l.split()[0] for l in projection if l.strip()}
removed_paths = sorted(set(before_map) - set(after_map))
added_paths = sorted(set(after_map) - set(before_map))
changed_paths = sorted(k for k in set(before_map) & set(after_map) if before_map[k] != after_map[k])
profiles_after = sorted({line.split("profiles/")[1].split("/")[0] for line in projection if "profiles/" in line})  # 先过滤再取版本段（10-23 修：原写法对不含 profiles/ 的行越界）
intermediate = json.loads(Path(R2 / "stages/retire_switch/pre-retire-selector.json").read_text())["snapshot_catalog"]["sha256"]
receipt = {
    "schema_version": "codex-runtime-profile-removal/v1",
    "status": "complete",
    "campaign_id": checkpoint["campaign"]["campaign_id"],
    "completed_at_utc": stage["completed_at_utc"],
    "active_version": checkpoint["campaign"]["target_version"],
    "rollback_version": checkpoint["campaign"]["baseline_version"],
    "removed_version": VER,
    "production_activation_sha256": activation_sha,
    "consumer_scan": {"catalog_references": catalog_refs, "selector_references": selector_refs, "unknown_references": len(unknown)},
    "consumer_scan_method": {
        "catalog_references": f"selector 指向的 active release graph 与 active snapshot catalog 中 {VER} 及其画像摘要的出现次数（退休构建树复算）",
        "selector_references": f"catalogdata/runtime/release-catalog.json 中 {VER} 的出现次数",
        "unknown_references": (f"backend/ 与 frontend/ 中对 catalogdata/runtime/profiles/{VER}/ 路径或画像摘要（{', '.join(d[:8] + '…' for d in digests)}）的引用，"
                               "排除：保留的画像字节本身、历史 snapshot-catalogs／release-graphs 归档、profilecontract/testdata 版本专属测试快照与索引；"
                               f"另有 {len(known_tests)} 个测试文件把该画像摘要当历史夹具常量（{', '.join(known_tests)}），"
                               "它们读的是原地保留的画像字节，不是运行消费者，单列于 known_test_fixture_references；其余未解释引用 0"),
        "runtime_projection": (f"cmd/egressruntimedump 导出的运行 Catalog 闭集由 {before_files} 个文件变为 {after_files} 个：移出 {'、'.join(removed_paths) or '无'}，"
                               f"新增 {'、'.join(added_paths) or '无'}，{'、'.join(changed_paths) or '无'} 内容随 selector 切换更新；profiles 仅剩 {'、'.join(profiles_after)}；"
                               f"{VER} 不再出现在任何运行投影文件中（ARM64 退休构建树复算，证据 stages/retire_switch/runtime-projection-before-files.txt 与 runtime-projection-files.txt）"),
    },
    "known_test_fixture_references": known_tests,
    "runtime_catalog_removed": True,
    "historical_evidence_preserved": True,
    "historical_archive_references": {
        "historical_snapshot_catalogs": hist_snap, "historical_release_graphs": hist_graph, "historical_transition_test_references": len(known_tests),
        "note": f"历史 snapshot catalog 与 release graph 逐字保留，仍按内容寻址引用 {VER}；与历次退休先例一致，不计入 consumer_scan",
    },
    "retired_runtime_profiles": retired,
    "runtime_catalog_after_removal": {
        "catalog": {"path": f"{RUNTIME}/release-catalog.json", "source": selector["source"]},
        "release_graph_sha256": selector["release_graph"]["sha256"],
        "snapshot_catalog_sha256": selector["snapshot_catalog"]["sha256"],
        "promotion_intermediate_snapshot_catalog_sha256": intermediate,
    },
    "retained_bytes_rationale": (f"{VER} 的画像字节按原路径逐字保留：{', '.join(d[:8] + '…' for d in digests)}（{VER} 期 Active，其后一轮又作为 Previous 回滚版本）被历次升级的晋升／激活／终态收据逐文件登记，"
                                 "属 §4.6.7 第 1 类，不得删除、改名或移动。退休因此按运行投影口径执行：Catalog、selector 与运行投影均不再引用该版本，"
                                 "画像字节作为历史终态制品原地保留，与历次退休同一处置。"),
}
out = R2 / "removal-receipt.json"
out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); out.chmod(0o600)
print("RemovalReceipt：", out, hashlib.sha256(out.read_bytes()).hexdigest(), "激活收据", activation_sha[:12])
print("消费者扫描：", receipt["consumer_scan"], "已知测试夹具引用：", known_tests)
PY
fi
python3 - "$D" "$NEW" "$CAND" "$ATT" "$R2" "$W/action-plan-vc6-retire.json" "$RETIRE_VERSION" <<'PY'
import glob, json, sys
D, CID, CAND, ATT, R2, OUT, VER = sys.argv[1:]
N = f"{D}/evidence/campaigns/{CID}"
cp = json.load(open(sorted(glob.glob(f"{N}/canonical/checkpoints/*.json"))[-1]))
reuse = sorted(i["item_id"] for i in cp["items"] if i["item_id"].startswith("candidate-"))
action = {"action_id": f"canonical-7-retire-{VER}", "operation": "VC-6:canonical-advance-retire", "timeout_seconds": 1800,
          "command": ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "canonical-advance",
                      "--campaign-dir", f"{D}/evidence/campaigns/{CID}", "--candidate-id", CAND, "--attempt-id", ATT,
                      "--canonical-step", "retire", "--retire-version", VER, "--step-receipt", f"{R2}/removal-receipt.json"],
          "item_ids": [f"retire-{VER}"]}
plan = {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": [f"retire-{VER}"], "reuse_item_ids": reuse, "actions": [action]}
open(OUT, "w").write(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print("动作计划：", [action["action_id"]], "复用项", len(reuse))
PY
chmod 600 "$W/action-plan-vc6-retire.json"
SEQ=$(next_seq); echo "=== 批次 ${SEQ}：VC-6 retire-$RETIRE_VERSION $(utc_now)"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-6 "$SEQ" VC-5 action-plan-vc6-retire.json
python3 -c "
import json, glob, sys
p = sorted(glob.glob(sys.argv[1] + '/canonical/checkpoints/*.json'))[-1]
d = json.load(open(p)); print('latest checkpoint:', p.split('/')[-1], d['phase'], 'execute 剩余:', d['plan']['execute_item_ids'])" "$NEWDIR"
echo "VC6_RETIRE_DONE $(utc_now)"
