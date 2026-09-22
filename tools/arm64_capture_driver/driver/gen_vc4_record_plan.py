#!/usr/bin/env python3
"""生成 record-candidate-build（严格 v2 合同）动作计划：
gen_vc4_record_plan.py <image_id sha256:…> <build_id> <evidence_root> <输出文件> <campaign_id> <candidate_id> <候选目录>（数据根由环境变量 D 给出）"""
import json, os, pathlib, sys

image_id, build_id, evidence_root, out, cid, cand, bdir = sys.argv[1:]
assert image_id.startswith("sha256:") and len(image_id) == 71
D = os.environ["D"]
NEW = f"{D}/evidence/campaigns/{cid}"
B = bdir
command = [
    "/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "record-candidate-build",
    "--campaign-dir", NEW,
    "--candidate-id", cand,
    "--candidate-purpose", "production_replacement",
    "--candidate-source", f"{B}/source",
    "--candidate-binary", f"{B}/artifacts/sub2api",
    "--runtime-image", f"sub2apiplus-c0154-candidate@{image_id}",
    "--candidate-image-id", image_id,
    "--build-id", build_id,
    "--deployed-version", "0.154.0",
    "--target-architecture", "linux/arm64",
    "--build-parameters", f"{B}/artifacts/build-parameters.json",
    "--build-tree", f"{B}/build-tree",
    "--docker-context", f"{B}/artifacts/ctx",
    "--frontend-dist-source", f"{B}/frontend-dist",
    "--catalog-stage-dir", f"{B}/source/docs/egress/lifecycle/codex-0154-candidate/catalog-stage",
    "--source-transition", f"{B}/artifacts/source-transition.json",
    "--gate-plan", f"{B}/source/docs/egress/lifecycle/codex-0154-candidate/gate-plan.json",
    "--implementation-test-root", evidence_root,
    "--implementation-test-receipt", "receipt.json",
]
plan = {
    "schema_version": "codex-upgrade-vc-action-plan/v1",
    "execute_item_ids": ["record-candidate-build"],
    "reuse_item_ids": [],
    "actions": [{
        "action_id": "record-candidate-build",
        "operation": "VC-4:record-candidate-build",
        "timeout_seconds": 1800,
        "command": command,
        "item_ids": ["record-candidate-build"],
    }],
}
pathlib.Path(out).write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("record-candidate-build 计划已写:", out)
