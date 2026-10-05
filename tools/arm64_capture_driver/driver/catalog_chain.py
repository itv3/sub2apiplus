#!/usr/bin/env python3
"""核验本轮 VC-3 资产并装入候选源码；内容寻址资产只新增或逐字复用。"""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

POINTERS = {"catalogdata/runtime/release-catalog.json", "releasecontract/testdata/release-graph.json"}
MUTABLE = POINTERS | {"profilecontract/testdata/snapshot-catalog.json"}
BLOBS = re.compile(r"(?:catalogdata/runtime/(?:profiles/[^/]+|release-graphs|snapshot-catalogs)|profilecontract/testdata/snapshots/[^/]+)/[a-f0-9]{64}\.json")
SNAPSHOT_INDEX = "profilecontract/testdata/snapshot-catalog.json"
MERGE_SCHEMA = "arm64-snapshot-index-merge/v1"


def index_rows(value: dict) -> dict:
    """这里只做可合并结构检查；画像内容和官方摘要交给候选源码的 Go 合同。"""
    if not isinstance(value, dict) or set(value) != {"schema_version", "snapshots"} or value["schema_version"] != 1:
        raise ValueError("测试快照索引 schema 非法")
    rows = value["snapshots"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("测试快照索引为空")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not {"version", "digest", "file"}.issubset(row) or set(row) - {"version", "digest", "file", "blob_sha256"}:
            raise ValueError("测试快照索引条目字段非法")
        if any(not isinstance(row[name], str) for name in ("version", "digest", "file")):
            raise ValueError("测试快照索引坐标类型非法")
        key = (row["version"], row["digest"])
        if key in result:
            raise ValueError("测试快照索引存在重复坐标")
        result[key] = row
    return result


def validate_snapshot_contract(repository: Path, checks: list[dict]) -> dict:
    """在 backend 内的临时编译目录运行候选 Go 合同；结束即清理，不改画像或索引。"""
    backend = repository / "backend"
    if not (backend / "go.mod").is_file():
        raise ValueError("候选源码缺少用于快照闭合校验的 Go 模块")
    helper = Path(__file__).with_name("snapshot_catalog_check.go")
    with tempfile.TemporaryDirectory(prefix=".snapshot-contract-", dir=backend) as directory:
        main = Path(directory) / "main.go"
        main.write_bytes(helper.read_bytes())
        result = subprocess.run(["go", "run", "-mod=readonly", str(main)], cwd=backend,
            input=json.dumps(checks, ensure_ascii=False), capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError("快照 Go 合同拒绝：" + result.stderr.strip())
    value = json.loads(result.stdout)
    if value != {"status": "passed", "catalog_count": len(checks)}:
        raise ValueError("快照 Go 合同输出不完整")
    implementation = backend / "internal/officialegress/profilecontract"
    sources = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
               for path in sorted(implementation.glob("*.go")) if not path.name.endswith("_test.go")]
    return {**value, "checker_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            "profile_contract_sha256": artifacts.digest(sources)}


def merge_snapshot_index(source: Path, destination: Path, repository: Path) -> tuple[bytes, dict]:
    original = destination / SNAPSHOT_INDEX
    incoming = source / SNAPSHOT_INDEX
    before = original.read_bytes() if original.exists() else None
    old_doc = json.loads(before) if before is not None else None
    new_doc = json.loads(incoming.read_bytes())
    previous = index_rows(old_doc) if old_doc is not None else {}
    staged = index_rows(new_doc)
    merged = dict(previous)
    for key, row in staged.items():
        if key in merged:
            old = merged[key]
            if old["file"] != row["file"] or (old.get("blob_sha256") and row.get("blob_sha256") and old["blob_sha256"] != row["blob_sha256"]):
                raise ValueError("同一 version+digest 的快照索引条目冲突")
        else:
            merged[key] = row
    document = {"schema_version": 1, "snapshots": list(previous.values()) +
                [merged[key] for key in sorted(set(merged) - set(previous))]}
    staged_root = str(source / "profilecontract/testdata")
    previous_root = str(destination / "profilecontract/testdata")
    checks = ([{"name": "before", "catalog": old_doc, "roots": [previous_root]}] if old_doc is not None else [])
    checks += [{"name": "staged", "catalog": new_doc, "roots": [staged_root, previous_root]},
               {"name": "merged", "catalog": document, "roots": [staged_root, previous_root]}]
    verified = validate_snapshot_contract(repository, checks)
    data = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    return data, {"schema_version": MERGE_SCHEMA, "status": "complete", "before_sha256": hashlib.sha256(before).hexdigest() if before else None,
        "staged_sha256": hashlib.sha256(incoming.read_bytes()).hexdigest(), "after_sha256": hashlib.sha256(data).hexdigest(),
        "preserved": [list(key) for key in previous], "added": [list(key) for key in sorted(set(staged) - set(previous))],
        "snapshot_count": len(merged), "contract_verification": verified}


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
    if lifecycle.is_absolute() or ".." in lifecycle.parts or not lifecycle.parts:
        raise ValueError("生命周期目录必须在仓库内")
    stage = repository / lifecycle / "catalog-stage"
    if any(path.is_symlink() for path in (stage, *stage.parents)) or source.resolve() == stage.resolve():
        raise ValueError("Catalog 留档目录不可信或与输入目录重叠")
    destination = repository / "backend/internal/officialegress"
    rows = receipt["inventory"]
    if not MUTABLE.issubset({row["path"] for row in rows}):
        raise ValueError("Catalog 缺少指针或测试快照索引")
    for row in rows:
        relative = row["path"]
        if relative not in MUTABLE and not BLOBS.fullmatch(relative):
            raise ValueError(f"未知 Catalog 资产路径：{relative}")
        target = destination / relative
        input_path = source / relative
        if any(parent.is_symlink() for parent in (target, *target.parents, input_path, *input_path.parents)):
            raise ValueError(f"Catalog 目标路径含符号链接：{relative}")
        if relative not in MUTABLE and target.exists() and target.read_bytes() != (source / relative).read_bytes():
            raise ValueError(f"不可变 Catalog blob 已存在且内容不同：{relative}")
    # 校验原索引、输入索引、合并后索引的实际画像内容；任何失败都在覆盖资产前发生。
    index_bytes, merge_receipt = merge_snapshot_index(source, destination, repository)
    merge_receipt["catalog_inventory_sha256"] = receipt["inventory_sha256"]
    identity = artifacts.digest({"inputs": {key: merge_receipt[key] for key in ("staged_sha256", "after_sha256", "catalog_inventory_sha256")},
        "checker": {key: merge_receipt["contract_verification"][key] for key in ("checker_sha256", "profile_contract_sha256")}})
    merge_path = repository / lifecycle / "snapshot-index-merges" / (identity + ".json")
    if any(path.is_symlink() for path in (merge_path, *merge_path.parents)):
        raise ValueError("快照合并凭证路径不可信")
    if merge_path.exists():
        old_receipt = json.loads(merge_path.read_text())
        if old_receipt.get("binding_sha256") != artifacts.digest({key: value for key, value in old_receipt.items() if key != "binding_sha256"}):
            raise ValueError("既有快照合并凭证自摘要错误")
        for field in ("schema_version", "status", "staged_sha256", "after_sha256", "catalog_inventory_sha256", "snapshot_count"):
            if old_receipt.get(field) != merge_receipt[field]:
                raise ValueError("既有快照合并凭证与实际输入结果不一致")
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(source, stage)
    for row in rows:
        target = destination / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if row["path"] == SNAPSHOT_INDEX:
            target.write_bytes(index_bytes)
        else:
            shutil.copyfile(source / row["path"], target)
        target.chmod(0o644)
    # 落盘后只读重放实际目标目录，不能靠仍保留在输入 staging 里的文件假闭合。
    written = validate_snapshot_contract(repository, [{"name": "written", "catalog": json.loads(index_bytes),
        "roots": [str(destination / "profilecontract/testdata")]}])
    merge_receipt["written_verification"] = written
    (repository / lifecycle / "gate-mapping.json").write_bytes(mapping_bytes)
    (repository / lifecycle / "gate-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if not merge_path.exists():
        merge_path.parent.mkdir(parents=True, exist_ok=True)
        merge_receipt["binding_sha256"] = artifacts.digest(merge_receipt)
        merge_path.write_text(json.dumps(merge_receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
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
