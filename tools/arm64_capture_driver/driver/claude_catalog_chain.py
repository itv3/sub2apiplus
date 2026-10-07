#!/usr/bin/env python3
"""Claude 候选源码链的取料核验与装配（Claude 升级 B3-04a，本机 ``local/local-claude-candidate-chain.sh`` 调用）。

三段提交（设计 B3-04「第三步实施结果」）：

* A（资产）：VC-3 暂存 Release 里逐字节取的画像与 Wire（``catalogdata/claude/...`` 落到 ``backend/internal/officialegress/``，
  内容寻址：已存在就必须逐字节相同）、改名为 ``claude_upgrade_wire_projection_<版本下划线>.json`` 的投影策略（受管树
  ``tools/official_client_capture/``），以及 lifecycle 目录下四个文件——``catalog-stage/`` 里的暂存 Release 收据与 Release 条目
  （先删再写，恰好两个）、门禁映射 ``gate-mapping.json``（``GATE_MAPPING_INPUT`` 原字节）、门禁计划 ``gate-plan.json``
  （``build_gate_plan``，indent 2、键排序、尾换行，与 ``plan-candidate-gates`` 写出的字节相同）；
* C：只有 ``catalogdata/claude/release-catalog.json``——底稿是 VC-3 冻结的 Active 目录，``releases`` 末尾追加暂存 Release，
  ``selectors.validation_candidate`` 换成暂存收据给出的选择器，其余不动（仓库格式：indent 2、不排序键、尾换行）；
* D：``freeze-successor-generate --before A --after C`` 生成的承接收据（新增文件在基线上没有 blob，所以只能是 A→C）。

文件落点与 ``claude_upgrade_candidate.verify_candidate_catalog`` 同一组常量；A、C 写完后用的验收也正是它（record-candidate-build
在采集主机上按同一函数复算）。

取料（本机脚本用一条只读 ssh tar 取回）：Campaign 的 ``campaign.json``／``campaign.sha256``、``profile/`` 下的阶段结果、准备结果、
准备包、暂存 Release 与联合清单，以及采集主机小步骤写的门禁需求预览 ``vc4-gate-requirements.json``。核验按摘要链：总计划
自摘要 → ``claude_upgrade_profile.load_stage_result``（准备结果、准备包逐文件、联合清单、暂存 Release 逐文件）→ 需求预览
绑定同一 Campaign、候选、目标版本与联合摘要。另要求 VC-3 冻结的 Active 目录副本与仓库当前（A 的父提交）的
``release-catalog.json`` 逐字节相同：不同说明主线在 VC-3 之后改过目录，候选链停下（r≥2 的候选也从不含候选目录改动的
提交开始，同一 Campaign 的暂存 Release 共用、内容寻址文件逐字复用）。

子命令（输出一行 JSON；任何不符都在写入之前拒绝，退出码 3）：

* ``assets``：核验取料并装配 A 段，输出写入的仓库相对路径（交给本机脚本精确 ``git add``）与门禁计划摘要；
* ``catalog``：装配 C 段 release-catalog.json，再用候选源码里的 Go 加载器（同目录 ``claude_catalog_check.go``，在 backend 内的
  临时目录 ``go run``）核对新目录能被加载、validation_candidate 指向暂存 Release、production_active 不变，并以
  ``verify_candidate_catalog`` 验收；任一项不过即把 release-catalog.json 还原成原字节；
* ``transition``：核对 D 段承接收据——结果为 ``passed_local_evidence_successor``、未登记与人工动作为 0、transitions 恰好只有
  release-catalog.json，并以 Claude 的 source transition 校验（与 record-candidate-build 同一函数）验收。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

sys.dont_write_bytecode = True

from tools.official_client_capture import claude_upgrade_candidate as candidate
from tools.official_client_capture import claude_upgrade_profile as profile
from tools.official_client_capture import claude_upgrade_release as release
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

REQUIREMENTS_FILENAME = "vc4-gate-requirements.json"
CAMPAIGN_SCHEMA = "official-client-upgrade-campaign/v1"
CATALOG_STAGE_DIRNAME = "catalog-stage"
FREEZE_RESULT_PASSED = "passed_local_evidence_successor"
GO_CHECK_TIMEOUT_SECONDS = 900


class ChainError(ValueError):
    """取料、装配或验收不满足合同。"""


# ---------------------------------------------------------------------------
# 取料核验
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ChainError(f"{label}缺失或不是普通文件：{path}")
    return path


def load_take(take: Path, *, campaign_id: str, candidate_id: str, target_version: str) -> dict[str, Any]:
    """核验取料目录，返回 Campaign 总计划、VC-3 阶段结果、暂存 Release 收据、Active 目录副本路径与门禁需求。"""

    take = take.resolve(strict=True)
    manifest_path = _plain_file(take / "campaign.json", "Campaign 总计划")
    expected = _plain_file(take / "campaign.sha256", "Campaign 总计划摘要").read_text(encoding="utf-8").strip()
    if _sha256(manifest_path.read_bytes()) != expected:
        raise ChainError("Campaign 总计划与 campaign.sha256 不一致")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version"), manifest.get("client"), manifest.get("campaign_mode")) != (CAMPAIGN_SCHEMA, "claude", "formal"):
        raise ChainError("取料不是 Claude 的 Formal Campaign 总计划")
    if (manifest.get("campaign_id"), manifest.get("target_version")) != (campaign_id, target_version):
        raise ChainError("取料的 Campaign 或目标版本与本轮参数不一致")
    stage = profile.load_stage_result(take, manifest)
    staged = release.load_staged_release(take, stage["staged_release"])
    if staged.get("campaign_id") != campaign_id or staged.get("target_version") != target_version:
        raise ChainError("暂存 Release 收据的 Campaign 或目标版本与本轮不一致")
    prepared = profile.load_prepare_result(take, manifest, str(stage["prepared_revision"]))
    active = _plain_file(
        take / str(prepared["prepared"][profile.PREPARED_EXTRA_FILES["active_release_catalog"]]["path"]), "VC-3 冻结的 Active 目录"
    )
    requirements = load_requirements(take / REQUIREMENTS_FILENAME, campaign_id=campaign_id, candidate_id=candidate_id,
                                     target_version=target_version, joint_sha256=str(stage["joint_sha256"]))
    return {"root": take, "manifest": manifest, "stage": stage, "staged": staged, "active": active, "requirements": requirements}


def load_requirements(path: Path, *, campaign_id: str, candidate_id: str, target_version: str, joint_sha256: str) -> dict[str, Any]:
    """采集主机小步骤写的门禁需求预览：状态与归属核对，需求按共享合同复算并绑定本轮联合摘要。"""

    preview = json.loads(_plain_file(path, "门禁需求预览").read_text(encoding="utf-8"))
    if (preview.get("status"), preview.get("mode"), preview.get("live_request_count")) != ("awaiting_gate_mapping", "gate_requirements", 0):
        raise ChainError("门禁需求预览的状态、模式或请求计数不符")
    if (preview.get("campaign_id"), preview.get("candidate_id")) != (campaign_id, candidate_id):
        raise ChainError("门禁需求预览不属于本轮 Campaign 与候选")
    try:
        requirements = artifacts.validate_gate_requirements(preview.get("requirements"))
    except artifacts.VCArtifactError as error:
        raise ChainError(f"门禁需求无法按共享合同复算：{error}") from error
    if preview.get("requirements_sha256") != requirements["requirements_sha256"]:
        raise ChainError("门禁需求预览登记的摘要与需求本身不一致")
    if (requirements["campaign_id"], requirements["target_version"], requirements["joint_manifest_sha256"]) != (
        campaign_id, target_version, joint_sha256
    ):
        raise ChainError("门禁需求没有绑定本轮 Campaign、目标版本或 VC-3 联合摘要")
    return requirements


# ---------------------------------------------------------------------------
# A 段：资产装配
# ---------------------------------------------------------------------------


def _safe_target(repository: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ChainError(f"装配路径越界：{relative}")
    target = repository.joinpath(*relative.parts)
    for path in (target, *target.parents):
        if path == repository.parent:
            break
        if path.is_symlink():
            raise ChainError(f"装配路径含符号链接：{path}")
    return target


def asset_files(loaded: Mapping[str, Any], lifecycle: PurePosixPath, mapping_bytes: bytes) -> tuple[dict[PurePosixPath, bytes], dict[str, Any], set[PurePosixPath]]:
    """A 段要写的文件（仓库相对路径 → 字节）、门禁计划与内容寻址路径集合；落点与 verify_candidate_catalog 同一组常量。"""

    staged = loaded["staged"]
    root = loaded["root"] / release.STAGED_RELEASE_ROOT
    version = str(loaded["manifest"]["target_version"])
    stage_dir = lifecycle / CATALOG_STAGE_DIRNAME
    files: dict[PurePosixPath, bytes] = {}
    immutable: set[PurePosixPath] = set()
    for item in staged["inventory"]:
        relative = PurePosixPath(str(item["path"]))
        data = root.joinpath(*relative.parts).read_bytes()
        if relative.parts[0] == "catalogdata":
            target = candidate.REPOSITORY_CATALOG_PARENT / relative
            immutable.add(target)
        elif relative.as_posix() == release.PROJECTION_POLICY_COPY_FILENAME:
            target = candidate.PROJECTION_POLICY_TREE_DIRECTORY / release.PROJECTION_POLICY_FILENAME.format(underscored=version.replace(".", "_"))
            immutable.add(target)
        elif relative.as_posix() == release.RELEASE_ENTRY_FILENAME:
            target = stage_dir / release.RELEASE_ENTRY_FILENAME
        else:
            raise ChainError(f"暂存 Release 含未登记的文件：{relative}")
        files[target] = data
    files[stage_dir / release.STAGED_RECEIPT_FILENAME] = (root / release.STAGED_RECEIPT_FILENAME).read_bytes()
    if {path.name for path in files if path.parent == stage_dir} != set(candidate.CATALOG_STAGE_FILES):
        raise ChainError("Catalog stage 的两个文件与 verify_candidate_catalog 的约定不一致")
    try:
        plan = artifacts.build_gate_plan(loaded["requirements"], json.loads(mapping_bytes), mapping_sha256=_sha256(mapping_bytes))
    except (artifacts.VCArtifactError, json.JSONDecodeError) as error:
        raise ChainError(f"门禁映射不能按本轮需求生成计划：{error}") from error
    files[lifecycle / "gate-mapping.json"] = mapping_bytes
    files[lifecycle / "gate-plan.json"] = (json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return files, plan, immutable


def assemble_assets(loaded: Mapping[str, Any], repository: Path, lifecycle: PurePosixPath, mapping_bytes: bytes) -> dict[str, Any]:
    """先验证全部输入、Active 底稿与既有内容寻址文件，再写 A 段；返回写入清单与计划摘要。"""

    repository = repository.resolve(strict=True)
    if lifecycle.is_absolute() or ".." in lifecycle.parts or not lifecycle.parts:
        raise ChainError("生命周期目录必须是仓库内相对路径")
    catalog = _safe_target(repository, candidate.RELEASE_CATALOG_RELATIVE)
    if catalog.read_bytes() != loaded["active"].read_bytes():
        raise ChainError("VC-3 冻结的 Active 目录与仓库当前的 release-catalog.json 不同：主线在 VC-3 之后改过 Claude 目录，候选链停下")
    files, plan, immutable = asset_files(loaded, lifecycle, mapping_bytes)
    targets = {relative: _safe_target(repository, relative) for relative in files}
    for relative in sorted(immutable):
        target = targets[relative]
        if target.exists() and target.read_bytes() != files[relative]:
            raise ChainError(f"内容寻址文件已存在且内容不同：{relative}（批准后变化须建立后继 Campaign）")
    stage_dir = _safe_target(repository, lifecycle / CATALOG_STAGE_DIRNAME)
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    for relative, data in sorted(files.items(), key=lambda pair: pair[0].as_posix()):
        target = targets[relative]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
    for path in repository.joinpath(*lifecycle.parts).rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return {
        "written": sorted(relative.as_posix() for relative in files),
        "release_sha256": str(loaded["staged"]["release_entry"]["release_sha256"]),
        "gate_count": plan["gate_count"],
        "plan_sha256": plan["plan_sha256"],
        "requirements_sha256": plan["requirements_sha256"],
    }


# ---------------------------------------------------------------------------
# C 段：release-catalog.json
# ---------------------------------------------------------------------------


def expected_catalog(loaded: Mapping[str, Any]) -> bytes:
    """Active 目录逐项保留，releases 末尾追加暂存 Release，validation_candidate 换成暂存收据的选择器（与验收同一推法）。"""

    document = json.loads(loaded["active"].read_text(encoding="utf-8"))
    staged = loaded["staged"]
    document["releases"] = [*document["releases"], staged["release_entry"]]
    document["selectors"]["validation_candidate"] = dict(staged["validation_candidate_selector"])
    if document["selectors"]["validation_candidate"].get("changeset") == document["selectors"]["production_active"].get("changeset"):
        raise ChainError("validation_candidate 与 production_active 的 changeset 相同（Go 加载器会拒绝折叠）")
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def go_catalog_check(repository: Path) -> dict[str, Any]:
    """在 backend 内的临时目录 go run 同目录的 claude_catalog_check.go（读候选源码里嵌入的 Claude 目录），结束即清理。"""

    backend = repository / "backend"
    if not (backend / "go.mod").is_file():
        raise ChainError("候选源码缺少 backend Go 模块")
    helper = Path(__file__).resolve().with_name("claude_catalog_check.go")
    with tempfile.TemporaryDirectory(prefix=".claude-catalog-check-", dir=backend) as directory:
        main = Path(directory) / "main.go"
        main.write_bytes(helper.read_bytes())
        completed = subprocess.run(["go", "run", "-mod=readonly", str(main)], cwd=backend, capture_output=True, text=True,
                                   timeout=GO_CHECK_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    if completed.returncode:
        raise ChainError("候选源码的 Go 加载器拒绝新的 Claude 目录：" + completed.stderr.strip()[-1200:])
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ChainError(f"Go 目录核对的输出不是 JSON：{error}") from error
    return {**value, "checker_sha256": _sha256(helper.read_bytes())}


def check_go_view(view: Mapping[str, Any], loaded: Mapping[str, Any], catalog_bytes: bytes) -> None:
    """Go 加载结果：目录摘要是新文件、validation_candidate 指向暂存 Release、production_active 与 Active 底稿相同。"""

    staged = loaded["staged"]
    entry, selector = staged["release_entry"], staged["validation_candidate_selector"]
    active = json.loads(loaded["active"].read_text(encoding="utf-8"))["selectors"]["production_active"]
    candidate_view, active_view = view.get("validation_candidate") or {}, view.get("production_active") or {}
    expected_candidate = {
        "version": entry["version"], "platform": entry["platform"], "release_sha256": entry["release_sha256"],
        "profile_sha256": entry["profile"]["sha256"], "wire_sha256": entry["wire"]["sha256"], "changeset": selector["changeset"],
    }
    if view.get("status") != "passed" or view.get("catalog_sha256") != _sha256(catalog_bytes):
        raise ChainError("Go 加载的 Claude 目录不是本次写入的 release-catalog.json")
    if {key: candidate_view.get(key) for key in expected_candidate} != expected_candidate:
        raise ChainError("Go 加载的 validation_candidate 不是暂存 Release")
    if (active_view.get("release_sha256"), active_view.get("changeset")) != (active["release_sha256"], active["changeset"]):
        raise ChainError("Go 加载的 production_active 与 VC-3 冻结的 Active 目录不同")


def assemble_catalog(loaded: Mapping[str, Any], repository: Path, lifecycle: PurePosixPath, *, go_check: Any = go_catalog_check) -> dict[str, Any]:
    """写 C 段并验收；任一项不过即把 release-catalog.json 还原成原字节。"""

    repository = repository.resolve(strict=True)
    catalog = _safe_target(repository, candidate.RELEASE_CATALOG_RELATIVE)
    original = catalog.read_bytes()
    if original != loaded["active"].read_bytes():
        raise ChainError("仓库当前的 release-catalog.json 不是 VC-3 冻结的 Active 目录（A 段之后不得改目录）")
    data = expected_catalog(loaded)
    catalog.write_bytes(data)
    try:
        view = go_check(repository)
        check_go_view(view, loaded, data)
        verified = candidate.verify_candidate_catalog(
            loaded["root"], loaded["manifest"], loaded["stage"], repository, repository.joinpath(*lifecycle.parts, CATALOG_STAGE_DIRNAME)
        )
    except BaseException:
        catalog.write_bytes(original)
        raise
    return {
        "release_catalog_sha256": verified["binding"]["release_catalog_sha256"],
        "catalog_tree_sha256": verified["binding"]["catalog_tree_sha256"],
        "release_sha256": str(verified["entry"]["release_sha256"]),
        "go_check": {key: view.get(key) for key in ("status", "catalog_sha256", "checker_sha256")},
    }


# ---------------------------------------------------------------------------
# D 段：承接收据
# ---------------------------------------------------------------------------


def check_transition(receipt_path: Path, repository: Path, commit: str) -> dict[str, Any]:
    receipt = json.loads(_plain_file(receipt_path, "承接收据").read_text(encoding="utf-8"))
    if (
        receipt.get("result") != FREEZE_RESULT_PASSED
        or receipt.get("unregistered_path_count") != 0
        or receipt.get("required_manual_actions") != []
        or receipt.get("deleted_frozen_paths") != []
    ):
        raise ChainError("承接收据有未登记路径、人工动作、冻结删除或不是通过结论")
    paths = [row.get("path") for row in receipt.get("transitions") or []]
    if paths != [candidate.RELEASE_CATALOG_RELATIVE.as_posix()]:
        raise ChainError(f"承接收据的摘要边应恰好只有 release-catalog.json，实际 {paths}")
    candidate._validate_candidate_source_transition(repository.resolve(strict=True), receipt, git_commit=commit)
    return {"base_commit": receipt["base_commit"], "current_commit": receipt["current_commit"], "identity_sha256": receipt["identity_sha256"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude 候选源码链的取料核验与装配（B3-04a）")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("assets", "catalog"):
        command = sub.add_parser(name)
        command.add_argument("--take", type=Path, required=True)
        command.add_argument("--repository", type=Path, required=True)
        command.add_argument("--lifecycle", required=True)
        command.add_argument("--campaign-id", required=True)
        command.add_argument("--candidate-id", required=True)
        command.add_argument("--target-version", required=True)
        if name == "assets":
            command.add_argument("--mapping", type=Path, required=True)
    transition = sub.add_parser("transition")
    transition.add_argument("--receipt", type=Path, required=True)
    transition.add_argument("--repository", type=Path, required=True)
    transition.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "transition":
            result = check_transition(args.receipt, args.repository, args.commit)
        else:
            loaded = load_take(args.take, campaign_id=args.campaign_id, candidate_id=args.candidate_id, target_version=args.target_version)
            lifecycle = PurePosixPath(args.lifecycle)
            if args.action == "assets":
                result = assemble_assets(loaded, args.repository, lifecycle, _plain_file(args.mapping, "门禁映射").read_bytes())
            else:
                result = assemble_catalog(loaded, args.repository, lifecycle)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"Claude 候选链拒绝：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
