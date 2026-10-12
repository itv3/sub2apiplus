"""在配置冻结前取得、复制并双重放 Claude 门禁第二根的真实原件。

只创建新的私有 import 目录，O_EXCL、逐件 fsync，最后写一次 proof。
失败目录不授予任何后继权；已冻结交付与历史 world 不补叶子、不重签。
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes, parse_json_bytes
from tools.official_client_control.errors import ControlError
from tools.official_client_control.qualification_material_import import (IMPORT_SCHEMA, ACQUISITION_SCHEMA,
    material_worker_spec, _strict_source_closure, validate_import_shape, validate_qualification_material_import)
from .claude_gate_material_worker import _plain_file, run_gate_material_reader
from .claude_qualification_material_source import VerifiedQualificationSource, resolve_qualification_source, qualification_roots, _TOKEN


@dataclass(frozen=True)
class GateMaterialReadset:
    source: VerifiedQualificationSource
    roots: tuple[Mapping[str, Any], ...]
    files: tuple[Mapping[str, Any], ...]
    reader_readset: tuple[tuple[str, str], ...]
    strict_readset: tuple[tuple[str, str], ...]
    reader_accesses: tuple[Mapping[str, Any], ...]
    replay: Mapping[str, Any] | None
    token: object


@dataclass(frozen=True)
class MaterialCopyPlan:
    data_root: Path
    import_id: str
    sources: tuple[VerifiedQualificationSource, ...]
    material_sets: tuple[GateMaterialReadset, ...]
    roots: tuple[Mapping[str, Any], ...]
    files: tuple[Mapping[str, Any], ...]
    parents: tuple[Mapping[str, Any], ...]
    domains: tuple[Mapping[str, Any], ...]
    origin: Mapping[str, Any]
    reader_readset: tuple[Mapping[str, Any], ...]
    strict_readset: tuple[Mapping[str, Any], ...]
    reader_accesses: tuple[Mapping[str, Any], ...]
    token: object


def _coordinate(roots: list[dict[str, Any]], logical: str) -> tuple[str, str]:
    matches = [(row["root_id"], Path(logical).relative_to(Path(row["source_locator"])).as_posix())
               for row in roots if Path(logical).is_relative_to(Path(row["source_locator"]))]
    if len(matches) != 1:
        raise ControlError("原 reader 实际读集存在未知根或重叠根")
    return matches[0]


def collect_sealed_gate_materials(store: Any, source: VerifiedQualificationSource, parent_binding: Mapping[str, Any],
                                  *, role: str) -> GateMaterialReadset:
    """完整执行原 reader；严格闭包使用能力表实读，不从 JSON 授予新根。"""
    source = resolve_qualification_source(store, source.qualification, source, purpose="current_import")
    # floor 和 Store 有效身份须在计划/复制之前核，不能先留下导入原件
    # 再由返回前的验证器拒绝旧策略。旧形成期材料仍可纯只读消费。
    from tools.official_client_control.qualification_material_import import MATERIALS_POLICY
    from tools.official_client_control.receipts import effective_tool_identity
    actual_identity = source.intake.reader_identity.document["tool_identity"]
    if actual_identity.get("policy_version", 0) < MATERIALS_POLICY:
        raise ControlError("当前第二根准备要求策略至少12，写入前拒绝旧读取身份")
    if source.role == "candidate" and effective_tool_identity(store, store.load_campaign(source.qualification["campaign_id"])) != actual_identity:
        raise ControlError("材料准备 reader 与 Store 当前有效七项工具身份不同，写入前拒绝")
    if role != source.role or dict(parent_binding) != source.parent_binding:
        raise ControlError("门禁导入角色或父绑定不是已验来源")
    roots, files, missing = [], [], []
    for capability in source.intake.roots:
        if capability.token is not _TOKEN:
            raise ControlError("门禁根不是来源取得接口返回的能力")
        roots.append({"root_id": capability.root_id, "source_locator": str(capability.logical_root),
            "source_reference_id": capability.reference_id, "source_reference_sha256": capability.reference_sha256,
            "root_kind": capability.root_kind, "archive_root_relative": "pending"})
        missing.extend(str(capability.logical_root / relative) for relative in capability.missing_relative_paths)
        for relative in capability.allowed_relative_paths:
            raw, metadata = _plain_file(capability.physical_root / relative)
            files.append({"root_id": capability.root_id, "source_relative_path": relative,
                "content_sha256": sha256_bytes(raw), "content_bytes": len(raw), "source_mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                "reader_role": "strict", "archive_binding": {"path": relative, "sha256": sha256_bytes(raw), "bytes": len(raw)}})
    if any(Path(left["source_locator"]).is_relative_to(Path(right["source_locator"])) for left in roots for right in roots if left is not right):
        raise ControlError("门禁取得的来源根不能相互嵌套")
    identity = source.intake.reader_identity
    reader_paths = set()
    accesses = []
    replay = None
    metadata_paths = set()
    if source.gate_root_id is not None:
        gate_root = next(row for row in roots if row["root_id"] == source.gate_root_id)
        specs = material_worker_spec(identity, roots, files, [])
        result = run_gate_material_reader(identity, specs[0], specs[1], sorted(set([*specs[2], *missing])),
            gate_root["source_locator"], source.receipt_relative, view="original", expected_subject=source.expected_subject)
        replay = result["result"]
        reader_paths = {_coordinate(roots, path) for path in result["reader_readset"]}
        metadata_paths = {_coordinate(roots, path) for path in result["metadata_files"]}
        accesses = result["reader_accesses"]
    strict_paths = set(_strict_source_closure(roots, files, sorted(reader_paths | metadata_paths),
        lambda row: _plain_file(Path(next(root["source_locator"] for root in roots if root["root_id"] == row["root_id"])) / row["source_relative_path"])[0]))
    selected = []
    for row in sorted(files, key=lambda value: (value["root_id"], value["source_relative_path"])):
        coordinate = (row["root_id"], row["source_relative_path"])
        if coordinate in reader_paths | strict_paths:
            selected.append({**row, "reader_role": "reader_and_strict" if coordinate in reader_paths and coordinate in strict_paths else "reader" if coordinate in reader_paths else "strict"})
    return GateMaterialReadset(source, tuple(roots), tuple(selected), tuple(sorted(reader_paths)),
                               tuple(sorted(strict_paths)), tuple(accesses), replay, _TOKEN)


def plan_qualification_material_import(sources: list[VerifiedQualificationSource], material_sets: list[GateMaterialReadset],
                                       *, managed_data_root: Path) -> MaterialCopyPlan:
    """纯计算确定性的一一复制计划；此时不会创建任何文件。"""
    if sorted(source.role for source in sources) != ["candidate", "rollback"] or len(material_sets) != 2:
        raise ControlError("门禁导入计划必须包含两份精确资格来源")
    data_root = Path(managed_data_root)
    if not data_root.is_absolute() or data_root.is_symlink() or not data_root.is_dir() or stat.S_IMODE(data_root.stat().st_mode) != 0o700:
        raise ControlError("门禁导入目标必须是实际私有受管数据根")
    source_index = {source.role: source for source in sources}
    roots, files, parents, domains = {}, {}, [], []
    reads, strict, accesses = set(), set(), {}
    for materials in material_sets:
        source = source_index.get(materials.source.role)
        if materials.token is not _TOKEN or source is None or materials.source is not source or source.token is not _TOKEN or source.intake.data_root != data_root:
            raise ControlError("门禁读集不属于对应受管来源或目标数据根")
        for row in materials.roots:
            previous = roots.setdefault(row["root_id"], dict(row))
            if previous != dict(row):
                raise ControlError("两份资格来源根映射冲突")
        for row in materials.files:
            previous = files.setdefault((row["root_id"], row["source_relative_path"]), dict(row))
            if previous != dict(row):
                raise ControlError("两份资格来源对同一文件声明不同字节")
        parents.append({"role": source.role, "qualification_roots": qualification_roots(source.qualification),
            "sealed_result_binding": dict(source.parent_binding), "parent_schema": source.parent["schema_version"],
            "parent_semantic_sha256": canonical_sha256(source.parent)})
        if source.gate_root_id is not None:
            domains.append({"role": source.role, "parent_binding": dict(source.parent_binding), "parent_schema": source.parent["schema_version"],
                "domain_pointer": "/external_gate", "domain_sha256": canonical_sha256(source.parent["external_gate"]),
                "source_root_id": source.gate_root_id, "receipt_relative_path": source.receipt_relative,
                "gate_subject_sha256": canonical_sha256(source.expected_subject)})
        reads.update(materials.reader_readset)
        strict.update(materials.strict_readset)
        for row in materials.reader_accesses:
            accesses[canonical_sha256(row)] = dict(row)
    reader = sources[0].intake.reader_identity.document
    from tools.official_client_control.qualification_material_import import MATERIALS_POLICY
    if reader["tool_identity"].get("policy_version", 0) < MATERIALS_POLICY:
        raise ControlError("复制计划要求策略至少12，禁止旧读取身份授予新写入权限")
    if any(source.intake.reader_identity.document != reader or source.intake.authority != sources[0].intake.authority or source.intake.source_scope != sources[0].intake.source_scope for source in sources):
        raise ControlError("一次导入必须使用同一实际受管 reader 和相同来源用途")
    parents.sort(key=lambda row: row["role"])
    domains.sort(key=lambda row: row["role"])
    source_material = [{key: value for key, value in files[coordinate].items() if key != "archive_binding"} for coordinate in sorted(files)]
    origin = {"authority": sources[0].intake.authority, "source_scope": sources[0].intake.source_scope,
        "deployment_sha256": canonical_sha256([source.intake.provenance.get("original_descriptors", {}).get("deployment", source.intake.provenance.get("deployment_binding")) for source in sorted(sources, key=lambda value: value.role)]),
        "source_instance_id": canonical_sha256([source.intake.provenance for source in sorted(sources, key=lambda value: value.role)]),
        "source_material_sha256": canonical_sha256(source_material), "formed_under": [source.formed_context for source in sorted(sources, key=lambda value: value.role)]}
    import_id = canonical_sha256({"source_origin": origin, "parents": parents, "reader_identity_sha256": reader["identity_sha256"], "source_files": source_material})
    for row in roots.values():
        row["archive_root_relative"] = f"control/qualification-imports/{import_id}/roots/{row['root_id']}/files"
    for coordinate, row in files.items():
        row["archive_binding"] = {"path": roots[row["root_id"]]["archive_root_relative"] + "/" + row["source_relative_path"],
            "sha256": row["content_sha256"], "bytes": row["content_bytes"]}
    def coordinate_rows(values):
        return tuple({"root_id": root_id, "source_relative_path": relative} for root_id, relative in sorted(values))
    return MaterialCopyPlan(data_root, import_id, tuple(sorted(sources, key=lambda value: value.role)), tuple(material_sets),
        tuple(roots[key] for key in sorted(roots)), tuple(files[key] for key in sorted(files)), tuple(parents), tuple(domains), origin,
        coordinate_rows(reads), coordinate_rows(strict), tuple(sorted(accesses.values(), key=lambda row: (row["logical_path"], row["operation"]))), _TOKEN)


def _directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise ControlError("导入祖先不是私有普通目录")
    else:
        _directory(path.parent)
        path.mkdir(mode=0o700)


def _write_once(root: Path, path: Path, raw: bytes, mode: int = 0o600) -> dict[str, Any]:
    _directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        actual, metadata = _plain_file(path)
        if actual != raw or stat.S_IMODE(metadata.st_mode) != mode:
            raise ControlError("导入写入后的实读字节或模式不同")
    finally:
        if descriptor != -1:
            # fdopen 已接管并关闭描述符；这里只处理进入上下文前的失败。
            try:
                os.close(descriptor)
            except OSError:
                pass
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256_bytes(raw), "bytes": len(raw)}


def copy_qualification_materials(copy_plan: MaterialCopyPlan) -> dict[str, Any]:
    """在全新目录里复制；双 reader 相等后最后写 proof，不能补旧冻结目录。"""
    if not isinstance(copy_plan, MaterialCopyPlan) or copy_plan.token is not _TOKEN:
        raise ControlError("复制须使用纯计划器返回的受管计划")
    plan = copy_plan
    directory = plan.data_root / "control/qualification-imports" / plan.import_id
    if directory.exists() or directory.is_symlink():
        raise ControlError("导入目录已存在：只允许纯重验，不得补写已开始或已冻结世界")
    _directory(directory.parent)
    directory.mkdir(mode=0o700)
    root_index = {row["root_id"]: row for row in plan.roots}
    for row in plan.files:
        path = Path(root_index[row["root_id"]]["source_locator"]) / row["source_relative_path"]
        raw, metadata = _plain_file(path)
        if sha256_bytes(raw) != row["content_sha256"] or len(raw) != row["content_bytes"] or f"{stat.S_IMODE(metadata.st_mode):04o}" != row["source_mode"]:
            raise ControlError("复制前源字节或模式漂移")
        actual_binding = _write_once(plan.data_root, plan.data_root / row["archive_binding"]["path"], raw, int(row["source_mode"], 8))
        if actual_binding != row["archive_binding"]:
            raise ControlError("复制文件与计划的一一映射不同")
    # 确保原 reader 使用过的空目录元数据也有真实对应目录。
    for access in plan.reader_accesses:
        if access["type"] == "directory":
            for root in plan.roots:
                logical = Path(access["logical_path"])
                if logical.is_relative_to(Path(root["source_locator"])):
                    _directory(plan.data_root / root["archive_root_relative"] / logical.relative_to(Path(root["source_locator"])))
    reader = plan.sources[0].intake.reader_identity
    for material in plan.material_sets:
        source = material.source
        if source.gate_root_id is None:
            continue
        root = root_index[source.gate_root_id]
        specs = material_worker_spec(reader, list(plan.roots), list(plan.files), list(plan.reader_accesses), data_root=plan.data_root)
        replay = run_gate_material_reader(reader, *specs, root["source_locator"], source.receipt_relative, view="imported", expected_subject=source.expected_subject)
        expected_reads = sorted(str(Path(root_index[root_id]["source_locator"]) / relative) for root_id, relative in material.reader_readset)
        if replay["result"] != material.replay or replay["reader_readset"] != expected_reads or replay["reader_accesses"] != list(material.reader_accesses):
            raise ControlError("原/副本 reader 完整结果、字节读集或实际元数据访问不同")
    for row in plan.files:
        raw, metadata = _plain_file(Path(root_index[row["root_id"]]["source_locator"]) / row["source_relative_path"])
        if sha256_bytes(raw) != row["content_sha256"] or len(raw) != row["content_bytes"] or f"{stat.S_IMODE(metadata.st_mode):04o}" != row["source_mode"]:
            raise ControlError("复制/重放期间来源材料漂移")
    reader_binding = _write_once(plan.data_root, directory / "reader-identity.json", canonical_json_bytes(reader.document))
    acquisitions = []
    for source in plan.sources:
        roots = [{"root_id": capability.root_id, "root_kind": capability.root_kind, "source_locator": str(capability.logical_root),
            "allowed_relative_paths": list(capability.allowed_relative_paths), "missing_relative_paths": list(capability.missing_relative_paths), "source_reference_id": capability.reference_id,
            "source_reference_sha256": capability.reference_sha256} for capability in source.intake.roots]
        provenance_binding = _write_once(plan.data_root, directory / (source.role + "-source-context.json"), canonical_json_bytes(source.intake.provenance))
        original_bindings = {}
        for original in source.intake.source_originals:
            if original.name in original_bindings or not original.name.replace("_", "").isalnum():
                raise ControlError("来源原件名称冲突或非法")
            raw, _metadata = _plain_file(original.path)
            if raw != original.raw:
                raise ControlError("来源参数、驱动清单或安装原件在复制前漂移")
            original_bindings[original.name] = _write_once(plan.data_root,
                directory / "source-originals" / source.role / (original.name + ".raw"), raw)
        acquisition = {"schema_version": ACQUISITION_SCHEMA, "source_id": canonical_sha256({"role": source.role, "roots": roots, "qualification_roots": qualification_roots(source.qualification), "parent": source.parent_binding, "reader": reader.document["identity_sha256"]}),
            "authority": source.intake.authority, "source_scope": source.intake.source_scope, "client": "claude", "host_id": "ARM64",
            "source_deployment_binding": original_bindings.get("deployment", provenance_binding),
            "source_parameter_binding": original_bindings.get("parameter", provenance_binding),
            "source_install_binding": original_bindings.get("install", provenance_binding),
            "source_driver_manifest_binding": original_bindings.get("driver_manifest", provenance_binding),
            "source_context_binding": provenance_binding, "source_original_bindings": original_bindings,
            "source_layout_sha256": canonical_sha256(roots), "formation_context": source.formed_context,
            "qualification_roots": qualification_roots(source.qualification), "entry_references": dict(source.entry_references),
            "sealed_parent_binding": dict(source.parent_binding), "roots": roots, "reader_identity_binding": reader_binding}
        acquisition["acquisition_content_sha256"] = canonical_sha256(acquisition)
        acquisition_binding = _write_once(plan.data_root, directory / (source.role + "-acquisition.json"), canonical_json_bytes(acquisition))
        acquisitions.append({"role": source.role, "binding": acquisition_binding})
    document = {"schema_version": IMPORT_SCHEMA, "import_id": plan.import_id, "source_origin": dict(plan.origin),
        "reader_contract_sha256": reader.document["contract_sha256"], "reader_identity_sha256": reader.document["identity_sha256"],
        "parents": list(plan.parents), "domains": list(plan.domains), "roots": list(plan.roots), "files": list(plan.files),
        "reader_readset": list(plan.reader_readset), "strict_readset": list(plan.strict_readset),
        "source_acquisition_bindings": acquisitions, "reader_identity_binding": reader_binding,
        "reader_accesses": list(plan.reader_accesses)}
    for key in ("reader_readset", "strict_readset", "files", "reader_accesses"):
        document[key + "_sha256"] = canonical_sha256(document[key])
    document["identity_sha256"] = canonical_sha256(document)
    validate_import_shape(document)
    proof_binding = _write_once(plan.data_root, directory / "material-import.json", canonical_json_bytes(document))
    # 逐目录同步，proof 可见不等于可用；返回前完整纯校验才取得后继权限。
    for path in [*sorted((member for member in directory.rglob("*") if member.is_dir()), reverse=True), directory, directory.parent]:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return proof_binding


def prepare_qualification_material_import(store: Any, sources: list[VerifiedQualificationSource], *, managed_data_root: Path) -> dict[str, Any]:
    material_sets = [collect_sealed_gate_materials(store, source, source.parent_binding, role=source.role) for source in sources]
    plan = plan_qualification_material_import(sources, material_sets, managed_data_root=managed_data_root)
    directory = plan.data_root / "control/qualification-imports" / plan.import_id
    if directory.exists() or directory.is_symlink():
        # prepare 在 proof 成功后、配置 staged 前中断，可接着纯重验既有
        # 完整导入。残缺目录不补件，copy API 本身仍一律 O_EXCL 拒绝。
        if directory.is_symlink() or not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise ControlError("既有导入不是私有完整目录，不得接着写入")
        proof_path = directory / "material-import.json"
        try:
            raw, metadata = _plain_file(proof_path)
        except (OSError, RuntimeError) as error:
            raise ControlError("既有导入缺完整 proof，不得补写已开始的世界") from error
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ControlError("既有导入 proof 模式改变，不得接着写入")
        binding = {"path": proof_path.relative_to(plan.data_root).as_posix(), "sha256": sha256_bytes(raw), "bytes": len(raw)}
    else:
        binding = copy_qualification_materials(plan)
    from tools.official_client_control.delivery_execution import VerifiedSource
    deployment = plan.origin["deployment_sha256"]
    if sources[0].intake.source_scope == "formal":
        originals = [item for item in sources[0].intake.source_originals if item.name == "deployment"]
        if len(originals) != 1:
            raise ControlError("正式导入准备缺实际部署原件")
        deployment = sha256_bytes(originals[0].raw)
    source = VerifiedSource("0" * 64, deployment, {}, "0" * 64, plan.origin["source_instance_id"], plan.data_root)
    validate_qualification_material_import(store, binding, source, {item.role: item.qualification for item in sources}, purpose="current_import", expected_source_scope=sources[0].intake.source_scope)
    return binding


class _MaterialPreparationView(Mapping[str, bytes]):
    """只合并当前真实 intake 与固定 Store 原件；未知路径仍交由原来源闭集拒绝。"""
    def __init__(self, base: Mapping[str, bytes], originals: Mapping[str, bytes]):
        self.base = base
        self.originals = dict(originals)

    def __iter__(self):
        return iter(self.originals)

    def __len__(self):
        return len(self.originals)

    def __getitem__(self, path):
        value = self.get(path)
        if value is None:
            raise KeyError(path)
        return value

    def get(self, path, default=None):
        if path in self.originals:
            return self.originals[path]
        return self.base.get(path, default)


def prepare_material_successor(store: Any, sources: list[VerifiedQualificationSource], *, managed_data_root: Path,
                               plan_ref: Mapping[str, Any], source: Any,
                               control_deployment_binding: Mapping[str, Any], delivery_lock_fd: int) -> dict[str, Any]:
    """原 delivery 锁内先全验真新材料，再封存唯一关联；旧配置和旧导入不修改。"""
    from dataclasses import replace
    from tools.official_client_control import delivery_execution as execution
    from tools.official_client_control.receipts import delivery_receipts, delivery_chain_state, tool_evolutions
    from tools.official_client_control.qualification_material_import import (
        SUCCESSOR_SCHEMA, SUCCESSOR_ENTRY, _successor_state, _validate_material_successor,
        _store_binding, describe_material_successor_bindings, validate_material_successor,
    )
    from tools.official_client_control.contracts import CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS, OBJECT_SCHEMA
    from tools.official_client_control.canonical import validate_external_binding
    root = Path(managed_data_root)
    if not isinstance(source, execution.VerifiedSource) or source.trusted_data_root != root:
        raise ControlError("材料后继准备必须先取得原通道验真的同一实际来源")
    if plan_ref.get("object_kind") != "candidate_delivery_plan":
        raise ControlError("材料后继准备只接受实际原 Plan 引用")
    plan = store.load_object(dict(plan_ref))["payload"]
    campaign_root = root / "evidence/campaigns" / plan["campaign_id"]
    lock_path = campaign_root / "control/delivery/.delivery.lock"
    if type(delivery_lock_fd) is not int or delivery_lock_fd < 0:
        raise ControlError("材料后继准备要求原 delivery 锁的真实 FD")
    held = os.fstat(delivery_lock_fd)
    actual = lock_path.lstat()
    if not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.geteuid() or stat.S_IMODE(actual.st_mode) != 0o600 or (held.st_dev, held.st_ino) != (actual.st_dev, actual.st_ino):
        raise ControlError("材料后继锁 FD 不是本原交付的固定私有锁")
    try:
        fcntl.flock(delivery_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise ControlError("材料后继原交付锁正被其他执行持有") from error
    if delivery_receipts(store, plan["campaign_id"], dict(plan_ref)):
        raise ControlError("已有完成收据的 Plan 不能准备材料后继或重新组包")
    configuration_facts = execution.evidence_facts(store, plan["executor_configuration_ref"], "configuration")
    configuration = configuration_facts["configuration"]
    execution._revalidate_guard_continuity(source, required=configuration["source_scope"] == "formal")
    if sorted(item.role for item in sources) != ["candidate", "rollback"] or any(item.qualification != configuration[item.role]["qualification"] or item.intake.data_root != root or item.token is not _TOKEN for item in sources):
        raise ControlError("材料后继当前来源必须恰好是原配置的两角色资格")
    chain = tool_evolutions(store, store.load_campaign(plan["campaign_id"]))
    if not chain:
        raise ControlError("材料后继准备前必须先登记当前真实工具演进")
    state = delivery_chain_state(store, plan["campaign_id"], plan["delivery_id"])
    if not state.complete():
        raise ControlError("材料后继准备只允许原四 pass 已完成且无待消费失效")
    evolution_path = campaign_root / chain[-1][1]["evolution_receipt"]["path"]
    evolution_raw, _metadata = _plain_file(evolution_path)
    evolution_binding = {"path": evolution_path.relative_to(root).as_posix(), "sha256": sha256_bytes(evolution_raw), "bytes": len(evolution_raw)}
    if evolution_binding["sha256"] != chain[-1][1]["evolution_receipt"]["sha256"]:
        raise ControlError("材料后继准备登记引用的实际演进原件漂移")
    control_binding = validate_external_binding(dict(control_deployment_binding), "material.successor.control_deployment")
    execution._source_bytes(source, configuration_facts["binding"])
    control_raw, _metadata = _plain_file(root / control_binding["path"])
    if sha256_bytes(control_raw) != control_binding["sha256"] or len(control_raw) != control_binding["bytes"]:
        raise ControlError("材料后继准备的实际控制部署 binding 漂移")
    freeze = execution.evidence_facts(store, plan["environment_freeze_ref"], "freeze")
    facts = {"schema_version": SUCCESSOR_SCHEMA, "client": "claude", "campaign_id": plan["campaign_id"],
        "candidate_id": plan["candidate_id"], "delivery_id": plan["delivery_id"], "plan_ref": dict(plan_ref),
        "executor_configuration_ref": plan["executor_configuration_ref"], "configuration_binding": configuration_facts["binding"],
        "configuration_sha256": plan["configuration_sha256"], "predecessor_materials_binding": configuration["qualification_materials_binding"],
        "successor_materials_binding": configuration["qualification_materials_binding"],
        "qualification_roots_by_role": {role: qualification_roots(configuration[role]["qualification"]) for role in ("candidate", "rollback")},
        "sealed_parent_bindings_by_role": {role: configuration[role]["qualification"]["sealed_result_binding"] for role in ("candidate", "rollback")},
        "delivery_fact_refs": state.valid_refs(store), "environment": {key: plan[key] for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS},
        "tool_evolution_registration_ref": chain[-1][0], "evolution_receipt_binding": evolution_binding,
        "control_deployment_binding": control_binding, "guard_identity": freeze["guard_identity"],
        "execution_context": {"campaign_id": plan["campaign_id"], "candidate_id": plan["candidate_id"], "delivery_id": plan["delivery_id"], "data_root": str(root), "configuration_ref": configuration_facts["binding"]}}
    _successor_state(store, configuration, facts, source)
    # 独立新 import 的既有 API 始终真实校验当前来源；残缺目录仍拒绝补叶子。
    successor_binding = prepare_qualification_material_import(store, sources, managed_data_root=root)
    if successor_binding["path"] == configuration["qualification_materials_binding"]["path"]:
        raise ControlError("材料后继准备未取得独立新 import ID，不能占用旧坐标")
    facts["successor_materials_binding"] = successor_binding
    import_source = execution.VerifiedSource("0" * 64, control_binding["sha256"], {}, source.configuration_sha256,
        source.service_instance_id, root)
    imported = validate_qualification_material_import(store, successor_binding, import_source,
        {item.role: item.qualification for item in sources}, purpose="current_import", expected_source_scope=configuration["source_scope"])
    originals = {row["path"]: execution._source_bytes(import_source, row) for row in imported.archive_bindings}
    originals[control_binding["path"]] = control_raw
    originals[evolution_binding["path"]] = evolution_raw
    for reference, registration in chain:
        binding = _store_binding(store, root, reference)
        originals[binding["path"]] = _plain_file(root / binding["path"])[0]
        path = campaign_root / registration["evolution_receipt"]["path"]
        originals[path.relative_to(root).as_posix()] = _plain_file(path)[0]
    manifest_path = campaign_root / "campaign.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        originals[manifest_path.relative_to(root).as_posix()] = _plain_file(manifest_path)[0]
    prepared_source = replace(source, bytes_by_path=_MaterialPreparationView(source.bytes_by_path, originals), gate_projection=None)
    existing = describe_material_successor_bindings(store, configuration, plan_ref=plan_ref)
    payload = {"schema_version": "official-client-operational-evidence/v1", "persona": plan["persona"],
        "manifest_id": canonical_sha256(facts), "entries": [{"id": SUCCESSOR_ENTRY, "facts": facts}]}
    object_document = {"schema_version": OBJECT_SCHEMA, "object_kind": "operational_evidence", "payload": payload}
    object_raw = canonical_json_bytes(object_document)
    reference = {"object_kind": "operational_evidence", "sha256": sha256_bytes(object_raw)}
    if existing is not None:
        if existing["reference"] != reference or store.load_object(reference) != object_document:
            raise ControlError("同一原 Plan 与有效登记已有不同完整材料关联，不能修补或分叉")
    else:
        _validate_material_successor(store, reference, configuration, prepared_source, purpose="current_import", plan_ref=plan_ref, pending_payload=payload)
        if delivery_receipts(store, plan["campaign_id"], dict(plan_ref)):
            raise ControlError("材料后继封存前原 Plan 已有完成收据，拒绝新关联")
        sealed = store.seal_object("operational_evidence", payload)
        if sealed != reference:
            raise ControlError("材料后继封存引用不是完整预检的同一原字节")
    association_path = store.object_path(reference).relative_to(root).as_posix()
    originals[association_path] = object_raw
    prepared_source = replace(source, bytes_by_path=_MaterialPreparationView(source.bytes_by_path, originals), gate_projection=None)
    projection = validate_material_successor(store, reference, configuration, prepared_source, purpose="current_import", plan_ref=plan_ref)
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import Arm64DeliveryExecutor, _ProducerOriginals
    from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
    continuity = execution._revalidate_guard_continuity(prepared_source, required=configuration["source_scope"] == "formal")
    initial = [item.as_value() for item in continuity.read_bindings] if continuity is not None else []
    profile = source.profile_projection
    if profile is not None:
        initial.extend(profile.material_bindings())
        initial.extend(profile.proof_bindings)
    # 独立新关联已全验真，才解除 bootstrap 的历史父边界。只扩展真实
    # producer owner 的显式 add 清单；一般 Mapping 仍须已含完整原件。
    if isinstance(source.bytes_by_path, _ProducerOriginals):
        Arm64DeliveryExecutor._expand_producer_originals(source.bytes_by_path, initial, projection, profile)
    project_delivery_raw_bindings(prepared_source, initial, gate_projection=projection, profile_projection=profile)
    if isinstance(source.bytes_by_path, _ProducerOriginals):
        source.bytes_by_path.revalidate()
    return {"successor_ref": reference, "successor_materials_binding": successor_binding, "projection": projection}
