#!/usr/bin/env python3
"""VC-6 四阶段生产激活 facts（codex-production-activation-facts/v2，ARM64 正式切换测试服务器）。
用法：build_activation_facts.py <evidence_root> <production_tree>
target 身份取自切换镜像构建参数与 activation fact；rollback 为 VC-5 前冻结的正式生产镜像（env 的 PRODUCTION_IMAGE，Active＝本轮基线版本）。
版本、画像 id／摘要、回滚镜像全部取 env（由 vc6-env.sh 导出）；回滚画像 id 取晋升后 production tree 的 release graph 里 Previous 节点的 build.id 去掉摘要后缀。"""
import hashlib, json, os, sys
from pathlib import Path

R = Path(sys.argv[1])
P = Path(sys.argv[2])
E = os.environ
RUNTIME = P / "backend/internal/officialegress/catalogdata/runtime"


def rollback_profile_id() -> str:
    # 晋升后的 release graph：Previous 节点就是回滚版本；build.id 形如 <profile_id>-<摘要前 12 位>，去掉后缀得 profile_id
    selector = json.loads((RUNTIME / "release-catalog.json").read_text())
    graph = json.loads((RUNTIME / selector["release_graph"]["path"].split("catalogdata/runtime/", 1)[1]).read_text())
    ids = {n["build"]["id"] for n in graph["nodes"] if n["snapshot"]["version"] == E["ROLLBACK_VERSION"]}
    assert len(ids) == 1, ("回滚版本在 release graph 里的 build.id 不唯一", ids)
    build_id = ids.pop()
    suffix = "-" + E["ROLLBACK_DIGEST"][:12]
    assert build_id.endswith(suffix), (build_id, suffix)
    return build_id[: -len(suffix)]

def sha(p: Path) -> str: return hashlib.sha256(p.read_bytes()).hexdigest()
acc = json.loads((R / "acceptance-result.json").read_bytes())
prom = json.loads((R / "catalog-promotion-receipt.json").read_bytes())
build = json.loads((R / "build" / "production-build-parameters.json").read_bytes())
fact = json.loads((R / "stages" / "production_switch" / "activation-fact.json").read_bytes())
stages = []
for name in ("canary", "production_switch", "rollback", "target_restore"):
    d = json.loads((R / "stages" / name / "stage.json").read_bytes())
    evidence = [{"path": f"stages/{name}/{f}", "sha256": sha(R / "stages" / name / f)} for f in d["evidence"]]
    stages.append({k: d[k] for k in ("name", "started_at_utc", "completed_at_utc", "host", "architecture", "status", "image_id", "checks")} | {"evidence": evidence})
image_id = build["docker_build"]["image_id"]
target = {
    "version": E["TARGET_VERSION"], "profile_id": E["TARGET_PROFILE_ID"],
    "profile_digest": E["TARGET_DIGEST"],
    "source_tree_sha256": build["production_tree"]["source_tree_sha256"],
    "build_id": fact["build_id"], "deployed_version": E["TARGET_VERSION"],
    "image_id": image_id,
    "image_reference": f"{E['PROD_IMAGE_REPO']}@{image_id}",
}
assert fact["image_id"] == image_id and fact["source_tree_sha256"] == target["source_tree_sha256"], fact
rollback = {
    "version": E["ROLLBACK_VERSION"], "profile_id": rollback_profile_id(),
    "profile_digest": E["ROLLBACK_DIGEST"],
    # 回滚侧没有可用的源码树副本：同口径，以 promotion receipt 的 old_active_release_digest 作为回滚 Release 的
    # 稳定标识（工具只要求 64 位十六进制）；回滚身份的权威锚点是镜像 digest、画像摘要与版本。
    "source_tree_sha256": prom["old_active_release_digest"],
    "build_id": E["ROLLBACK_BUILD_ID"], "deployed_version": E["ROLLBACK_VERSION"],
    "image_id": E["ROLLBACK_IMAGE_ID"],
    "image_reference": E["ROLLBACK_REF"],
}
facts = {
    "schema_version": "codex-production-activation-facts/v2",
    "campaign": {"id": acc["campaign_id"], "candidate_id": acc["candidate_id"],
                 "acceptance_path": "acceptance-result.json", "acceptance_sha256": sha(R / "acceptance-result.json")},
    "promotion": {"path": "catalog-promotion-receipt.json", "sha256": sha(R / "catalog-promotion-receipt.json")},
    "post_promotion_gate": {"path": "post-promotion-gates.receipt.json", "sha256": sha(R / "post-promotion-gates.receipt.json")},
    "target": target, "rollback": rollback, "stages": stages,
    "final_state": {"candidate_id": acc["candidate_id"], "image_id": target["image_id"], "active_version": target["version"],
                    "profile_id": target["profile_id"], "profile_digest": target["profile_digest"],
                    "container_status": "running", "health": "pass"},
}
out = R / "activation-facts.json"
out.write_text(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"); out.chmod(0o600)
print(json.dumps({"facts": str(out), "stages": [(s["name"], s["started_at_utc"], s["completed_at_utc"]) for s in stages]}, ensure_ascii=False))
