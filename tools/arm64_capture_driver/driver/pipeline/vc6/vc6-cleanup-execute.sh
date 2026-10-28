#!/bin/bash
# VC-6 §4.6.8 清理执行（不在流水线 ORDER 里：VC-6 先登记 deferred 清理决定，老板批准后才手工跑本脚本）。
# 批准依据不再写死在脚本里：必须由调度者在环境变量 VC6_CLEANUP_APPROVED_BY 里写明（不得沿用历史批准依据）。
# 只删已登记清单内的目标；保护前缀与受保护镜像一律跳过；VC-6 金丝雀堆栈（含生产库克隆）只停止不删除（删除另行确认）；
# 删后复验生产并生成 executed 形态收据。
# 运行镜像放行规则：生产容器当前运行的镜像必须是“VC-6 最终镜像（退休后重建的那一个）”，或“其后的发布镜像”。
#   发布镜像＝先发版、生产已换成 ghcr 发布镜像之后再清理的常见顺序。认定办法：环境变量 VC6_RELEASE_IMAGE 写发布镜像引用（例如 ghcr.io/<组织>/<镜像>:<标签>），
#   脚本核对“该引用在本机的镜像 ID＝容器正在跑的镜像 ID”，且它不在待删的候选镜像清单里；没给 VC6_RELEASE_IMAGE 时，运行镜像不是最终镜像就拒绝（不猜）。
#   收据如实写明当前运行的是哪个（running_image_kind＝final 或 release，附镜像引用与 ID），不再一律写“仍是退休后最终镜像”。
# 用法：bash vc6-cleanup-execute.sh --check   只读预检，不做任何改动
#       bash vc6-cleanup-execute.sh           执行（预检不过即中止）
set -Eeuo pipefail; umask 077
MODE="${1:-execute}"
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
: "${VC6_CLEANUP_APPROVED_BY:?必须在环境变量 VC6_CLEANUP_APPROVED_BY 写明本次清理的批准依据（调度者填）}"
export VC6_CLEANUP_APPROVED_BY
CID=$NEW
N=$D/evidence/campaigns/$CID
V6=$RUNROOT/vc6; R=$(cat "$V6/vc6-retire-root.txt")
test ! -e "$N/control/vc/receipts/$CAND/cleanup-decision-executed.json" || { echo "执行收据已存在，拒绝重复执行"; exit 2; }
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 - "$D" "$R" "$N" "$START" "$MODE" <<'PY'
import json, os, shutil, subprocess, sys, datetime
from pathlib import Path
D, R, N, START, MODE = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5]
ENV = os.environ
B = D / "candidates" / ENV["CAND"]
manifest = json.loads((R / "cleanup" / "cleanup-manifest.json").read_text())
ARCHIVE_DIR = Path(ENV.get("VC6_ARCHIVE_DIR", "/root/private-archives"))   # 私有归档目录（测试可改）
PROTECTED = ("/root/docker/sub2apiplus", str(ARCHIVE_DIR), "/root/vc-rounds", str(N), str(D / "evidence"), str(D / "tools"),
             str(D / "control"), str(B / "production-tree"), str(B / "source"))
# 当前生产镜像（退休后的最终镜像）、固定回滚镜像（env 的 PRODUCTION_IMAGE）、四阶段激活镜像：三个镜像号一律现取，
# 取自 VC-6 证据根里留存的构建参数收据副本（清单里的构建产物目录会被本脚本删除，所以不读 retire-artifacts／production-artifacts）
PRODUCTION = json.loads((R / "build" / "retire-build-parameters.json").read_text())["docker_build"]["image_id"]
ROLLBACK = ENV["ROLLBACK_IMAGE_ID"]
PROD_ROOT = Path((Path(ENV["RUNROOT"]) / "vc6" / "vc6-root.txt").read_text().strip())   # 切换阶段证据根（vc6-promote.sh 写的 vc6-root.txt）
ACTIVATION = json.loads((PROD_ROOT / "build" / "production-build-parameters.json").read_text())["docker_build"]["image_id"]
PROTECTED_IMAGES = {PRODUCTION, ROLLBACK, ACTIVATION}
CANARY = ("vc6-canary-app", "vc6-canary-postgres", "vc6-canary-redis")
def docker(args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)
def image_present(ref):
    return docker(["image", "inspect", ref, "--format", "{{.Id}}"]).returncode == 0
def container_state(name):
    out = docker(["inspect", name, "--format", "{{.State.Status}}"])
    return out.stdout.strip() if out.returncode == 0 else "absent"
def protected(path):
    return any(str(path) == p or str(path).startswith(p.rstrip("/") + "/") for p in PROTECTED)
# 预检：清单目标全部存在且不落入保护前缀；候选镜像存在、不受保护、不被任何容器（含已停止）引用；生产在跑最终镜像
used = {docker(["inspect", "--format", "{{.Image}}", c]).stdout.strip() for c in docker(["ps", "-aq"]).stdout.split()}
problems = []
for target in manifest["delete_candidates"]:
    path = Path(target["path"])
    if protected(path): problems.append(f"命中保护前缀：{path}")
    if not path.exists(): problems.append(f"不存在：{path}")
for image in manifest["candidate_images"]:
    if image["image_id"] in PROTECTED_IMAGES: problems.append(f"受保护镜像：{image['reference']}")
    if image["image_id"] in used: problems.append(f"仍被容器引用：{image['reference']}")
    if not image_present(image["image_id"]): problems.append(f"镜像不存在：{image['reference']}")
def classify_running(running_id):
    """运行镜像归类：final＝VC-6 最终镜像；release＝VC6_RELEASE_IMAGE 指定的发布镜像（本机 ID 必须等于容器在跑的 ID）；否则 unknown。"""
    if running_id == PRODUCTION:
        return "final", ENV.get("VC6_RELEASE_IMAGE", "") or "", running_id
    ref = ENV.get("VC6_RELEASE_IMAGE", "")
    if ref:
        out = docker(["image", "inspect", ref, "--format", "{{.Id}}"])
        if out.returncode == 0 and out.stdout.strip() == running_id:
            return "release", ref, running_id
    return "unknown", ref, running_id
health = docker(["inspect", "sub2apiplus", "--format", "{{.State.Health.Status}}"]).stdout.strip()
running_image = docker(["inspect", "sub2apiplus", "--format", "{{.Image}}"]).stdout.strip()
running_kind, running_ref, _ = classify_running(running_image)
if health != "healthy" or running_kind == "unknown":
    problems.append(f"生产状态异常：health={health} image={running_image}（既不是 VC-6 最终镜像，也不是 VC6_RELEASE_IMAGE={running_ref or '未设置'} 指定的发布镜像）")
if running_kind == "release":
    # 发布镜像是线上在用的镜像，同样不允许被清单当候选镜像删掉
    PROTECTED_IMAGES.add(running_image)
    for image in manifest["candidate_images"]:
        if image["image_id"] == running_image: problems.append(f"受保护镜像（当前运行的发布镜像）：{image['reference']}")
canary_before = {name: container_state(name) for name in CANARY}
print(json.dumps({"mode": MODE, "targets": len(manifest["delete_candidates"]), "images": len(manifest["candidate_images"]),
                  "problems": problems, "health": health, "image_ok": running_kind != "unknown", "running_image_kind": running_kind,
                  "running_image_ref": running_ref, "canary": canary_before},
                 ensure_ascii=False))
if problems:
    sys.exit("预检未通过，不做任何改动")
if MODE == "--check":
    sys.exit(0)
removed_paths, removed_images, skipped = [], [], []
# 1. 停止 VC-6 金丝雀堆栈（可逆；容器、网络与匿名卷里的生产库克隆保留，删除另行确认）
canary_stop = []
for name in CANARY:
    if canary_before[name] == "running":
        out = docker(["stop", name])
        canary_stop.append({"container": name, "returncode": out.returncode, "state_after": container_state(name)})
# 2. 清单内目录
for target in manifest["delete_candidates"]:
    path = Path(target["path"])
    shutil.rmtree(path) if path.is_dir() else path.unlink()
    removed_paths.append({"path": str(path), "file_count": target.get("file_count"), "bytes": target.get("bytes"), "exists_after": path.exists()})
# 3. 清单内候选镜像
for image in manifest["candidate_images"]:
    out = docker(["rmi", image["reference"]])
    removed_images.append({"reference": image["reference"], "image_id": image["image_id"], "size": image["size"],
                           "returncode": out.returncode, "exists_after": image_present(image["image_id"]), "stderr": out.stderr.strip()[:200]})
END = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.strip().splitlines()[-1]
health = docker(["inspect", "sub2apiplus", "--format", "{{.State.Health.Status}}"]).stdout.strip()
running_image = docker(["inspect", "sub2apiplus", "--format", "{{.Image}}"]).stdout.strip()
running_kind, running_ref, _ = classify_running(running_image)
archives = sorted(ARCHIVE_DIR.glob("codex-" + ENV["TARGET_VERSION"] + "-*.tar.gz"))
canary_after = {name: container_state(name) for name in CANARY}
execution = {
    "schema_version": "vc6-cleanup-execution/v1", "started_at_utc": START, "completed_at_utc": END,
    "approved_by": ENV["VC6_CLEANUP_APPROVED_BY"],
    "removed_paths": removed_paths, "removed_images": removed_images, "removed_containers": [],
    "extra_process_files_removed": [], "skipped": skipped,
    "canary_stack": {"before": canary_before, "stopped": canary_stop, "after": canary_after,
                     "note": "VC-6 金丝雀堆栈（含生产库克隆的匿名卷）只停止不删除；容器、网络与卷的删除另行请示"},
    "post_checks": {"production_container_health": health, "production_image_id": running_image, "production_image_expected": PRODUCTION,
                    "running_image_kind": running_kind, "running_image_ref": running_ref,
                    "rollback_image_present": image_present(ROLLBACK), "rollback_image_id": ROLLBACK,
                    "activation_image_present": image_present(ACTIVATION),
                    "campaign_evidence_file_count": sum(1 for p in N.rglob("*") if p.is_file()),
                    "private_archives": [{"path": str(p), "bytes": p.stat().st_size} for p in archives],
                    "production_tree_present": (B / "production-tree").is_dir(), "candidate_source_present": (B / "source").is_dir(),
                    "disk": disk},
}
out = R / "cleanup" / "cleanup-execution.json"
out.write_text(json.dumps(execution, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); out.chmod(0o600)
ok = (health == "healthy" and running_kind != "unknown" and execution["post_checks"]["rollback_image_present"]
      and execution["post_checks"]["activation_image_present"] and archives
      and len(removed_paths) == len(manifest["delete_candidates"]) and all(not r["exists_after"] for r in removed_paths)
      and len(removed_images) == len(manifest["candidate_images"])
      and all(r["returncode"] == 0 and not r["exists_after"] for r in removed_images)
      and all(state != "running" for state in canary_after.values()))
print(json.dumps({"removed_paths": len(removed_paths), "removed_images": len(removed_images), "canary_after": canary_after,
                  "health": health, "image_ok": running_kind != "unknown", "running_image_kind": running_kind,
                  "rollback_present": execution["post_checks"]["rollback_image_present"], "disk": disk, "all_ok": bool(ok)}, ensure_ascii=False))
assert ok, "删后复验未通过"
PY
[ "$MODE" = "--check" ] && { echo "CLEANUP_CHECK_DONE $(date -u +%H:%M:%SZ)"; exit 0; }
python3 - "$R" "$N" "$CAND" <<'PY'
import hashlib, json, os, sys
from pathlib import Path
R, N, CAND = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
mp, dp, ep = R / "cleanup/cleanup-manifest.json", R / "cleanup/cleanup-dry-run.json", R / "cleanup/cleanup-execution.json"
execution = json.loads(ep.read_text())
# 收据里如实写明当前运行的是哪个镜像：最终镜像（先清理后发版）或其后的发布镜像（先发版换 ghcr 镜像后清理）
pc = execution["post_checks"]
if pc["running_image_kind"] == "release":
    running_text = "运行镜像是 VC-6 最终镜像之后的发布镜像 " + (pc["running_image_ref"] or "?") + "（镜像 ID " + pc["production_image_id"] + "）"
else:
    running_text = "运行镜像仍是退休后最终镜像（镜像 ID " + pc["production_image_id"] + "）"
removed = len(execution["removed_paths"]) + len(execution["removed_images"])
subject = json.loads((N / "control/vc/receipts" / CAND / "vc5-completion.json").read_text())["subject"]
facts = {"schema_version": "codex-upgrade-vc-receipt-facts/v1", "kind": "cleanup_decision", "subject": subject,
         "assertions": {"decision": "executed",
                        "reason": (os.environ["VC6_CLEANUP_APPROVED_BY"] + "；按已登记清单删除构建树／构建产物／归档复验副本"
                                   "与未被任何容器使用的候选镜像；VC-6 金丝雀堆栈只停止不删除（删除另行请示）；删除后复验生产容器 healthy、"
                                   + running_text + "、固定回滚镜像与激活镜像仍在、Campaign 证据与私有归档完好。"),
                        "target_count": removed, "target_manifest_sha256": sha(mp), "dry_run_verified": True,
                        "production_dependencies_zero": True, "unique_evidence_copy_preserved": True, "approved": True, "execution_verified": True},
         "evidence": [{"role": "cleanup_manifest", "path": mp.relative_to(N).as_posix()}, {"role": "dry_run", "path": dp.relative_to(N).as_posix()},
                      {"role": "execution", "path": ep.relative_to(N).as_posix()}]}
fp = N / "control/vc/receipts" / CAND / "cleanup-decision-executed.facts.json"
fp.write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n"); fp.chmod(0o600)
print("执行收据 facts：目标", removed)
PY
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py finalize --evidence-root "$N" --facts "control/vc/receipts/$CAND/cleanup-decision-executed.facts.json" --output "control/vc/receipts/$CAND/cleanup-decision-executed.json" | cut -c1-300
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py replay --evidence-root "$N" --receipt "control/vc/receipts/$CAND/cleanup-decision-executed.json" | cut -c1-300
echo "CLEANUP_EXECUTED_DONE $(date -u +%H:%M:%SZ)"
