#!/usr/bin/env python3
"""核验本轮 VC-3 资产并装入候选源码；内容寻址资产只新增或逐字复用。"""

import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

POINTERS = {"catalogdata/runtime/release-catalog.json", "releasecontract/testdata/release-graph.json"}
MUTABLE = POINTERS | {"profilecontract/testdata/snapshot-catalog.json"}
BLOBS = re.compile(r"(?:catalogdata/runtime/(?:profiles/[^/]+|release-graphs|snapshot-catalogs)|profilecontract/testdata/snapshots/[^/]+)/[a-f0-9]{64}\.json")


def assemble(source: Path, repository: Path, lifecycle: Path, requirements_path: Path, mapping_path: Path) -> dict:
    """先验证所有输入和既有 blob，再写资产；不会根据旧映射自动猜测新门禁。"""

    receipt = json.loads((source / "catalog-stage-receipt.json").read_text())
    upgrade._verify_catalog_stage_output(source, receipt)
    requirements = artifacts.validate_gate_requirements(json.loads(requirements_path.read_text()))
    for field in ("campaign_id", "target_version"):
        if receipt.get(field) != requirements[field]:
            raise ValueError(f"Catalog 与门禁需求的 {field} 不一致")
    if receipt.get("post_promotion_gate_requirements_sha256") != requirements["requirements_sha256"]:
        raise ValueError("Catalog 未绑定本轮门禁需求")
    mapping_bytes = mapping_path.read_bytes()
    plan = artifacts.build_gate_plan(requirements, json.loads(mapping_bytes),
                                    mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest())
    if lifecycle.is_absolute() or ".." in lifecycle.parts:
        raise ValueError("生命周期目录必须在仓库内")
    destination = repository / "backend/internal/officialegress"
    rows = receipt["inventory"]
    if not MUTABLE.issubset({row["path"] for row in rows}):
        raise ValueError("Catalog 缺少指针或测试快照索引")
    for row in rows:
        relative = row["path"]
        if relative not in MUTABLE and not BLOBS.fullmatch(relative):
            raise ValueError(f"未知 Catalog 资产路径：{relative}")
        target = destination / relative
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise ValueError(f"Catalog 目标路径含符号链接：{relative}")
        if relative not in MUTABLE and target.exists() and target.read_bytes() != (source / relative).read_bytes():
            raise ValueError(f"不可变 Catalog blob 已存在且内容不同：{relative}")
    stage = repository / lifecycle / "catalog-stage"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(source, stage)
    for row in rows:
        target = destination / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / row["path"], target)
        target.chmod(0o644)
    (repository / lifecycle / "gate-mapping.json").write_bytes(mapping_bytes)
    (repository / lifecycle / "gate-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    for path in (repository / lifecycle).rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return plan


if __name__ == "__main__":
    try:
        plan = assemble(*(Path(value) for value in sys.argv[1:]))
        print(json.dumps({"campaign_id": plan["campaign_id"], "gate_count": plan["gate_count"]}, ensure_ascii=False))
    except (ValueError, OSError, upgrade.ConfigurationError, artifacts.VCArtifactError) as error:
        print(f"候选资产拒绝装配：{error}", file=sys.stderr)
        raise SystemExit(3)
