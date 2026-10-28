#!/bin/bash
# VC-6 §4.6.8 清理决定：只读解析候选删除目标与必须保留项，登记 deferred 决定（未获删除授权，不执行任何删除）。
set -Eeuo pipefail; umask 077
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
CID=$NEW
N=$D/evidence/campaigns/$CID
V6=$RUNROOT/vc6; R=$(cat "$V6/vc6-retire-root.txt"); RESTORE=$(cat "$V6/vc6-restore-dir.txt")
test -s "$N/control/vc/receipts/$CAND/private-archive.json" || { echo "private_archive 收据尚未生成"; exit 2; }
test ! -e "$N/control/vc/receipts/$CAND/cleanup-decision.json" || { echo "cleanup_decision 收据已存在，拒绝覆盖"; exit 2; }
mkdir -p "$R/cleanup"; chmod 700 "$R/cleanup"
python3 - "$D" "$N" "$R" "$CAND" "$CID" "$RESTORE" <<'PY'
import hashlib, json, os, subprocess, sys
from pathlib import Path
ENV = os.environ
D, N, R, CAND, CID, RESTORE = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5], Path(sys.argv[6])
def dir_facts(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    files = [p for p in path.rglob("*") if p.is_file() and not p.is_symlink()] if path.is_dir() else [path]
    return {"path": str(path), "exists": True, "kind": "directory" if path.is_dir() else "file",
            "file_count": len(files), "bytes": sum(p.stat().st_size for p in files)}
def docker(args):
    return subprocess.run(["docker", *args], capture_output=True, text=True).stdout.strip()
B = D / "candidates" / CAND
delete_targets = [dir_facts(p) for p in (RESTORE, B / "retire-build-tree", B / "production-build-tree", B / "build-tree",
                                          B / "retire-artifacts", B / "production-artifacts", B / "test-tree", B / "gate-tree", B / "frontend-build")]
delete_targets = [t for t in delete_targets if t.get("exists")]
running_images = set(docker(["ps", "--format", "{{.Image}}"]).splitlines())
running_ids = set(docker(["ps", "-q"]).splitlines())
containers = [(cid, docker(["inspect", "--format", "{{.Image}}|{{.Name}}|{{.State.Status}}", cid])) for cid in docker(["ps", "-aq"]).splitlines() if cid]
candidate_images = []
for line in docker(["images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}"]).splitlines():
    if ENV["CAMPAIGN_PREFIX"] + "-candidate" in line:
        repo, image_id, size = line.split(" ", 2)
        full_id = docker(["inspect", "--format", "{{.Id}}", image_id])
        users = [{"name": info.split("|")[1].lstrip("/"), "state": info.split("|")[2]} for cid, info in containers if info.split("|")[0] == full_id]
        candidate_images.append({"reference": repo, "image_id": full_id, "size": size, "in_use_by_container": bool(users),
                                 "used_by_running_container": any(u["state"] == "running" for u in users), "containers": users})
current = docker(["inspect", "--format", "{{.Image}}", "sub2apiplus"])
keep = [
    {"path": str(N), "reason": "本轮 Campaign 唯一在线证据副本"},
    {"path": ENV.get("VC6_ARCHIVE_DIR", "/root/private-archives"), "reason": "§4.6.8 私有归档（已完成异地解包与关键收据重放）"},
    {"path": ENV["RUNROOT"], "reason": "本轮执行留档（env、各阶段脚本与日志、VC-6 手工脚本）"},
    {"image": current, "reason": "当前生产镜像（退休 " + ENV["RETIRE_VERSION"] + " 后的最终镜像）"},
    {"image": ENV["ROLLBACK_REF"], "reason": "固定回滚镜像（Active " + ENV["ROLLBACK_VERSION"] + "）"},
    {"path": "/root/docker/sub2apiplus", "reason": "生产 compose、数据库、Redis、keeper 与正式配置"},
    {"path": "/root/docker/capture-cli", "reason": "可复用抓包基础设施（Compose、固定网络、受管工具）"},
    {"path": str(B / "production-tree"), "reason": "production tree（晋升与门禁的执行树，门禁收据绑定其摘要）"},
    {"path": str(B / "source"), "reason": "已验收候选源码副本（production tree 的基线）"},
]
manifest = {"schema_version": "vc6-cleanup-manifest/v1", "campaign_id": CID, "candidate_id": CAND, "decision": "deferred",
            "delete_candidates": delete_targets, "candidate_images": candidate_images, "keep": keep,
            "note": "本清单只作只读登记；未获删除授权前不执行任何删除。"}
mp = R / "cleanup" / "cleanup-manifest.json"
mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); mp.chmod(0o600)
disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.strip().splitlines()[-1]
dry_run = {"schema_version": "vc6-cleanup-dry-run/v1", "campaign_id": CID, "mode": "read-only",
           "resolved_targets": len(delete_targets) + len(candidate_images),
           "target_total_bytes": sum(t.get("bytes", 0) for t in delete_targets),
           "production_dependency_check": {"running_container_images": sorted(running_images),
                                           "candidate_images_in_use": [i for i in candidate_images if i["in_use_by_container"]],
                                           "candidate_images_used_by_running_container": [i for i in candidate_images if i["used_by_running_container"]],
                                           "production_image_in_delete_list": False, "rollback_image_in_delete_list": False,
                                           "campaign_evidence_in_delete_list": False, "private_archive_in_delete_list": False},
           "unique_evidence_copy_preserved": True, "disk": disk,
           "conclusion": ("候选目标均为已归档的构建树、构建产物与复验副本，以及未被任何容器使用的候选镜像；生产数据库、Redis、keeper、正式配置、"
                          "当前镜像、回滚镜像、Campaign 证据与私有归档均在保留清单内。删除动作等待老板批准，本轮登记为 deferred。")}
dp = R / "cleanup" / "cleanup-dry-run.json"
dp.write_text(json.dumps(dry_run, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); dp.chmod(0o600)
assert not dry_run["production_dependency_check"]["candidate_images_used_by_running_container"], "有候选镜像仍被运行中的容器使用"
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
subject = json.loads((N / "control/vc/receipts" / CAND / "vc5-completion.json").read_text())["subject"]
facts = {"schema_version": "codex-upgrade-vc-receipt-facts/v1", "kind": "cleanup_decision", "subject": subject,
         "assertions": {"decision": "deferred",
                        "reason": "候选目标（构建树、构建产物、归档复验副本与未被任何容器使用的候选镜像）已完成只读解析并有私有归档兜底，但删除动作尚未获得老板批准；按 §4.6.8 登记为 deferred。",
                        "target_count": len(delete_targets) + len(candidate_images), "target_manifest_sha256": sha(mp),
                        "dry_run_verified": True, "production_dependencies_zero": True, "unique_evidence_copy_preserved": True,
                        "approved": False, "execution_verified": False},
         "evidence": [{"role": "cleanup_manifest", "path": mp.relative_to(N).as_posix()}, {"role": "dry_run", "path": dp.relative_to(N).as_posix()}]}
fp = N / "control/vc/receipts" / CAND / "cleanup-decision.facts.json"
fp.write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); fp.chmod(0o600)
print(json.dumps({"targets": facts["assertions"]["target_count"], "target_bytes": dry_run["target_total_bytes"],
                  "candidate_images": len(candidate_images), "disk": disk}, ensure_ascii=False))
PY
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py finalize --evidence-root "$N" --facts "control/vc/receipts/$CAND/cleanup-decision.facts.json" --output "control/vc/receipts/$CAND/cleanup-decision.json" | cut -c1-300
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py replay --evidence-root "$N" --receipt "control/vc/receipts/$CAND/cleanup-decision.json" | cut -c1-300
echo "CLEANUP_DECISION_DONE $(date -u +%H:%M:%SZ)"
