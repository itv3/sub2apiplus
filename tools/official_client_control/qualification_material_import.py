"""门禁材料导入/acquisition 的纯闭集验证和真实复制视图重放。

输入 source 须先经既有通道或受管 guard 本地匹配验真。本层只消费
明确资格四根及导入原件，不能取得上层 Plan/Package/Receipt 权限。
"""

from __future__ import annotations

import importlib.util
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
import stat
import threading
from typing import Any, Callable, Mapping

from .canonical import canonical_sha256, expect_exact_keys, parse_json_bytes, sha256_bytes, validate_external_binding, validate_relative_path
from .errors import ControlError


IMPORT_SCHEMA = "claude-qualification-material-import/v1"
ACQUISITION_SCHEMA = "claude-qualification-source-acquisition/v1"
MATERIALS_POLICY = 12
IMPORT_FIELDS = {"schema_version", "import_id", "source_origin", "reader_contract_sha256", "reader_identity_sha256", "parents", "domains", "roots", "files", "reader_readset", "strict_readset", "reader_readset_sha256", "strict_readset_sha256", "files_sha256", "identity_sha256", "source_acquisition_bindings", "reader_identity_binding", "reader_accesses", "reader_accesses_sha256"}
PARENT_FIELDS = {"role", "qualification_roots", "sealed_result_binding", "parent_schema", "parent_semantic_sha256"}
DOMAIN_FIELDS = {"role", "parent_binding", "parent_schema", "domain_pointer", "domain_sha256", "source_root_id", "receipt_relative_path", "gate_subject_sha256"}
ROOT_FIELDS = {"root_id", "source_locator", "source_reference_id", "source_reference_sha256", "root_kind", "archive_root_relative"}
FILE_FIELDS = {"root_id", "source_relative_path", "content_sha256", "content_bytes", "source_mode", "reader_role", "archive_binding"}
ACQUISITION_FIELDS = {"schema_version", "source_id", "authority", "source_scope", "client", "host_id", "source_deployment_binding", "source_parameter_binding", "source_install_binding", "source_driver_manifest_binding", "source_context_binding", "source_original_bindings", "source_layout_sha256", "formation_context", "qualification_roots", "entry_references", "sealed_parent_binding", "roots", "reader_identity_binding", "acquisition_content_sha256"}
ORIGIN_FIELDS = {"authority", "source_scope", "deployment_sha256", "source_instance_id", "source_material_sha256", "formed_under"}


def requires_qualification_materials(store: Any, campaign: Mapping[str, Any], *, formed_identity: Mapping[str, Any] | None = None) -> bool:
    if campaign.get("client") != "claude":
        return False
    from .receipts import effective_tool_identity
    identity = dict(formed_identity) if formed_identity is not None else effective_tool_identity(store, dict(campaign))
    return isinstance(identity, dict) and identity.get("policy_version", 0) >= MATERIALS_POLICY


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlError(label + "必须是对象")
    expect_exact_keys(value, fields, label)
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ControlError(label + "必须是数组")
    return value


def _coordinate(row: Mapping[str, Any]) -> tuple[str, str]:
    _exact(dict(row), {"root_id", "source_relative_path"}, "material.coordinate")
    return str(row["root_id"]), validate_relative_path(row["source_relative_path"], "material.source_relative_path")


def validate_import_shape(value: Any) -> dict[str, Any]:
    document = _exact(value, IMPORT_FIELDS, "material.import")
    if document["schema_version"] != IMPORT_SCHEMA:
        raise ControlError("门禁导入 schema 不受支持")
    _exact(document["source_origin"], ORIGIN_FIELDS, "material.source_origin")
    if document["source_origin"]["authority"] not in {"claude-arm64-qualified-intake", "claude-regression-qualified-intake"} or document["source_origin"]["source_scope"] not in {"formal", "regression_fixture"}:
        raise ControlError("门禁导入来源用途或 authority 非法")
    for key, fields in (("parents", PARENT_FIELDS), ("domains", DOMAIN_FIELDS), ("roots", ROOT_FIELDS), ("files", FILE_FIELDS)):
        for row in _array(document[key], "material." + key):
            _exact(row, fields, "material." + key)
    if sorted(row["role"] for row in document["parents"]) != ["candidate", "rollback"]:
        raise ControlError("门禁导入父必须恰好是两个精确资格角色")
    if len(document["roots"]) != len({row["root_id"] for row in document["roots"]}):
        raise ControlError("门禁导入根 ID 重复")
    roots = {row["root_id"]: row for row in document["roots"]}
    for row in roots.values():
        expected = f"control/qualification-imports/{document['import_id']}/roots/{row['root_id']}/files"
        if row["root_kind"] not in {"gate_evidence", "unit_records", "unit_sources", "read_audit_materials"} or row["archive_root_relative"] != expected or not Path(row["source_locator"]).is_absolute() or ".." in Path(row["source_locator"]).parts:
            raise ControlError("门禁导入根映射越界或类型错误")
    coordinates = []
    archive_paths = []
    for row in document["files"]:
        coordinate = (row["root_id"], validate_relative_path(row["source_relative_path"], "material.source_relative_path"))
        if row["root_id"] not in roots or row["reader_role"] not in {"reader", "strict", "reader_and_strict"}:
            raise ControlError("门禁复制文件根或读取角色错误")
        binding = validate_external_binding(row["archive_binding"], "material.archive_binding")
        if binding["path"] != roots[row["root_id"]]["archive_root_relative"] + "/" + coordinate[1] or binding["sha256"] != row["content_sha256"] or binding["bytes"] != row["content_bytes"] or row["source_mode"] not in {"0400", "0600"}:
            raise ControlError("门禁复制文件不符合一一根映射、字节或私有模式")
        coordinates.append(coordinate)
        archive_paths.append(binding["path"])
    if coordinates != sorted(set(coordinates)) or len(archive_paths) != len(set(archive_paths)):
        raise ControlError("门禁复制映射存在重复、冲突或未排序")
    reader = [_coordinate(row) for row in _array(document["reader_readset"], "material.reader_readset")]
    strict = [_coordinate(row) for row in _array(document["strict_readset"], "material.strict_readset")]
    if reader != sorted(set(reader)) or strict != sorted(set(strict)) or set(coordinates) != set(reader) | set(strict):
        raise ControlError("门禁复制映射不恰好覆盖完整读集并集")
    for key in ("reader_readset", "strict_readset", "files", "reader_accesses"):
        if document[key + "_sha256"] != canonical_sha256(document[key]):
            raise ControlError("门禁导入读集、访问或文件清单摘要漂移")
    if document["identity_sha256"] != canonical_sha256({key: value for key, value in document.items() if key != "identity_sha256"}):
        raise ControlError("门禁导入证明内容摘要漂移")
    validate_external_binding(document["reader_identity_binding"], "material.reader_identity_binding")
    acquisitions = _array(document["source_acquisition_bindings"], "material.source_acquisition_bindings")
    if len(acquisitions) != 2:
        raise ControlError("门禁导入必须绑定两个真实 acquisition 原件")
    for row in acquisitions:
        _exact(row, {"role", "binding"}, "material.source_acquisition")
        validate_external_binding(row["binding"], "material.acquisition_binding")
    if sorted(row["role"] for row in acquisitions) != ["candidate", "rollback"]:
        raise ControlError("门禁 acquisition 角色闭集错误")
    return document


def _read_bound(source: Any, binding: Mapping[str, Any]) -> bytes:
    from .delivery_execution import _source_bytes
    return _source_bytes(source, binding)


def _subject_artifact_requirements(store: Any, data_root: Path, qualification: Mapping[str, Any],
                                   bindings: list[dict[str, Any]], projections: list[Any],
                                   read_parent: Callable[[Mapping[str, Any]], bytes]) -> dict[str, dict[str, Any]]:
    """只将已验 subject 的严格构建制品要求转成数据根内的精确绑定。

    活父投影先核原件、元数据域和坐标；既有库存合同仍负责判定严格三制品，
    不按文件名或嵌套 schema 放宽权限。返回项必须同时在父投影和真实读集中。
    """
    from tools.official_client_capture.claude_qualification_material_source import VerifiedQualificationSubjectProjection
    from .qualification_bindings import QualificationBindingProjection

    known = {}
    for binding in bindings:
        row = dict(validate_external_binding(binding, "subject.artifact_read_binding"))
        if known.setdefault(row["path"], row) != row:
            raise ControlError("subject 制品读集对同一路径绑定不同字节")
    external_relative = qualification["external_root_relative"]
    base = "." if external_relative == "." else validate_relative_path(external_relative, "subject.artifact_root")
    requirements = {}
    for projection in projections:
        if type(projection) is not VerifiedQualificationSubjectProjection:
            raise ControlError("subject 制品要求缺原 reader 的准确父投影类型")
        parent = dict(validate_external_binding(dict(projection.parent_binding), "subject.artifact_parent"))
        reference = {"receipt_kind": "candidate_build", "sha256": parent["sha256"]}
        # 只有同一 Store 的内容寻址构建父进入库存合同；其它 subject 父不授模式。
        if data_root / parent["path"] != store.receipt_path(reference):
            continue
        if known.get(parent["path"]) != parent or projection.base_relative != base:
            raise ControlError("subject 制品父原件或外部坐标根与实际读集不同")
        raw = read_parent(parent)
        projected = projection.project(parent, raw)
        if projected is None:
            raise ControlError("subject 制品父投影没有承接同一原件")
        contract = QualificationBindingProjection(store)
        contract.bindings(parse_json_bytes(raw), reference)
        manifest = contract.manifest()
        domains = [row for row in manifest["parent_domains"] if row["parent_ref"] == reference
            and row["domain_kind"] == "candidate_build_parameters"]
        if (len(domains) != 1 or domains[0]["parent_sha256"] != parent["sha256"]
                or domains[0]["parent_bytes"] != parent["bytes"]
                or (("build_identity", "parameters"), domains[0]["domain_sha256"]) not in projection.metadata_domains):
            raise ControlError("subject 制品要求与已验构建父域不同")
        projected_bindings = {row["path"]: row for row in projected}
        for requirement in manifest["artifact_file_requirements"]:
            if requirement["parent_ref"] != reference or requirement["required_mode"] != "0755":
                raise ControlError("subject 制品要求不是同一构建父的严格 0755 合同")
            row = dict(validate_external_binding({"path": (Path(base) / requirement["relative_path"]).as_posix(),
                "sha256": requirement["content_sha256"], "bytes": requirement["content_bytes"]}, "subject.artifact_binding"))
            if known.get(row["path"]) != row or projected_bindings.get(row["path"]) != row:
                raise ControlError("subject 严格制品不在已验父投影及真实绑定闭集")
            if requirements.setdefault(row["path"], row) != row:
                raise ControlError("subject 严格制品同路径要求不同字节")
    return requirements


def material_worker_spec(identity: Any, roots: list[dict[str, Any]], files: list[dict[str, Any]],
                          accesses: list[dict[str, Any]], *, data_root: Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """精确材料及实际目录元数据映射；代码/运行库只用身份固定清单。"""
    root_index = {row["root_id"]: row for row in roots}
    output = []
    directories = {}
    missing = []
    for root in roots:
        logical = root["source_locator"]
        physical = str(data_root / root["archive_root_relative"]) if data_root is not None else logical
        directories[logical] = {"logical_path": logical, "physical_path": physical}
    for row in files:
        root = root_index[row["root_id"]]
        logical = str(Path(root["source_locator"]) / row["source_relative_path"])
        physical = str(data_root / row["archive_binding"]["path"]) if data_root is not None else logical
        output.append({"logical_path": logical, "physical_path": physical, "sha256": row["content_sha256"],
                       "bytes": row["content_bytes"], "mode": row["source_mode"]})
        for parent in Path(logical).parents:
            if parent == Path(root["source_locator"]) or parent.is_relative_to(Path(root["source_locator"])):
                relative = parent.relative_to(Path(root["source_locator"]))
                physical_parent = Path(physical).parent
                physical_parent = (data_root / root["archive_root_relative"] / relative) if data_root is not None else parent
                directories[str(parent)] = {"logical_path": str(parent), "physical_path": str(physical_parent)}
            else:
                directories.setdefault(str(parent), {"logical_path": str(parent), "physical_path": str(parent)})
    for row in accesses:
        if row["type"] == "missing":
            missing.append(row["logical_path"])
        elif row["type"] == "directory":
            logical = Path(row["logical_path"])
            for root in roots:
                source = Path(root["source_locator"])
                if logical.is_relative_to(source):
                    physical = str(data_root / root["archive_root_relative"] / logical.relative_to(source)) if data_root is not None else str(logical)
                    directories[str(logical)] = {"logical_path": str(logical), "physical_path": physical}
    code = [*identity.document["code_bindings"], *identity.document["target_bindings"], *identity.document["runtime_manifest"]["files"]]
    from tools.official_client_capture.claude_gate_material_worker import reader_bytecode_probe
    for row in code:
        for parent in Path(row["absolute_path"]).parents:
            directories.setdefault(str(parent), {"logical_path": str(parent), "physical_path": str(parent)})
        if row["absolute_path"].endswith(".py"):
            cached = reader_bytecode_probe(identity.document, row["absolute_path"])
            if not Path(cached).exists():
                missing.append(cached)
    return output, list(directories.values()), sorted(set(missing))


def _strict_source_closure(roots: list[dict[str, Any]], files: list[dict[str, Any]], initial: list[tuple[str, str]], read_file: Any) -> list[tuple[str, str]]:
    """未知 JSON 的标准绑定也实读递归；循环及缺映射均失败。"""
    from .delivery_execution import typed_bindings
    root_index = {row["root_id"]: row for row in roots}
    file_index = {(row["root_id"], row["source_relative_path"]): row for row in files}
    result = set()
    visiting = set()

    def visit(coordinate):
        if coordinate in visiting:
            raise ControlError("门禁未知 JSON 的原件绑定形成循环")
        if coordinate in result:
            return
        row = file_index.get(coordinate)
        if row is None:
            raise ControlError("门禁严格闭包缺真实文件映射")
        raw = read_file(row)
        if sha256_bytes(raw) != row["content_sha256"] or len(raw) != row["content_bytes"]:
            raise ControlError("门禁严格闭包原件字节漂移")
        visiting.add(coordinate)
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            document = None
        for binding in typed_bindings(document):
            target = (coordinate[0], binding["path"])
            other = file_index.get(target)
            if other is None or other["content_sha256"] != binding["sha256"] or other["content_bytes"] != binding["bytes"]:
                raise ControlError("未知 JSON 的标准下层绑定缺件、跨根或漂移")
            visit(target)
        visiting.remove(coordinate)
        result.add(coordinate)

    for coordinate in initial:
        visit(coordinate)
    return sorted(result)


def _validate_intake_originals(store: Any, source: Any, acquisition: Mapping[str, Any], provenance: Mapping[str, Any],
                               qualification: Mapping[str, Any], *, purpose: str) -> None:
    """正式首用重建安装和批准合同；形成期只消费冻结原字节，不回读源门禁。"""
    from tools.official_client_capture.claude_qualification_material_source import FORMAL_AUTHORITY, FIXTURE_AUTHORITY
    if acquisition["source_scope"] == "regression_fixture":
        if acquisition["authority"] != FIXTURE_AUTHORITY or provenance.get("fixture_contract") not in {"qualified-gate-fixture/v1", "explicit-no-gate-rollback/v1"}:
            raise ControlError("回归来源必须是明确的本机夹具合同，不能改名成正式来源")
        return
    if acquisition["authority"] != FORMAL_AUTHORITY:
        raise ControlError("正式来源 authority 与来源用途不同")
    from tools.official_client_capture.claude_qualification_material_source import managed_driver_module
    install = managed_driver_module("install", current=purpose != "formed_replay")
    parse_env = managed_driver_module("parse_env", current=purpose != "formed_replay")
    admission = managed_driver_module("vc5_admission", current=purpose != "formed_replay")
    originals = acquisition["source_original_bindings"]
    required = {"parameter", "admission", "effective_parameters", "admission_event", "driver_manifest", "install", "deployment", "public_context", "intake_approval"}
    _exact(originals, required, "material.formal_source_originals")
    descriptors = _exact(provenance.get("original_descriptors"), required, "material.formal_source_descriptors")
    raw = {name: _read_bound(source, originals[name]) for name in required}
    manifest = install.validate_manifest(parse_json_bytes(raw["driver_manifest"]))
    for module, relative in ((install, "install.py"), (parse_env, "driver/parse_env.py"), (admission, "driver/vc5_admission.py")):
        entries = [row for row in manifest["files"] if row["path"] == relative]
        if len(entries) != 1 or entries[0]["sha256"] != getattr(module, "_qualification_loaded_source_sha256", None):
            raise ControlError("正式来源纯合同代码不是冻结驱动清单绑定的原实现，禁止换实现重放")
    context = parse_json_bytes(raw["public_context"])
    if context != provenance.get("public_context") or context.get("schema_version") != "claude-qualification-intake-context/v1" or context.get("data_root") != str(source.trusted_data_root) or context.get("client") != "claude" or context.get("host_id") != "ARM64" or context.get("campaign_id") != qualification["campaign_id"] or context.get("candidate_id") != qualification["candidate_id"] or context.get("qualification_roots") != acquisition["qualification_roots"] or context.get("entry_references") != acquisition["entry_references"] or context.get("parameter_descriptor") != descriptors["parameter"]:
        raise ControlError("正式来源上下文不是对应资格、参数及已验受管根")
    if acquisition["formation_context"] != {"candidate_revision": context["candidate_revision"], "evaluation_baseline": context["evaluation_baseline"], "qualification_roots": context["qualification_roots"], "store_formed_under": store.load_fact(qualification["candidate_ref"])["formed_under"]}:
        raise ControlError("正式来源形成期不是 Store 原事实的对应冻结上下文")
    context_id = canonical_sha256(context)
    context_binding = {"path": f"control/qualification-intake/{context_id}/context.json", "sha256": descriptors["public_context"]["sha256"], "bytes": descriptors["public_context"]["bytes"]}
    if descriptors["public_context"]["absolute_path"] != str(source.trusted_data_root / context_binding["path"]):
        raise ControlError("正式来源上下文原坐标不是固定受管位置")
    approval = parse_json_bytes(raw["intake_approval"])
    _exact(approval, {"schema_version", "context_binding", "context_sha256", "scope", "approved_by"}, "material.intake_approval")
    if approval["schema_version"] != "claude-qualification-intake-approval/v1" or approval["context_binding"] != context_binding or approval["context_sha256"] != context_id or approval["scope"] != "read-copy-exact-qualified-layout/v1" or not isinstance(approval["approved_by"], str) or not approval["approved_by"].strip() or descriptors["intake_approval"]["absolute_path"] != str(source.trusted_data_root / f"control/qualification-intake/{context_id}/approval.json"):
        raise ControlError("正式来源没有绑定精确上下文的范围批准")
    config = parse_env.parse(raw["parameter"].decode("utf-8"))
    config.update(parse_env.derive(config))
    if config.get("CLIENT") != "claude" or Path(config["D"]) != source.trusted_data_root or config["NEW"] != qualification["campaign_id"] or config["CAND"] != qualification["candidate_id"] or Path(descriptors["parameter"]["absolute_path"]).parent != Path(config["RUNROOT"]):
        raise ControlError("正式来源原参数不属于精确资格或 RUNROOT")
    sealed = parse_json_bytes(_read_bound(source, qualification["sealed_result_binding"]))
    if sealed.get("candidate_revision") != context["candidate_revision"] or sealed.get("evaluation_baseline") != context["evaluation_baseline"]:
        raise ControlError("正式来源不得改用当前 revision 或最新评估基线")
    expected_roots = {str(Path(config["G"]) / qualification["candidate_id"] / ("b" + str(context["evaluation_baseline"]))): "gate_evidence"}
    if "VC5_TARGET_RECORD_STORE" in config:
        expected_roots[str(Path(config["VC5_TARGET_RECORD_STORE"]))] = "unit_records"
    if "VC5_TARGET_REQUEST" in config:
        expected_roots.setdefault(str(Path(config["VC5_TARGET_REQUEST"]).parent), "unit_sources")
    layout = context.get("source_layout")
    if not isinstance(layout, list) or {row["source_locator"]: row["root_kind"] for row in layout} != expected_roots or len(layout) != len(expected_roots):
        raise ControlError("来源辅助范围不是原参数明确选择且审核过的分离布局")
    acquired = {row["source_locator"]: row for row in acquisition["roots"]}
    if set(acquired) != set(expected_roots):
        raise ControlError("正式 acquisition 的根不等于明确批准范围")
    for row in layout:
        if set(row) != {"root_kind", "source_locator", "allowed_relative_paths"} or acquired[row["source_locator"]]["allowed_relative_paths"] != row["allowed_relative_paths"] or acquired[row["source_locator"]]["source_reference_id"] != "managed-qualified-layout:" + context_id:
            raise ControlError("正式 acquisition 的精确文件范围或来源引用改变")
    received = parse_json_bytes(raw["admission"])
    effective = parse_json_bytes(raw["effective_parameters"])
    event = parse_json_bytes(raw["admission_event"])
    unsigned = {key: value for key, value in received.items() if key != "receipt_sha256"}
    bindings = received.get("bindings") or {}
    if received.get("schema_version") != "official-client-vc5-admission/v1" or received.get("status") != "ready" or admission.digest(unsigned) != received.get("receipt_sha256") or received.get("admission_key") != admission.digest(bindings) or bindings.get("campaign") != qualification["campaign_id"] or bindings.get("candidate") != qualification["candidate_id"] or received.get("effective_parameters") != effective or bindings.get("effective_parameters_sha256") != admission.digest(effective) or event.get("status") != "passed" or event.get("receipt_sha256") != received["receipt_sha256"] or event.get("admission_key") != received["admission_key"] or event.get("approval_sha256") != received.get("approval_sha256") or event.get("approval", {}).get("seal_admission") is not True:
        raise ControlError("正式来源的公开准入原件、有效参数或通过事件无法重放")
    receipt = install.validate_install_receipt(parse_json_bytes(raw["install"]))
    deploy = parse_json_bytes(raw["deployment"])
    bound = install.receipt_bindings(receipt).get("claude")
    if receipt["install_target"] != "/root/arm64-capture-driver" or descriptors["driver_manifest"]["absolute_path"] != "/root/arm64-capture-driver/" + install.MANIFEST_NAME or receipt["manifest_sha256"] != manifest["manifest_sha256"] or receipt["files"] != manifest["files"] or bound is None or bound["sha256"] != descriptors["deployment"]["sha256"] or deploy.get("status") != "passed" or source.deployment_sha256 != bound["sha256"]:
        raise ControlError("正式来源驱动清单、安装与已验部署不是同一原件链")
    for name, key in (("deployment", "source_deployment_binding"), ("parameter", "source_parameter_binding"), ("install", "source_install_binding"), ("driver_manifest", "source_driver_manifest_binding")):
        if acquisition[key] != originals[name]:
            raise ControlError("正式 acquisition 没有绑定真实来源原件")
    if purpose != "formed_replay":
        # 首次 freeze 不能只凭请求自报 acquisition：重验当前固定安装和
        # 安装 receipt，正式来源只在真实 ARM64 的 root 安装上下文可用。
        import os, platform
        if platform.system() != "Linux" or platform.machine().lower() not in {"aarch64", "arm64"} or os.geteuid() != 0:
            raise ControlError("formal_intake_not_ready：当前来源缺 ARM64 受管安装现场")
        target = Path("/root/arm64-capture-driver")
        actual_manifest = install.load_manifest(target)
        install.verify_tree(target, actual_manifest, check_mode=True, require_root_owner=True)
        verified = install.verify_install(target, source.trusted_data_root, "claude")
        if actual_manifest != manifest or descriptors["install"]["absolute_path"] != str(source.trusted_data_root / "control" / verified["install_receipt"]):
            raise ControlError("当前正式 intake 与实际安装或最新部署不同")


def validate_qualification_material_import(store: Any, import_binding: Mapping[str, Any], source: Any,
                                            qualification_by_role: Mapping[str, Any], *, purpose: str,
                                            expected_source_scope: str | None = None) -> Any:
    """受管来源已验真后，校验纯证明并真实重放复制视图；不复制或补叶子。"""
    return _validate_material_import(store, import_binding, source, qualification_by_role,
        purpose=purpose, expected_source_scope=expected_source_scope, historical_anchor=False)


def _validate_material_import(store: Any, import_binding: Mapping[str, Any], source: Any,
                              qualification_by_role: Mapping[str, Any], *, purpose: str,
                              expected_source_scope: str | None, historical_anchor: bool, historical_reader: Any = None) -> Any:
    """前驱锚点只验不可变材料；该私有入口本身不授后继消费权。"""
    from .sealed_gate_bindings import VerifiedGateProjection, _TOKEN as projection_token
    from tools.official_client_capture.claude_gate_material_worker import VerifiedReaderIdentity, verify_reader_identity, read_verified_gate_material, _IDENTITY_TOKEN
    from tools.official_client_capture.claude_qualification_material_source import _check_four_roots, qualification_roots
    if purpose not in {"current_import", "current_delivery", "formed_replay"} or set(qualification_by_role) != {"candidate", "rollback"} or source.trusted_data_root is None:
        raise ControlError("门禁导入消费缺明确用途、两份资格或可信实际根")
    document = validate_import_shape(parse_json_bytes(_read_bound(source, import_binding)))
    if expected_source_scope is not None and document["source_origin"]["source_scope"] != expected_source_scope:
        raise ControlError("门禁来源用途与已验配置的真实来源用途不同")
    expected_path = f"control/qualification-imports/{document['import_id']}/material-import.json"
    if import_binding["path"] != expected_path:
        raise ControlError("门禁导入证明不在固定受管坐标")
    reader_raw = _read_bound(source, document["reader_identity_binding"])
    reader_document = parse_json_bytes(reader_raw)
    if reader_document["identity_sha256"] != document["reader_identity_sha256"] or reader_document["contract_sha256"] != document["reader_contract_sha256"]:
        raise ControlError("门禁导入读取身份或合同不是绑定原件")
    if historical_reader is not None:
        # 旧 proof 与旧 reader 文档只作为形成期原件；实际消费另验当前真实 reader。
        # 此私有参数只能由下面的独立历史纯验入口传入，不改旧 import 的身份字段。
        if historical_anchor or purpose != "formed_replay":
            raise ControlError("当前 reader 的历史纯验不能取得导入或交付写权限")
        _validate_historical_reader(reader_document)
        if type(historical_reader) is not VerifiedReaderIdentity or historical_reader.token is not _IDENTITY_TOKEN:
            raise ControlError("历史纯验必须使用当前真实受管 reader")
        verify_reader_identity(historical_reader.document)
        if historical_reader.document["formed_context"] != reader_document["formed_context"]:
            raise ControlError("当前历史 reader 没有绑定原资格形成期上下文")
        reader = historical_reader
    elif historical_anchor:
        _validate_historical_reader(reader_document)
        reader = None
    else:
        verify_reader_identity(reader_document)
        code_root = Path(reader_document["contract_binding"]["absolute_path"]).parents[2]
        reader = VerifiedReaderIdentity(reader_document, code_root, _IDENTITY_TOKEN)
    roots = {row["root_id"]: row for row in document["roots"]}
    all_bindings = [dict(import_binding), dict(document["reader_identity_binding"])]
    subject_projections = []
    reader_replays = []
    prior_projection = source.gate_projection
    if prior_projection is not None and (not isinstance(prior_projection, VerifiedGateProjection)
            or prior_projection.token is not projection_token):
        raise ControlError("同次门禁投影必须来自受管纯验证器，不能由 JSON 授权")
    prior_replays = prior_projection.reader_replays if prior_projection is not None else ()
    if purpose != "formed_replay" and reader_document["tool_identity"].get("policy_version", 0) < MATERIALS_POLICY:
        raise ControlError("当前第二根导入不能使用旧读取身份取得新写入权限")
    if purpose != "formed_replay":
        from .receipts import effective_tool_identity
        candidate_campaign = store.load_campaign(qualification_by_role["candidate"]["campaign_id"])
        if effective_tool_identity(store, candidate_campaign) != reader_document["tool_identity"]:
            raise ControlError("当前材料导入 reader 与 Store 的有效七项工具身份不同")
    acquisitions_by_role, provenance_by_role = {}, {}
    acquired_roots = set()
    for acquisition in document["source_acquisition_bindings"]:
        acq = _exact(parse_json_bytes(_read_bound(source, acquisition["binding"])), ACQUISITION_FIELDS, "material.acquisition")
        q = qualification_by_role[acquisition["role"]]
        if acq["schema_version"] != ACQUISITION_SCHEMA or acq["client"] != "claude" or acq["host_id"] != "ARM64" or acq["authority"] != document["source_origin"]["authority"] or acq["source_scope"] != document["source_origin"]["source_scope"] or acq["qualification_roots"] != qualification_roots(q) or acq["sealed_parent_binding"] != q["sealed_result_binding"] or acq["reader_identity_binding"] != document["reader_identity_binding"]:
            raise ControlError("acquisition 来源、资格角色或读取身份不同源")
        if acq["acquisition_content_sha256"] != canonical_sha256({key: value for key, value in acq.items() if key != "acquisition_content_sha256"}):
            raise ControlError("acquisition 原件内容摘要漂移")
        provenance = parse_json_bytes(_read_bound(source, acq["source_context_binding"]))
        acquisitions_by_role[acquisition["role"]] = acq
        provenance_by_role[acquisition["role"]] = provenance
        if acq["source_id"] != canonical_sha256({"role": acquisition["role"], "roots": acq["roots"], "qualification_roots": qualification_roots(q), "parent": q["sealed_result_binding"], "reader": reader_document["identity_sha256"]}) or acq["source_layout_sha256"] != canonical_sha256(acq["roots"]) or acq["formation_context"] != provenance.get("formation_context"):
            raise ControlError("来源 ID、布局或形成期没有绑定同一 acquisition")
        for name in ("source_deployment_binding", "source_parameter_binding", "source_install_binding", "source_driver_manifest_binding", "source_context_binding"):
            _read_bound(source, validate_external_binding(acq[name], "material." + name))
            all_bindings.append(dict(acq[name]))
        originals = _exact(acq["source_original_bindings"], set(acq["source_original_bindings"]), "material.source_originals")
        for name, original_binding in originals.items():
            raw_original = _read_bound(source, validate_external_binding(original_binding, "material.source_original"))
            descriptor = provenance.get("original_descriptors", {}).get(name)
            if not isinstance(descriptor, dict) or set(descriptor) != {"absolute_path", "sha256", "bytes"} or descriptor["sha256"] != sha256_bytes(raw_original) or descriptor["bytes"] != len(raw_original):
                raise ControlError("来源原件没有对应冻结的真实坐标及原字节")
            all_bindings.append(dict(original_binding))
        acq_roots = _array(acq["roots"], "material.acquisition.roots")
        for row in acq_roots:
            _exact(row, {"root_id", "root_kind", "source_locator", "allowed_relative_paths", "missing_relative_paths", "source_reference_id", "source_reference_sha256"}, "material.acquisition.root")
            acquired_roots.add(row["root_id"])
            root = roots.get(row["root_id"])
            if root is None or any(row[key] != root[key] for key in ("root_kind", "source_locator", "source_reference_id", "source_reference_sha256")):
                raise ControlError("导入根不是对应 acquisition 取得的受管范围")
            identity_fields = {"authority": acq["authority"], "reference_id": row["source_reference_id"], "reference_sha256": row["source_reference_sha256"], "source_locator": row["source_locator"], "root_kind": row["root_kind"]}
            if canonical_sha256(identity_fields) != row["root_id"]:
                raise ControlError("来源根 ID 与固定来源凭证不同")
            allowed = row["allowed_relative_paths"]
            if allowed != sorted(set(allowed)) or any(validate_relative_path(path, "material.allowed_relative") != path for path in allowed):
                raise ControlError("acquisition 来源范围没有闭合")
            if any(file["source_relative_path"] not in allowed for file in document["files"] if file["root_id"] == row["root_id"]):
                raise ControlError("复制映射超出 acquisition 的精确来源范围")
            missing = row["missing_relative_paths"]
            if missing != sorted(set(missing)) or set(allowed) & set(missing) or any(validate_relative_path(path, "material.missing_relative") != path for path in missing):
                raise ControlError("来源负向探测范围不闭合")
            if row["source_reference_sha256"] != canonical_sha256(provenance):
                raise ControlError("来源根凭证摘要与取得来源的真实原件不同")
        all_bindings.append(dict(acquisition["binding"]))
    if acquired_roots != set(roots):
        raise ControlError("导入证明声明了来源取得表以外的根")
    ordered_provenance = [provenance_by_role[role] for role in ("candidate", "rollback")]
    material = [{key: value for key, value in file.items() if key != "archive_binding"} for file in document["files"]]
    origin = document["source_origin"]
    if origin["source_instance_id"] != canonical_sha256(ordered_provenance) or origin["deployment_sha256"] != canonical_sha256([value.get("original_descriptors", {}).get("deployment", value.get("deployment_binding")) for value in ordered_provenance]) or origin["source_material_sha256"] != canonical_sha256(material) or origin["formed_under"] != [acquisitions_by_role[role]["formation_context"] for role in ("candidate", "rollback")]:
        raise ControlError("导入来源实例、部署、材料或形成期摘要没有完整重算")
    if document["import_id"] != canonical_sha256({"source_origin": origin, "parents": document["parents"], "reader_identity_sha256": reader_document["identity_sha256"], "source_files": material}):
        raise ControlError("确定性导入 ID 与真实来源及资格父不同")
    for role in ("candidate", "rollback"):
        _validate_intake_originals(store, source, acquisitions_by_role[role], provenance_by_role[role], qualification_by_role[role], purpose=purpose)
    if origin["source_scope"] == "formal" and reader_document["formed_context"] != {"qualification_roots": {role: qualification_roots(qualification_by_role[role]) for role in ("candidate", "rollback")}}:
        raise ControlError("正式读取身份没有绑定两份精确来源资格")
    coordinates = {(row["root_id"], row["source_relative_path"]): row for row in document["files"]}
    replay_reads = set()
    metadata_reads = set()
    replay_accesses = {}
    for parent in document["parents"]:
        q = qualification_by_role[parent["role"]]
        _check_four_roots(store, q)
        if parent["qualification_roots"] != qualification_roots(q) or parent["sealed_result_binding"] != q["sealed_result_binding"]:
            raise ControlError("门禁投影资格父不是对应冻结四根")
        raw = _read_bound(source, parent["sealed_result_binding"])
        sealed = parse_json_bytes(raw)
        if parent["parent_schema"] != "claude-upgrade-acceptance/v1" or sealed.get("schema_version") != parent["parent_schema"] or canonical_sha256(sealed) != parent["parent_semantic_sha256"] or sealed.get("store", {}).get("acceptance_ref") != q["acceptance_ref"] or sealed.get("store", {}).get("validation_completed_ref") != q["validation_ref"] or sealed.get("candidate_id") != q["candidate_id"] or sealed.get("production_state") != "not_activated":
            raise ControlError("门禁投影封存父语义或原件漂移")
        all_bindings.append(dict(parent["sealed_result_binding"]))
        domains = [row for row in document["domains"] if row["role"] == parent["role"]]
        if not domains:
            if document["source_origin"]["source_scope"] != "regression_fixture" or parent["role"] != "rollback" or "external_gate" in sealed:
                raise ControlError("正式资格或候选门禁域缺失，禁止降级")
            continue
        if len(domains) != 1:
            raise ControlError("同一资格角色的门禁父域重复")
        domain = domains[0]
        root = roots.get(domain["source_root_id"])
        if domain["parent_binding"] != parent["sealed_result_binding"] or domain["parent_schema"] != parent["parent_schema"] or domain["domain_pointer"] != "/external_gate" or root is None or root["root_kind"] != "gate_evidence" or canonical_sha256(sealed.get("external_gate")) != domain["domain_sha256"] or sealed["external_gate"]["evidence_root"] != root["source_locator"] or sealed["external_gate"]["receipt"]["path"] != domain["receipt_relative_path"]:
            raise ControlError("门禁父指针、角色、根或原域摘要错配")
        receipt = coordinates.get((root["root_id"], domain["receipt_relative_path"]))
        if receipt is None or receipt["content_sha256"] != sealed["external_gate"]["receipt"]["sha256"] or receipt["content_bytes"] != sealed["external_gate"]["receipt"]["bytes"]:
            raise ControlError("門禁父收据没有真实一一副本")
        receipt_document = parse_json_bytes(_read_bound(source, receipt["archive_binding"]))
        from tools.official_client_capture.claude_qualification_material_source import qualification_gate_subject
        # 与来源准备共用原件读取；真实 accept 不含候选身份和模式字段。
        # 原 reader、subject 摘要和完整结果比较仍在本调用之后强制执行。
        subject_bindings = []
        current_projections = []
        expected = qualification_gate_subject(store, q, source.trusted_data_root, sealed,
            read_bindings=subject_bindings, read_projections=current_projections)
        subject_projections.extend(current_projections)
        if type(source.bytes_by_path) is _SuccessorReadSnapshot:
            source.bytes_by_path._register_subject_artifacts(_subject_artifact_requirements(store,
                source.trusted_data_root, q, subject_bindings, current_projections,
                lambda binding: _read_bound(source, binding)))
        for binding in subject_bindings:
            # 对照来源能力的精确原字节，回传既有归档列表；形成期重放
            # 不得用当前磁盘的同名材料掩盖缺失或不同的历史原件。
            _read_bound(source, binding)
            all_bindings.append(binding)
        if canonical_sha256(expected) != domain["gate_subject_sha256"]:
            raise ControlError("门禁完整 subject 不是四根与原父推出的身份")
        if historical_anchor:
            continue
        specs = material_worker_spec(reader, list(roots.values()), document["files"], document["reader_accesses"], data_root=source.trusted_data_root)
        result, replay = read_verified_gate_material(reader, *specs, root["source_locator"], domain["receipt_relative_path"],
            view="imported", expected_subject=expected, prior_replays=prior_replays)
        reader_replays.append(replay)
        if result["result"] != receipt_document:
            raise ControlError("复制视图的原 reader 完整结果不同")
        for logical in result["reader_readset"]:
            matches = [(row["root_id"], Path(logical).relative_to(Path(row["source_locator"])).as_posix()) for row in roots.values() if Path(logical).is_relative_to(Path(row["source_locator"]))]
            if len(matches) != 1:
                raise ControlError("复制视图读集未知根或多根重叠")
            replay_reads.add(matches[0])
        for access in result["reader_accesses"]:
            replay_accesses[canonical_sha256(access)] = access
        for logical in result["metadata_files"]:
            matches = [(row["root_id"], Path(logical).relative_to(Path(row["source_locator"])).as_posix()) for row in roots.values() if Path(logical).is_relative_to(Path(row["source_locator"]))]
            if len(matches) != 1:
                raise ControlError("复制视图元数据读集未知根或多根重叠")
            metadata_reads.add(matches[0])
    declared_reader = {_coordinate(row) for row in document["reader_readset"]}
    if historical_anchor:
        replay_reads = declared_reader
        replay_accesses = {canonical_sha256(row): row for row in document["reader_accesses"]}
        for access in document["reader_accesses"]:
            if access.get("type") == "regular":
                logical = Path(access["logical_path"])
                matches = [(row["root_id"], logical.relative_to(Path(row["source_locator"])).as_posix())
                    for row in roots.values() if logical.is_relative_to(Path(row["source_locator"]))]
                if len(matches) != 1:
                    raise ControlError("前驱元数据读集存在未知或重叠根")
                metadata_reads.add(matches[0])
    if replay_reads != declared_reader or sorted(replay_accesses.values(), key=lambda row: (row["logical_path"], row["operation"])) != document["reader_accesses"]:
        raise ControlError("复制视图字节读集或实际元数据访问证明不相同")
    strict = _strict_source_closure(list(roots.values()), document["files"], sorted(replay_reads | metadata_reads), lambda row: _read_bound(source, row["archive_binding"]))
    if strict != sorted({_coordinate(row) for row in document["strict_readset"]}):
        raise ControlError("未知 JSON 严格闭包不是声明的完整读集")
    for file in document["files"]:
        raw = _read_bound(source, file["archive_binding"])
        path = source.trusted_data_root / file["archive_binding"]["path"]
        if path.is_symlink() or not path.is_file() or f"{stat.S_IMODE(path.stat().st_mode):04o}" != file["source_mode"]:
            raise ControlError("复制原件实际模式或类型漂移")
        all_bindings.append(dict(file["archive_binding"]))
    unique = {}
    for row in all_bindings:
        previous = unique.setdefault(row["path"], row)
        if previous != row:
            raise ControlError("门禁投影原件对同一路径绑定不同字节")
    return VerifiedGateProjection(tuple(document["parents"]), tuple(document["domains"]), tuple(unique[path] for path in sorted(unique)), tuple(document["files"]), projection_token, tuple(subject_projections), tuple(reader_replays))


_HISTORICAL_MATERIAL_READER = ContextVar("claude_historical_material_reader", default=None)


@contextmanager
def historical_material_reader_scope(repository: Path):
    """固定当前实际源码根，只给原成功方法的历史材料纯验使用。

    不改来源 resolver、Store 有效身份、旧 proof 或 reader 身份；不生成新导入，
    不授 current_import/current_delivery 权限。每次材料消费都运行真实当前 worker，
    比较完整旧结果、subject、读集、元数据访问和严格闭包；作用域退出再核当前代码。
    """
    from tools.official_client_capture.claude_gate_material_worker import verify_reader_identity
    repository = repository.resolve(strict=True)
    if repository.is_symlink() or not (repository / "tools/official_client_capture/claude_gate_material_worker.py").is_file():
        raise ControlError("历史材料纯验缺当前实际受管源码根")
    state = {"repository": repository, "readers": [], "consumers": []}
    token = _HISTORICAL_MATERIAL_READER.set(state)
    try:
        yield state["consumers"]
    finally:
        try:
            for reader in state["readers"]:
                verify_reader_identity(reader.document)
        finally:
            _HISTORICAL_MATERIAL_READER.reset(token)


def revalidate_historical_material_import(store: Any, import_binding: Mapping[str, Any], source: Any,
        qualification_by_role: Mapping[str, Any], *, expected_source_scope: str) -> Any:
    """由当前真正 reader 实读原归档材料；原形成期 proof 始终保持原字节。"""
    from .delivery_execution import VerifiedSource
    from tools.official_client_capture.claude_gate_material_worker import compute_gate_reader_identity, verify_reader_identity
    state = _HISTORICAL_MATERIAL_READER.get()
    if state is None or type(source) is not VerifiedSource or source.trusted_data_root is None:
        raise ControlError("历史材料当前纯验缺固定源码作用域与已验真实来源")
    document = validate_import_shape(parse_json_bytes(_read_bound(source, import_binding)))
    old_reader = _validate_historical_reader(parse_json_bytes(_read_bound(source, document["reader_identity_binding"])))
    reader = compute_gate_reader_identity(state["repository"], formed_context=old_reader["formed_context"])
    verify_reader_identity(reader.document)
    state["readers"].append(reader)
    projection = _validate_material_import(store, import_binding, source, qualification_by_role,
        purpose="formed_replay", expected_source_scope=expected_source_scope, historical_anchor=False,
        historical_reader=reader)
    # 记录只读主语和实际当前 reader；该列表不是新的来源或写入 capability。
    state["consumers"].append({"import_binding": dict(import_binding),
        "qualification_roots": {role: dict(value) for role, value in qualification_by_role.items()},
        "historical_reader_sha256": document["reader_identity_sha256"],
        "current_reader_identity": reader.document["tool_identity"],
        "current_reader_sha256": reader.document["identity_sha256"],
        "reader_replay_count": len(projection.reader_replays)})
    verify_reader_identity(reader.document)
    return projection


def projection_for_configuration(store: Any, configuration: Mapping[str, Any], source: Any, *, purpose: str) -> Any:
    """只有已经验真的同一冻结配置能选择本次第二根证明。"""
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import validate_configuration, MATERIAL_CONFIGURATION_SCHEMA
    validate_configuration(dict(configuration))
    if configuration.get("schema_version") != MATERIAL_CONFIGURATION_SCHEMA:
        return None
    if canonical_sha256(configuration) != source.configuration_sha256:
        raise ControlError("门禁投影配置摘要不是已验真的同一来源配置")
    if _HISTORICAL_MATERIAL_READER.get() is not None:
        if purpose != "formed_replay":
            raise ControlError("原成功方法的当前历史材料纯验禁止取得新写入用途")
        # 已存在的真实材料后继仍交给原完整合同；不补造登记或四个 pass。
        if describe_material_successor_bindings(store, configuration) is not None:
            raise ControlError("该历史纯验入口不替代已有材料后继合同；须显式消费其原完整关联")
        return revalidate_historical_material_import(store, configuration["qualification_materials_binding"], source,
            {name: configuration[name]["qualification"] for name in ("candidate", "rollback")},
            expected_source_scope=configuration["source_scope"])
    successor = resolve_material_successor(store, configuration, source, purpose=purpose)
    if successor is not None:
        return successor
    return validate_qualification_material_import(store, configuration["qualification_materials_binding"], source,
        {name: configuration[name]["qualification"] for name in ("candidate", "rollback")}, purpose=purpose,
        expected_source_scope=configuration["source_scope"])


SUCCESSOR_SCHEMA = "claude-qualification-materials-successor/v1"
SUCCESSOR_ENTRY = "qualification-materials-successor"
SUCCESSOR_FIELDS = {"schema_version", "client", "campaign_id", "candidate_id", "delivery_id", "plan_ref",
    "executor_configuration_ref", "configuration_binding", "configuration_sha256",
    "predecessor_materials_binding", "successor_materials_binding", "qualification_roots_by_role",
    "sealed_parent_bindings_by_role", "delivery_fact_refs", "environment", "tool_evolution_registration_ref",
    "evolution_receipt_binding", "control_deployment_binding", "guard_identity", "execution_context"}


def _validate_historical_reader(value: Any) -> dict[str, Any]:
    """旧 reader 只是原 proof 的不可变身份锚点，不读取旧代码、不运行旧 worker。"""
    from tools.official_client_capture.claude_gate_material_worker import IDENTITY_SCHEMA, RUNTIME_SCHEMA
    from .contracts import validate_tool_identity
    document = _exact(value, {"schema_version", "tool_identity", "formed_context", "contract_binding",
        "contract_sha256", "code_bindings", "target_bindings", "runtime_manifest", "io_version", "identity_sha256"}, "material.predecessor_reader")
    if document["schema_version"] != IDENTITY_SCHEMA or document["io_version"] != "managed-path-fd-metadata/v1" or document["identity_sha256"] != canonical_sha256({key: item for key, item in document.items() if key != "identity_sha256"}):
        raise ControlError("前驱 reader 文档形状或内容摘要漂移")
    validate_tool_identity(document["tool_identity"], "material.predecessor_reader.tool_identity")
    runtime = document["runtime_manifest"]
    if not isinstance(runtime, dict) or runtime.get("schema_version") != RUNTIME_SCHEMA or runtime.get("manifest_sha256") != canonical_sha256({key: item for key, item in runtime.items() if key != "manifest_sha256"}):
        raise ControlError("前驱 reader 运行库锚点不闭合")
    for group in ([document["contract_binding"]], document["code_bindings"], document["target_bindings"], runtime.get("files")):
        rows = _array(group, "material.predecessor_reader.descriptors")
        paths = []
        for row in rows:
            _exact(row, {"absolute_path", "sha256", "bytes", "mode"}, "material.predecessor_reader.descriptor")
            if not isinstance(row["absolute_path"], str) or not Path(row["absolute_path"]).is_absolute() or ".." in Path(row["absolute_path"]).parts:
                raise ControlError("前驱 reader 描述坐标非法")
            validate_external_binding({"path": "descriptor", "sha256": row["sha256"], "bytes": row["bytes"]}, "material.predecessor_reader.bytes")
            if row["mode"] not in {"0400", "0444", "0500", "0555", "0600", "0644", "0700", "0755"}:
                raise ControlError("前驱 reader 原模式非法")
            paths.append(row["absolute_path"])
        if len(paths) != len(set(paths)):
            raise ControlError("前驱 reader 原件描述重复")
    return document


def _successor_payload(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _exact(value, {"schema_version", "persona", "manifest_id", "entries"}, "material.successor_container")
    entries = payload["entries"]
    if payload["schema_version"] != "official-client-operational-evidence/v1" or not isinstance(entries, list) or len(entries) != 1:
        raise ControlError("材料后继必须使用既有 operational v1 的唯一固定条目")
    entry = _exact(entries[0], {"id", "facts"}, "material.successor_entry")
    facts = _exact(entry["facts"], SUCCESSOR_FIELDS, "material.successor_facts")
    if entry["id"] != SUCCESSOR_ENTRY or facts["schema_version"] != SUCCESSOR_SCHEMA or facts["client"] != "claude" or payload["manifest_id"] != canonical_sha256(facts):
        raise ControlError("材料后继条目、主语或确定性清单 ID 错误")
    for key in ("configuration_binding", "predecessor_materials_binding", "successor_materials_binding", "evolution_receipt_binding", "control_deployment_binding"):
        validate_external_binding(facts[key], "material.successor." + key)
    if facts["predecessor_materials_binding"] == facts["successor_materials_binding"]:
        raise ControlError("材料后继必须绑定真正独立的新导入坐标")
    return payload, facts


def _store_binding(store: Any, source_root: Path, reference: Mapping[str, Any]) -> dict[str, Any]:
    """Store 文档必须在本实际 data_root 内，绑定其完整原字节。"""
    path = store.object_path(dict(reference))
    if not path.is_relative_to(source_root):
        raise ControlError("材料后继 Store 原件不属于同一实际 data_root")
    from tools.official_client_capture.claude_gate_material_worker import _plain_file
    raw, _metadata = _plain_file(path)
    return {"path": path.relative_to(source_root).as_posix(), "sha256": sha256_bytes(raw), "bytes": len(raw)}


def describe_material_successor_bindings(store: Any, configuration: Mapping[str, Any], *, plan_ref: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """只返回唯一候选的取件描述；此函数不创建投影或来源读取权限。"""
    from .receipts import tool_evolutions
    digest = canonical_sha256(configuration)
    campaign_id = configuration["candidate"]["qualification"]["campaign_id"]
    campaign = store.load_campaign(campaign_id)
    chain = tool_evolutions(store, campaign)
    matches = []
    stale = False
    for reference in store.list_object_refs("operational_evidence"):
        payload = store.load_object(reference)["payload"]
        if not any(isinstance(row, dict) and row.get("id") == SUCCESSOR_ENTRY for row in payload.get("entries", [])):
            continue
        _payload, facts = _successor_payload(payload)
        if facts["campaign_id"] != campaign_id or facts["configuration_sha256"] != digest or (plan_ref is not None and facts["plan_ref"] != dict(plan_ref)):
            continue
        if not chain or facts["tool_evolution_registration_ref"] != chain[-1][0]:
            stale = True
            continue
        matches.append((reference, facts))
    if len(matches) > 1:
        raise ControlError("同一原配置及有效登记存在不同材料后继，禁止分叉选择")
    if not matches:
        if stale:
            raise ControlError("原配置材料后继没有承接当前有效工具登记")
        return None
    reference, facts = matches[0]
    root = Path(facts["execution_context"]["data_root"])
    return {"reference": dict(reference), "facts": dict(facts), "object_binding": _store_binding(store, root, reference)}


class _SuccessorReadSnapshot(Mapping[str, bytes]):
    """同次读取同时核来源能力和真实私有文件实例；尾验不靠缓存字节。"""
    material_successor_read = True

    def __init__(self, source: Any):
        self.source = source
        self.records: dict[str, tuple[bytes, Any]] = {}
        self.metadata_records: dict[tuple[str, str], Any] = {}
        self._artifact_bindings: dict[str, dict[str, Any]] = {}
        self.dependencies: dict[Any, Any] = {}
        self.dependency_covered = False

    def _register_subject_artifacts(self, requirements: Mapping[str, Mapping[str, Any]]) -> None:
        """登记本次已验父合同的准确制品；已观察的模式或绑定不能重设。"""
        for path, binding in requirements.items():
            row = dict(validate_external_binding(dict(binding), "material.successor.artifact_binding"))
            if path != row["path"] or self._artifact_bindings.setdefault(path, row) != row:
                raise ControlError("材料后继严格制品要求存在同坐标冲突")
            previous = self.records.get(path)
            if previous is not None and (previous[1][4] != (stat.S_IFREG | 0o755)
                    or len(previous[0]) != row["bytes"] or sha256_bytes(previous[0]) != row["sha256"]):
                raise ControlError("材料后继严格制品要求不能替换已观察的模式或字节")

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)

    def __getitem__(self, path):
        return self.get(path)

    def get(self, path, default=None, *, force_read=False):
        from tools.official_client_capture.claude_gate_material_worker import _plain_file
        root = self.source.trusted_data_root
        relative = validate_relative_path(path, "material.successor.read_path")
        target = root / relative
        ancestors = []
        for parent in (root, *target.parents):
            if not parent.is_relative_to(root):
                continue
            metadata = parent.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) not in {0o700, 0o500}:
                raise ControlError(f"材料后继原件祖先不是同一用户的私有目录：绑定={relative}，祖先={parent}，实际模式={stat.S_IMODE(metadata.st_mode):04o}，实际类型={stat.S_IFMT(metadata.st_mode):06o}，实际uid={metadata.st_uid}，要求目录/uid={os.geteuid()}/模式=0500或0700")
            ancestors.append((str(parent), metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid, metadata.st_mode))
        artifact = self._artifact_bindings.get(relative)
        modes = {0o755} if artifact is not None else {0o600, 0o400}
        required_mode = "0755" if artifact is not None else "0400或0600"
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) not in modes:
            raise ControlError(f"材料后继原件类型、所有者或模式漂移：绑定={relative}，文件={target}，实际模式={stat.S_IMODE(before.st_mode):04o}，实际类型={stat.S_IFMT(before.st_mode):06o}，实际uid={before.st_uid}，要求普通文件/uid={os.geteuid()}/模式={required_mode}")
        from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import _ProducerOriginals, _JoinedCompletedOriginals
        provider = self.source.bytes_by_path
        if type(provider) is _JoinedCompletedOriginals:
            provider = provider.current if relative in provider.current else provider.producer
        if type(provider) is _ProducerOriginals and provider.root == root:
            # 同一产品 Mapping 已核精确绑定、权限、祖先及实际描述符；不再双读。
            raw, metadata = provider.read_with_metadata(relative, force_read=force_read)
            supplied = raw
        else:
            raw, metadata = _plain_file(target)
            supplied = self.source.bytes_by_path.get(relative)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
        if tuple(getattr(before, key) for key in fields) != tuple(getattr(metadata, key) for key in fields):
            raise ControlError(f"材料后继原件在打开期间实例或权限漂移：绑定={relative}，文件={target}，实际模式={stat.S_IMODE(metadata.st_mode):04o}，实际类型={stat.S_IFMT(metadata.st_mode):06o}，实际uid={metadata.st_uid}，要求普通文件/uid={os.geteuid()}/模式={required_mode}")
        if artifact is not None and (len(raw) != artifact["bytes"] or sha256_bytes(raw) != artifact["sha256"]):
            raise ControlError("材料后继严格制品与已验父合同绑定不同")
        if supplied is not None and supplied != raw:
            raise ControlError("材料后继原件与来源精确字节能力不同")
        identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid, metadata.st_mode,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns, tuple(ancestors))
        previous = self.records.setdefault(relative, (raw, identity))
        if previous != (raw, identity):
            raise ControlError("材料后继同次读取原件实例、权限或字节漂移")
        return raw

    def finish(self, *, force_read: bool = True) -> None:
        for path in tuple(self.records):
            self.get(path, force_read=force_read)
        for (path, _operation), (access, _identity) in tuple(self.metadata_records.items()):
            self.record_metadata(Path(path), access)
        self.revalidate_dependencies()

    def capture_dependencies(self, configuration: Mapping[str, Any], facts: Mapping[str, Any]) -> None:
        """仅为已登记产品来源捕获有界实际目录闭集；不足时保留原冷体。"""
        from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import _ProducerOriginals, _JoinedCompletedOriginals
        provider = self.source.bytes_by_path
        if type(provider) is _JoinedCompletedOriginals:
            provider = provider.producer
        if type(provider) is not _ProducerOriginals or provider._subject_dependency_roots is None:
            return
        records = {}
        def capture(path: Path, expected: str | None = None, *,
                    enumerate_members: bool = False, stable_directory: bool = False,
                    selection_pattern: str | None = None, selection_kind: str | None = None) -> bool:
            if len(records) >= 20000:
                return False
            key = str(path) if selection_pattern is None and selection_kind is None else (str(path), selection_pattern, selection_kind)
            previous = records.get(key)
            # 同一路径可先用于祖先检查，后用于真实枚举；只提升到已证明的实际用途。
            enumerate_members = enumerate_members or (previous is not None and previous[1] is not None)
            stable_directory = stable_directory and (previous is None or previous[0] is None or len(previous[0]) == 5)
            value = _successor_dependency_metadata(path, enumerate_members=enumerate_members,
                stable_directory=stable_directory, selection_pattern=selection_pattern, selection_kind=selection_kind)
            if value[1] is not None and len(value[1]) > 20000:
                return False
            actual = value[0]
            if ((expected == "missing" and actual is not None)
                    or (expected == "directory" and (actual is None or not stat.S_ISDIR(actual[2])))):
                return False
            if previous is not None:
                same_identity = (actual is None and previous[0] is None) or (actual is not None
                    and previous[0] is not None and actual[:len(previous[0])] == previous[0])
                if (not same_identity or value[2] != previous[2]
                        or (previous[1] is not None and value[1] != previous[1])):
                    raise ControlError("材料消费者实际依赖在首轮捕获期间漂移")
            records[key] = value
            return True
        selections = getattr(provider, "_material_subject_selections", None)
        if selections is None:
            return
        # subject 的纯坐标根只核路径身份；成员集合只来自已核读取器的准确选择。
        for path in provider._subject_dependency_roots:
            if not capture(path, "directory", stable_directory=True):
                return
        for path, kind in selections:
            if not capture(path, enumerate_members=True,
                    stable_directory=kind != "sealed-boundary", selection_kind=kind):
                return
        # 封存边界首轮捕获必须仍等于已验清单或已验重绑链末，不能把首验后
        # 新出现的文件或收据直接登记成热复用的合法依赖。
        for contract in getattr(provider, "_material_subject_sealed_boundaries", ()):
            current_roots, current_entries = [], []
            for path, prefix in contract["roots"]:
                identity, members, _ancestors = records[(str(path), None, "sealed-boundary")]
                if (identity is None or len(identity) != 9 or identity[2] & 0o077
                        or not (stat.S_ISREG(identity[2]) or stat.S_ISDIR(identity[2]))):
                    return
                boundary = _successor_sealed_stat_view(identity)
                current_roots.append({"path": str(path), "prefix": prefix, **boundary})
                if stat.S_ISREG(identity[2]):
                    current_entries.append({"path": prefix + "/" + path.name, **boundary})
                else:
                    for relative, child in members or ():
                        if len(child) != 9 or not stat.S_ISREG(child[2]) or child[2] & 0o077:
                            return
                        current_entries.append({"path": prefix + "/" + relative,
                            **_successor_sealed_stat_view(child)})
            current = {"roots": current_roots, "entries": sorted(current_entries, key=lambda row: row["path"])}
            rebind_key = (str(contract["rebind_root"]), None, "sealed-rebinds")
            rebind_members = records[rebind_key][1]
            if (current not in contract["views"] or rebind_members is None
                    or tuple(name for name, _identity in rebind_members) != contract["rebind_names"]):
                return
        _verify_material_recorded_dependency_members(provider, records)
        for path in provider._subject_dependency_probes:
            if not capture(path, stable_directory=True):
                return
        # 绑定的父目录只承担路径身份检查，不能因兄弟文件增删扩大材料失效范围。
        for relative in tuple(provider.bindings):
            for parent in (self.source.trusted_data_root / relative).parents:
                if parent.is_relative_to(self.source.trusted_data_root) and not capture(parent, "directory", stable_directory=True):
                    return
        if configuration["source_scope"] == "formal":
            # 原正式注册只选择 Claude 部署 glob 内的普通非符号链接文件。
            from tools.official_client_capture.claude_qualification_material_source import managed_driver_module
            background = managed_driver_module("background_validation", current=True)
            if not capture(self.source.trusted_data_root / "control", "directory", enumerate_members=True,
                    stable_directory=True, selection_pattern=background.DEPLOY_RECEIPT_GLOBS["claude"]):
                return
        documents = [validate_import_shape(parse_json_bytes(_read_bound(replace(self.source, bytes_by_path=self), facts[name])))
            for name in ("predecessor_materials_binding", "successor_materials_binding")]
        for document in documents:
            for root in document["roots"]:
                if not capture(self.source.trusted_data_root / root["archive_root_relative"], "directory", stable_directory=True):
                    return
            for access in document["reader_accesses"]:
                if access["type"] not in {"directory", "missing"}:
                    continue
                logical = Path(access["logical_path"])
                matches = [row for row in document["roots"] if logical.is_relative_to(Path(row["source_locator"]))]
                if len(matches) > 1:
                    raise ControlError("材料消费者目录及负向探测存在重叠逻辑根")
                if matches:
                    root = matches[0]
                    actual = self.source.trusted_data_root / root["archive_root_relative"] / logical.relative_to(Path(root["source_locator"]))
                    enumerated = access["operation"] in {"listdir", "scandir"}
                    if not capture(actual, access["type"], enumerate_members=enumerated, stable_directory=not enumerated):
                        return
        # 只用当前 reader 的规格定位根外目录和 bytecode 探测，不读取旧代码描述。
        from tools.official_client_capture.claude_gate_material_worker import VerifiedReaderIdentity, verify_reader_identity, _IDENTITY_TOKEN
        document = documents[1]
        reader_document = parse_json_bytes(_read_bound(replace(self.source, bytes_by_path=self), document["reader_identity_binding"]))
        verify_reader_identity(reader_document)
        reader = VerifiedReaderIdentity(reader_document, Path(reader_document["contract_binding"]["absolute_path"]).parents[2], _IDENTITY_TOKEN)
        _files, directories, missing = material_worker_spec(reader, document["roots"], document["files"],
            document["reader_accesses"], data_root=self.source.trusted_data_root)
        directory_index = {row["logical_path"]: Path(row["physical_path"]) for row in directories}
        for row in directories:
            # 规格补充的材料／代码祖先只有路径用途，不能枚举 /、临时根和用户目录。
            if not capture(Path(row["physical_path"]), "directory", stable_directory=True):
                return
        for access in document["reader_accesses"]:
            if access["type"] != "directory":
                continue
            actual = directory_index.get(access["logical_path"])
            if actual is None:
                # 规格没有唯一实际映射便没有复用覆盖；原完整语义体照常执行。
                return
            enumerated = access["operation"] in {"listdir", "scandir"}
            if not capture(actual, "directory", enumerate_members=enumerated, stable_directory=not enumerated):
                return
        for logical in missing:
            path = Path(logical)
            matches = [row for row in document["roots"] if path.is_relative_to(Path(row["source_locator"]))]
            if len(matches) > 1:
                raise ControlError("材料消费者当前 missing 存在重叠逻辑根")
            if matches:
                root = matches[0]
                path = self.source.trusted_data_root / root["archive_root_relative"] / path.relative_to(Path(root["source_locator"]))
            if not capture(path, "missing"):
                return
        prefix = reader_document["runtime_manifest"].get("bytecode_prefix")
        if prefix and not capture(Path(prefix), "missing"):
            return
        self.dependencies = records
        self.dependency_covered = True

    def revalidate_dependencies(self) -> None:
        for key, previous in self.dependencies.items():
            path, selection_pattern, selection_kind = (key, None, None) if isinstance(key, str) else key
            if _successor_dependency_metadata(Path(path), enumerate_members=previous[1] is not None,
                    stable_directory=previous[0] is not None and len(previous[0]) == 5,
                    selection_pattern=selection_pattern, selection_kind=selection_kind) != previous:
                raise ControlError("材料消费者实际目录全集、负向探测、文件实例或祖先漂移")

    def record_metadata(self, path: Path, access: Mapping[str, Any]) -> None:
        """前驱材料目录及 missing 实际探测；只对已核固定复制根生效。"""
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if access["type"] != "missing":
                raise ControlError("前驱材料目录实际缺失")
            identity = None
        else:
            if access["type"] != "directory" or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or f"{stat.S_IMODE(metadata.st_mode):04o}" != access.get("mode"):
                raise ControlError("前驱材料目录类型、所有者、模式或负向探测漂移")
            members = sorted(item.name for item in path.iterdir()) if "members" in access else None
            if members is not None and members != access["members"]:
                raise ControlError("前驱材料目录成员不是原 reader 的完整结果")
            identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid, metadata.st_mode,
                metadata.st_mtime_ns, metadata.st_ctime_ns, members)
        previous = self.metadata_records.setdefault((str(path), access["operation"]), (dict(access), identity))
        if previous != (dict(access), identity):
            raise ControlError("前驱材料目录或 missing 探测在同次消费期间漂移")


def _successor_dependency_metadata(path: Path, *, enumerate_members: bool = False,
                                   stable_directory: bool = False,
                                   selection_pattern: str | None = None,
                                   selection_kind: str | None = None) -> tuple[Any, ...]:
    """真实路径／负向探测；只有实际枚举用途才保留目录成员。

    祖先仅绑定 dev、inode、类型、uid、gid 和模式；目录大小及写入时间
    属于其内容集合，不能把无关兄弟路径变化升级为材料拒绝条件。
    """
    ancestors = []
    for parent in path.parents:
        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            ancestors.append((str(parent), None))
            continue
        ancestors.append((str(parent), _successor_file_identity(metadata)[:5]))
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None, None, tuple(ancestors)
    def dependency_identity(value: Any) -> tuple[Any, ...]:
        identity = _successor_file_identity(value)
        # EvidenceManifest 的 stat 边界包含硬链接数，普通字节身份不能代替它。
        return (*identity, value.st_nlink) if selection_kind == "sealed-boundary" else identity

    def selected_members() -> tuple[Any, ...]:
        if selection_kind is not None:
            from tools.official_client_capture import claude_upgrade as upgrade
            from tools.official_client_capture import codex_upgrade_project_ledger as ledger
            from tools.official_client_capture import claude_upgrade_release as release
            from tools.official_client_capture import codex_upgrade_evidence_manifest as boundary
            recursive = selection_kind in {"source", "inventory", "release", "sealed-boundary", "recorded-files"}
            entries = []
            for item in sorted(path.rglob("*") if recursive else path.iterdir()):
                relative = item.relative_to(path)
                if selection_kind == "sealed-rebinds" and boundary.REBIND_RE.fullmatch(item.name) is None:
                    continue
                if selection_kind == "source" and (any(part in upgrade.SKIP_DIRECTORIES for part in relative.parts)
                        or not item.is_file() or item.is_symlink()):
                    continue
                if selection_kind == "release" and (not item.is_file() or item.name == release.STAGED_RECEIPT_FILENAME):
                    continue
                if selection_kind == "inventory" and item.is_dir() and not item.is_symlink():
                    continue
                if selection_kind == "recorded-files" and not (item.is_file() or item.is_symlink()):
                    # 与原录制合同保持同一递归文件／链接选择，不扩大空目录语义。
                    continue
                if selection_kind == "events" and ledger.INFLIGHT_EVENT_TEMP_RE.fullmatch(item.name):
                    continue
                if selection_kind == "prepare-results" and item.name.startswith("."):
                    continue
                metadata = item.lstat()
                identity = dependency_identity(metadata)
                if (selection_kind == "sealed-boundary" and stat.S_ISDIR(metadata.st_mode)
                        and not metadata.st_mode & 0o077):
                    # 原 preflight 只检查嵌套目录私有权限，不把其时间或空目录
                    # 成员纳入 stat 清单。异常目录保留在结果里，使覆盖判定回冷。
                    continue
                if selection_kind in {"directory", "prepare-results"}:
                    # 原枚举只选名字及检查类型；文件字节由精确读集另核。
                    identity = (stat.S_IFMT(metadata.st_mode),)
                elif stat.S_ISDIR(metadata.st_mode) and selection_kind != "sealed-boundary":
                    identity = identity[:5]
                if dependency_identity(item.lstat())[:len(identity)] != identity and selection_kind not in {"directory", "prepare-results"}:
                    raise ControlError("材料 subject 选择中的文件实例在捕获期间漂移")
                entries.append((relative.as_posix(), identity))
            return tuple(entries)
        if selection_pattern is None:
            return tuple(sorted(item.name for item in path.iterdir()))
        return tuple(sorted(item.name for item in path.glob(selection_pattern)
            if item.is_file() and not item.is_symlink()))
    members = selected_members() if enumerate_members and stat.S_ISDIR(metadata.st_mode) else None
    identity = dependency_identity(metadata)
    if stable_directory and stat.S_ISDIR(metadata.st_mode):
        identity = identity[:5]
    if dependency_identity(path.lstat())[:len(identity)] != identity:
        raise ControlError("材料消费者实际目录／探测在捕获期间实例或元数据漂移")
    if (selection_pattern is not None or selection_kind is not None) and members is not None and selected_members() != members:
        raise ControlError("材料消费者实际选择集合在捕获期间漂移")
    return identity, members, tuple(ancestors)


def _successor_sealed_stat_view(identity: tuple[Any, ...]) -> dict[str, int]:
    """把本次真实 stat 转成原 EvidenceManifest 的七字段边界，不增加新字段。"""
    return {"mode": stat.S_IMODE(identity[2]), "device": identity[0], "inode": identity[1],
        "size": identity[5], "mtime_ns": identity[6], "ctime_ns": identity[7], "nlink": identity[8]}


def _material_successor_sealed_boundary_dependencies(originals: Any, campaign: Path,
        stage: Mapping[str, Any], attempt_path: Path) -> tuple[Any, Any] | None:
    """只闭合已验普通封存 attempt 的 stat 清单和准确重绑选择。

    原 subject 已沿 sealed_attempt_context 完整核验清单及当前证据根；此处
    只从它保留的原字节取依赖，不授予封存或重绑权限。恢复、前序覆盖和
    OverlayFS 预演尚无本层闭集证明，继续交给原完整语义体。
    """
    from tools.official_client_capture import codex_upgrade_evidence_manifest as boundary
    from tools.official_client_capture import claude_upgrade_predecessor_sealed as predecessor

    if ("recovery" in stage or stage.get(predecessor.STAGE_FIELD)
            or os.environ.get(boundary.REHEARSAL_CONTEXT_ENV) == "1"):
        return None
    binding = stage["sealed_evidence_manifest"]
    manifest_path = campaign / validate_relative_path(binding["path"], "subject.sealed_manifest")
    if manifest_path != attempt_path.parent / "evidence-manifest.json":
        return None
    relative = manifest_path.relative_to(originals.root).as_posix()
    retained = originals.bindings.get(relative)
    if retained is None or retained["sha256"] != binding["sha256"]:
        return None
    document = parse_json_bytes(originals.add(retained))
    if not isinstance(document, Mapping):
        return None
    try:
        manifest = boundary.validate_manifest_document(document)
        chain = boundary.load_boundary_rebinds(manifest_path, manifest)
        selected = tuple(sorted(path.name for path in manifest_path.parent.iterdir()
            if boundary.REBIND_RE.fullmatch(path.name)))
    except (boundary.EvidenceManifestError, OSError, UnicodeError):
        # 无法再证明依赖时不给热权限；原完整读者保留自己的异常和分类。
        return None
    names = tuple(f"evidence-manifest-rebind-{row['index']:02d}.json" for row in chain)
    if selected != names:
        return None
    for name, receipt in zip(names, chain):
        path = manifest_path.parent / name
        retained = originals.bindings.get(path.relative_to(originals.root).as_posix())
        if retained is None or parse_json_bytes(originals.add(retained)) != receipt:
            return None
    roots = []
    for row in manifest["roots"]:
        path = Path(row["path"])
        if (not path.is_absolute() or not path.is_relative_to(originals.root)
                or path == originals.root or ".." in path.parts):
            return None
        roots.append((path, row["prefix"]))
    if not roots:
        return None
    # 首轮原完整合同接受清单本身或最新重绑边界；不能逐根混搭两种边界。
    view = boundary._boundary_view(manifest["roots"], manifest["entries"], drop=boundary._BOUNDARY_VIEW_DROP)
    views = [view]
    if chain:
        views.append(chain[-1]["new_boundary"])
    contract = {"roots": tuple(roots), "views": tuple(views),
        "rebind_root": manifest_path.parent, "rebind_names": names}
    selections = {(path, "sealed-boundary") for path, _prefix in roots}
    selections.add((manifest_path.parent, "sealed-rebinds"))
    return selections, contract


def _verify_material_recorded_dependency_members(originals: Any, records: Mapping[Any, Any]) -> None:
    """首次实际枚举必须等于原 reader 已验并保留的录制闭集，不能重冻新增成员。"""
    for path, kind, expected in getattr(originals, "_material_subject_recorded_members", ()):
        captured = records.get((str(path), None, kind))
        if captured is None or captured[1] is None or tuple(sorted(row[0] for row in captured[1])) != expected:
            raise ControlError("录制 proof 的首次实际成员集合与已验精确原件闭集不同")


def _material_recorded_subject_dependencies(originals: Any, proof_projection: Any,
                                             projections: list[Any], parent_cache: dict[str, Any]) -> Any:
    """只从原录制 reader 的受管父投影及已登记原字节定位目录闭集。

    四个 proof 子坐标和 state.snapshot 必须有原投影的逐字段证明。其余
    文件只能匹配本次 producer 已有的精确绑定，不能凭 JSON 或目录授权。
    成员名字来自已验 snapshot、copied_originals 和网关父；首次实际捕获
    再与这三个固定集合核对，后续完整字节与尾部语义校验保持原流程。
    """
    from tools.official_client_capture.claude_qualification_material_source import VerifiedQualificationSubjectProjection, _TOKEN
    from tools.official_client_capture import claude_recorded_evaluation_contract as recorded
    data_root = originals.root
    if type(proof_projection) is not VerifiedQualificationSubjectProjection or proof_projection.token is not _TOKEN:
        return None
    proven = {coordinate for coordinate, _root in proof_projection.coordinate_roots}
    proven.update(coordinate for coordinate, _original, _normalized in proof_projection.resolved_bindings)
    if not {(name,) for name in ("policy", "source_state", "projection", "gateway_byte_projection")} <= proven:
        return None

    def known(binding):
        row = dict(validate_external_binding(binding, "subject.recorded_dependency"))
        if originals.bindings.get(row["path"]) != row:
            return None
        originals.add(row)
        return row

    def parent(binding):
        row = known(binding)
        if row is None:
            return None
        if row["path"] not in parent_cache:
            matches = [value for value in projections if type(value) is VerifiedQualificationSubjectProjection
                and value.token is _TOKEN and dict(value.parent_binding) == row]
            if len(matches) != 1:
                return None
            value = matches[0]
            raw = originals.add(row)
            document = parse_json_bytes(raw)
            children = value.project(row, raw)
            if not isinstance(document, Mapping) or children is None:
                return None
            parent_cache[row["path"]] = value, document, children
        return parent_cache[row["path"]]

    def child(value, coordinate):
        subject, document, children = value
        node = document
        for key in coordinate:
            node = node[int(key)] if isinstance(node, list) else node[key]
        rows = [dict(normalized) for current, original, normalized in subject.resolved_bindings
            if current == coordinate and dict(original) == node]
        rows.extend({**node, "path": (Path(base) / node["path"]).as_posix()}
            for current, base in subject.coordinate_roots if current == coordinate)
        if len(rows) != 1 or rows[0] not in children:
            return None
        return known(rows[0])

    proof_parent = parent(proof_projection.parent_binding)
    if proof_parent is None:
        return None
    proof = proof_parent[1]
    children = {name: child(proof_parent, (name,))
        for name in ("policy", "source_state", "projection", "gateway_byte_projection")}
    if any(value is None for value in children.values()):
        return None
    state_parent = parent(children["source_state"])
    projection_parent = parent(children["projection"])
    gateway_parent = parent(children["gateway_byte_projection"])
    if any(value is None for value in (state_parent, projection_parent, gateway_parent)):
        return None
    state, projection, gateway = state_parent[1], projection_parent[1], gateway_parent[1]
    snapshot_binding = child(state_parent, ("snapshot",))
    if snapshot_binding is None or Path(state["data_root"]) != data_root:
        return None
    snapshot_path = data_root / snapshot_binding["path"]
    snapshot = parse_json_bytes(originals.add(snapshot_binding))
    logical = proof["logical_probe"]
    destination = Path(projection["output_root"])
    if (not destination.is_absolute() or not destination.is_relative_to(data_root / "runs")
            or destination.relative_to(data_root).as_posix() != logical["directory"]
            or data_root / proof_projection.parent_binding["path"] != destination / recorded.PROBE_FILENAME
            or projection["probe_id"] != logical["probe_id"] or projection["job_id"] != logical["job_id"]):
        return None
    rows = [row for row in snapshot["probes"] if row["probe_id"] == logical["probe_id"]]
    if len(rows) != 1:
        return None
    row = rows[0]
    attempt = validate_relative_path(row["attempt"], "subject.recorded_attempt")
    host_relative = f"{row['source_id']}/runtime-receipts/{logical['probe_id']}-{Path(attempt).name}.json"
    if row.get("host_receipt", host_relative) != host_relative:
        return None
    host_relative = validate_relative_path(host_relative, "subject.recorded_host_receipt")
    snapshot_root, prefix = snapshot_path.parent / "data", attempt + "/"
    source_files = {}
    for relative, item in snapshot["files"].items():
        if relative.startswith(prefix) or relative == host_relative:
            relative = validate_relative_path(relative, "subject.recorded_original")
            binding = known({"path": (snapshot_root / relative).relative_to(data_root).as_posix(),
                "sha256": item["sha256"], "bytes": item["bytes"]})
            if binding is None:
                return None
            source_files[relative] = binding
    if prefix + "relay-manifest.json" not in source_files or host_relative not in source_files:
        return None
    origin_root, copied_root = snapshot_root / attempt, destination / recorded.ORIGINAL_DIRECTORY
    copied_files = {}
    for index, item in enumerate(proof["copied_originals"]):
        original = source_files.get(item["source_relative"])
        binding = child(proof_parent, ("copied_originals", str(index), "copied"))
        if (original is None or not item["source_relative"].startswith(prefix) or binding is None
                or (binding["sha256"], binding["bytes"]) != (original["sha256"], original["bytes"])
                or data_root / binding["path"] != copied_root / item["source_relative"][len(prefix):]
                or item["source_relative"] in copied_files):
            return None
        copied_files[item["source_relative"]] = binding
    if set(copied_files) != {relative for relative in source_files if relative.startswith(prefix)}:
        return None
    output_files = []
    for item in gateway["projected_files"]:
        relative = validate_relative_path(item["path"], "subject.recorded_gateway_file")
        binding = {**item, "path": (destination / relative).relative_to(data_root).as_posix()}
        if binding not in gateway_parent[2] or known(binding) is None:
            return None
        output_files.append(relative)
    source_names = tuple(sorted(relative[len(prefix):] for relative in copied_files))
    members = {(origin_root, "recorded-files"): source_names,
        (copied_root, "recorded-files"): source_names,
        (origin_root / "relay", "directory"): tuple(sorted(name[len("relay/"):] for name in source_names if name.startswith("relay/"))),
        (destination / "relay", "directory"): tuple(sorted(name[len("relay/"):] for name in output_files if name.startswith("relay/")))}
    roots = {snapshot_path.parent, snapshot_root, origin_root, copied_root, destination,
        origin_root / "relay", destination / "relay", (snapshot_root / host_relative).parent}
    probes = {origin_root / "relay/intervention.jsonl", copied_root / "relay/intervention.jsonl",
        origin_root / "tls-clienthello.pcap", copied_root / "tls-clienthello.pcap"}
    return roots, probes, set(members), members


def _material_successor_subject_dependencies(originals: Any, configuration: Mapping[str, Any],
                                             bindings: list[dict[str, Any]], projections: list[Any]) -> tuple[Any, Any]:
    """从固定 subject 调用与已验父投影定位准确选择；未知支路不授热复用。"""
    from tools.official_client_capture.claude_qualification_material_source import VerifiedQualificationSubjectProjection, _TOKEN
    from tools.official_client_capture import claude_upgrade_profile as profile
    from tools.official_client_capture import claude_upgrade_classify as classify
    from tools.official_client_capture import claude_upgrade_release as release
    from tools.official_client_capture import codex_upgrade_project_ledger as ledger
    from tools.official_client_capture import codex_upgrade_wire_transition as transition
    from tools.official_client_capture import claude_upgrade as upgrade
    from tools.official_client_capture import claude_upgrade_capture as capture_contract
    data_root = originals.root
    campaigns = {data_root / "evidence/campaigns" / configuration[role]["qualification"]["campaign_id"]
        for role in ("candidate", "rollback")}
    campaigns = {root for root in campaigns if any((data_root / row["path"]).is_relative_to(root) for row in bindings)}
    roots, probes, selections = set(campaigns), set(), set()
    source_roots, source_campaigns = {}, set()
    recorded_members, recorded_parents = {}, {}
    sealed_boundaries = []
    for campaign in campaigns:
        # 原 subject 总是 load_staged_release；只核其固定库存根，不扫描 Campaign。
        selections.add((campaign / release.STAGED_RELEASE_ROOT, "release"))
        selections.update(((campaign / transition.EVOLUTION_DIR, "migrations"),
            (campaign / transition.TRANSITIONS_DIR, "migrations")))
    for projection in projections:
        if type(projection) is not VerifiedQualificationSubjectProjection or projection.token is not _TOKEN:
            raise ControlError("材料消费者目录依赖缺原 subject 的已验投影")
        base = data_root if projection.base_relative == "." else data_root / validate_relative_path(projection.base_relative, "subject.dependency_root")
        coordinates = dict(projection.coordinate_roots)
        proven_coordinates = set(coordinates) | {coordinate for coordinate, _original, _normalized in projection.resolved_bindings}
        for relative in (projection.base_relative, *coordinates.values()):
            if relative != ".":
                roots.add(data_root / validate_relative_path(relative, "subject.dependency_root"))
        document = parse_json_bytes(originals.add(projection.parent_binding))
        parent = data_root / projection.parent_binding["path"]
        domains = {coordinate for coordinate, _digest in projection.metadata_domains}
        if not isinstance(document, Mapping):
            continue
        if ("recovery_evidence",) in domains or ("recovery" in document and domains != {()}):
            # 恢复库存和跨 segment 选择尚无本层闭包证明，保留原完整校验。
            return None, ()
        if {("binary",), ("build", "parameters"), ("implementation_tests",)} <= domains:
            root = Path(document["source"]["root"])
            if not root.is_absolute() or not root.is_relative_to(data_root) or root == data_root:
                return None, ()
            campaign = next((value for value in campaigns if parent.is_relative_to(value)), None)
            if campaign is None or (campaign in source_roots and source_roots[campaign] != root):
                return None, ()
            source_roots[campaign] = root
        if ("verification", "source_manifest") in coordinates:
            bundle = data_root / coordinates[("verification", "source_manifest")]
            selections.add((bundle, "inventory"))
            imported = base / validate_relative_path(document["imported_run"]["path"], "subject.imported_run")
            probes.add(imported.parent / "client-run-failure.json")
        if ("execution", "isolation_recipe") in coordinates:
            # 该签名只由原已验客户端任务登记；capture 按精确绑定及固定优先选择闭合。
            execution = document["execution"]
            selections.add((data_root / coordinates[("execution", "isolation_recipe")], "inventory"))
            for coordinate, relative in coordinates.items():
                if len(coordinate) == 3 and coordinate[0] == "scenario_reuse" and coordinate[2] == "source_manifest":
                    selections.add((data_root / relative, "inventory"))
            capture = Path(execution["capture_root"])
            if not capture.is_absolute() or not capture.is_relative_to(data_root) or capture == data_root:
                return None, ()
            roots.add(capture)
            probes.add(capture / "ready.json")
            for filename in ("capture-origin-committed.json", "capture-origin-ready.json", "capture-origin-reserved.json"):
                path = capture / filename
                probes.add(path)
                if path.is_file():
                    if filename == "capture-origin-committed.json" and execution["source_scope"] == "formal":
                        # committed 的解码依赖包还需独立库存证明；不能仅凭 capture 根授权。
                        return None, ()
                    break
            selected_campaign = None
            for ancestor in base.parents:
                path = ancestor / "campaign.json"
                probes.add(path)
                if path.is_file():
                    selected_campaign = ancestor
                    break
            if selected_campaign not in campaigns:
                return None, ()
            source_campaigns.add(selected_campaign)
            if execution["source_scope"] == "formal":
                project = Path(execution["project_ledger"])
                if not project.is_absolute() or not project.is_relative_to(data_root) or project == data_root:
                    return None, ()
                roots.add(project)
                selections.update(((project / "events", "events"), (project / "migrations", "migrations")))
                current = selected_campaign
                for _ in range(ledger.MAX_ANCESTOR_DEPTH + 1):
                    candidate = current / ledger.ledger_dir_name("claude")
                    probes.update((candidate, candidate / "plan.json"))
                    if candidate == project:
                        break
                    if current.parent == current:
                        return None, ()
                    current = current.parent
                else:
                    return None, ()
        # 精确草案父由原 load_revision 验真；这里只保存该 revision 的成员。
        file_coordinates = {coordinate[1] for coordinate in coordinates if len(coordinate) == 2 and coordinate[0] == "files"}
        if file_coordinates == set(profile.PREPARE_FILES):
            draft = parent.parent
            campaign = next((root for root in campaigns if draft.is_relative_to(root / profile.PREPARE_DRAFT_ROOT)), None)
            if campaign is None:
                return None, ()
            selections.update(((draft, "directory"), (campaign / profile.PREPARE_RESULTS_ROOT, "prepare-results")))
        elif file_coordinates == set(classify.DRAFT_FILES):
            draft = parent.parent
            if not any(draft.is_relative_to(root / classify.DRAFT_ROOT) for root in campaigns):
                return None, ()
            selections.add((draft, "directory"))
        if document.get("job_checkpoint") is not None and any(coordinate[:1] == ("results",) for coordinate in coordinates):
            selections.add((parent.parent / "checkpoints", "directory"))
        if {(name,) for name in ("policy", "source_state", "projection", "gateway_byte_projection")} <= proven_coordinates:
            dependencies = _material_recorded_subject_dependencies(originals, projection, projections, recorded_parents)
            if dependencies is None:
                return None, ()
            recorded_roots, recorded_probes, recorded_selections, members = dependencies
            roots.update(recorded_roots)
            probes.update(recorded_probes)
            selections.update(recorded_selections)
            for key, expected in members.items():
                if recorded_members.setdefault(key, expected) != expected:
                    raise ControlError("录制 proof 对同一目录选择声明不同的已验闭集")
    for role in ("candidate", "rollback"):
        qualification = configuration[role]["qualification"]
        campaign = data_root / "evidence/campaigns" / qualification["campaign_id"]
        if campaign not in campaigns:
            continue
        sealed = parse_json_bytes(originals.add(qualification["sealed_result_binding"]))
        if configuration["source_scope"] == "regression_fixture" and role == "rollback" and "external_gate" not in sealed:
            # 沿用原来源已验父的显式无门禁回退，不给未执行的 subject 增加首轮要求。
            continue
        stage_path = upgrade._legacy_stage_path(campaign, "capture-candidate", qualification["candidate_id"])[1]
        stage_binding = originals.bindings.get(stage_path.relative_to(data_root).as_posix())
        if stage_binding is None:
            return None, ()
        stage = parse_json_bytes(originals.add(stage_binding))
        if "recovery" in stage:
            return None, ()
        attempt_path = campaign / validate_relative_path(stage["attempt"]["path"], "subject.round_one_attempt")
        attempt_binding = originals.bindings.get(attempt_path.relative_to(data_root).as_posix())
        if attempt_binding is None or attempt_binding["sha256"] != stage["attempt"]["sha256"]:
            return None, ()
        attempt = parse_json_bytes(originals.add(attempt_binding))
        if attempt.get("schema_version") == capture_contract.CAPTURE_ATTEMPT_SCHEMA and stage.get("sealed_evidence_manifest") is not None:
            if configuration["source_scope"] != "regression_fixture":
                return None, ()
            selected = _material_successor_sealed_boundary_dependencies(originals, campaign, stage, attempt_path)
            if selected is None:
                return None, ()
            boundary_selections, contract = selected
            selections.update(boundary_selections)
            sealed_boundaries.append(contract)
    for campaign in source_campaigns:
        root = source_roots.get(campaign)
        if root is None:
            return None, ()
        roots.add(root)
        selections.add((root, "source"))
    originals._material_subject_recorded_members = tuple(sorted((path, kind, expected)
        for (path, kind), expected in recorded_members.items()))
    originals._material_subject_selections = tuple(sorted(selections))
    originals._material_subject_sealed_boundaries = tuple(sealed_boundaries)
    return tuple(sorted(roots)), tuple(sorted(probes))


def _successor_state(store: Any, configuration: Mapping[str, Any], facts: Mapping[str, Any], source: Any) -> tuple[Any, Any, Any]:
    """仅从 Store 及冻结原件推出原主语，不能反调用来源 verifier。"""
    from . import delivery_execution as execution
    from .contracts import CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS
    from .receipts import delivery_chain_state, tool_evolutions
    from tools.official_client_capture.claude_qualification_material_source import qualification_roots
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import validate_configuration
    validate_configuration(dict(configuration))
    plan = store.load_object(dict(facts["plan_ref"]))["payload"]
    if facts["plan_ref"].get("object_kind") != "candidate_delivery_plan" or plan.get("schema_version") != execution.PLAN_V2:
        raise ControlError("材料后继必须绑定实际原 Plan v2")
    campaign = store.load_campaign(plan["campaign_id"])
    config_facts = execution.evidence_facts(store, plan["executor_configuration_ref"], "configuration")
    if (facts["campaign_id"], facts["candidate_id"], facts["delivery_id"], facts["executor_configuration_ref"], facts["configuration_sha256"], facts["configuration_binding"]) != (plan["campaign_id"], plan["candidate_id"], plan["delivery_id"], plan["executor_configuration_ref"], plan["configuration_sha256"], config_facts["binding"]):
        raise ControlError("材料后继主语不是原 Plan 与原配置的精确同一交付")
    if configuration != config_facts["configuration"] or canonical_sha256(configuration) != facts["configuration_sha256"] or source.configuration_sha256 != facts["configuration_sha256"] or parse_json_bytes(_read_bound(source, facts["configuration_binding"])) != configuration or facts["predecessor_materials_binding"] != configuration["qualification_materials_binding"]:
        raise ControlError("材料后继不得重签、改写或替换原冻结配置")
    if source.trusted_data_root != Path(facts["execution_context"]["data_root"]) or store.root != source.trusted_data_root / configuration["candidate"]["qualification"]["store_relative"]:
        raise ControlError("材料后继 Store、原配置与来源实际根不同")
    roles = {name: configuration[name]["qualification"] for name in ("candidate", "rollback")}
    if facts["qualification_roots_by_role"] != {role: qualification_roots(q) for role, q in roles.items()} or facts["sealed_parent_bindings_by_role"] != {role: q["sealed_result_binding"] for role, q in roles.items()}:
        raise ControlError("材料后继两角色资格四根或父原件改变")
    state = delivery_chain_state(store, plan["campaign_id"], plan["delivery_id"])
    if not state.complete() or facts["delivery_fact_refs"] != state.valid_refs(store):
        raise ControlError("材料后继要求原四个有效物理 pass 且无待消费失效")
    expected_environment = {key: plan[key] for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS}
    for reference in facts["delivery_fact_refs"]:
        fact = store.load_fact(reference)
        if fact["payload"].get("delivery_plan_ref") != facts["plan_ref"] or any(fact["payload"].get(key) != value for key, value in expected_environment.items()):
            raise ControlError("材料后继四个原物理成果不属于该 Plan 与环境")
    freeze = execution.evidence_facts(store, plan["environment_freeze_ref"], "freeze")
    if facts["environment"] != expected_environment or facts["guard_identity"] != freeze["guard_identity"]:
        raise ControlError("材料后继环境或原活守卫完整身份不同")
    expected_context = {"campaign_id": plan["campaign_id"], "candidate_id": plan["candidate_id"], "delivery_id": plan["delivery_id"], "data_root": str(source.trusted_data_root), "configuration_ref": facts["configuration_binding"]}
    if facts["execution_context"] != expected_context:
        raise ControlError("材料后继稳定执行上下文不是原保存身份")
    from tools.official_client_capture.claude_gate_material_worker import _plain_file
    saved_path = source.trusted_data_root / configuration["paths"]["guard_root_relative"] / "configuration.json"
    saved_raw, _metadata = _plain_file(saved_path)
    saved = parse_json_bytes(_read_bound(source, {"path": saved_path.relative_to(source.trusted_data_root).as_posix(), "sha256": sha256_bytes(saved_raw), "bytes": len(saved_raw)}))
    if saved != {"configuration": dict(configuration), "context": expected_context}:
        raise ControlError("材料后继原守卫保存配置或稳定上下文改变")
    chain = tool_evolutions(store, campaign)
    if not chain or facts["tool_evolution_registration_ref"] != chain[-1][0]:
        raise ControlError("材料后继未绑定当前有效工具演进登记")
    from .receipts import current_tool_identity
    effective = chain[-1][1]["to_identity"]
    if effective != current_tool_identity("claude"):
        raise ControlError("材料后继有效登记不是当前真实双受管根七身份")
    for key in ("policy_sha256", "policy_version", "wire_producer_sha256"):
        if chain[-1][1]["from_identity"][key] != effective[key]:
            raise ControlError("材料后继不能承接 policy 或 wire 变化")
    return plan, roles, chain


def _validate_successor_registration(store: Any, facts: Mapping[str, Any], chain: Any,
                                     old_reader: Mapping[str, Any], new_reader: Mapping[str, Any],
                                     control_raw: bytes, evolution_raw: bytes, predecessor_projection: Any,
                                     source: Any, roles: Mapping[str, Any]) -> None:
    """把有效 Store 登记接回真实演进原件与当前部署，守恒原评估器四摘要。"""
    from tools.official_client_capture import claude_tool_identity_policy as policy
    from tools.official_client_capture import claude_upgrade_evolution as evolution
    from tools.official_client_capture import codex_upgrade_wire_transition as transition
    from .contracts import TOOL_IDENTITY_FIELDS
    campaign_root = source.trusted_data_root / "evidence/campaigns" / facts["campaign_id"]
    registration = chain[-1][1]
    expected_evolution_path = (campaign_root / registration["evolution_receipt"]["path"]).relative_to(source.trusted_data_root).as_posix()
    if facts["evolution_receipt_binding"]["path"] != expected_evolution_path or facts["evolution_receipt_binding"]["sha256"] != registration["evolution_receipt"]["sha256"]:
        raise ControlError("材料后继演进原件不是有效登记所指的原坐标")
    document = parse_json_bytes(evolution_raw)
    control = parse_json_bytes(control_raw)
    try:
        transition._self_check(document, "材料后继演进原件")
    except transition.WireTransitionError as error:
        raise ControlError(str(error)) from error
    if document.get("schema_version") != transition.EVOLUTION_SCHEMA or document.get("campaign_id") != facts["campaign_id"] or document.get("approved_by") != registration["approved_by"] or document.get("approved_at_utc") != registration["approved_at_utc"]:
        raise ControlError("材料后继演进原件的批准、内容摘要或 Campaign 不一致")
    target = document.get("to_identity", {})
    target_store = {key: target.get(key) for key in TOOL_IDENTITY_FIELDS}
    target_store["tool_files_sha256"] = target.get("files_sha256")
    if target_store != new_reader["tool_identity"] or document.get("to_summary") != transition.identity_summary(target):
        raise ControlError("材料后继演进收据与新 reader 七身份不同")
    control_identity = {key: control.get("identity", {}).get(key) for key in TOOL_IDENTITY_FIELDS}
    control_identity["client"] = control.get("client")
    if control.get("schema_version") != "official-client-managed-deploy/v1" or control.get("status") != "passed" or control.get("client") != "claude" or control_identity != new_reader["tool_identity"]:
        raise ControlError("材料后继当前控制部署不是真实当前 Claude 七身份")
    deployment = document.get("bindings", {}).get("deployment_receipt", {})
    deploy_path = deployment.get("path")
    if not isinstance(deploy_path, str):
        raise ControlError("材料后继演进原件缺真实控制部署坐标")
    # 演进收据使用其对账器的实际绝对路径，归档绑定使用同一 data_root 相对坐标。
    actual_deploy_path = Path(deploy_path)
    if not actual_deploy_path.is_absolute() or actual_deploy_path != source.trusted_data_root / facts["control_deployment_binding"]["path"] or deployment.get("sha256") != sha256_bytes(control_raw) or deployment.get("created_at_utc") != control.get("created_at_utc"):
        raise ControlError("材料后继控制部署 binding 不是演进原件的同坐标实际部署")
    actual_evaluator = policy.evaluator_dependency_digests(Path(new_reader["contract_binding"]["absolute_path"]).parents[2])
    recorded_evaluator = document.get("to_evaluator_digests", {})
    if any(recorded_evaluator.get(key) != digest for key, digest in actual_evaluator.items()):
        raise ControlError("材料后继演进没有绑定当前真实四项 evaluator 摘要")
    original_evaluators = []
    q = roles["candidate"]
    sealed_parent = parse_json_bytes(_read_bound(source, q["sealed_result_binding"]))
    for binding in predecessor_projection.archive_bindings:
        if Path(binding["path"]).name != "evaluation-run.json":
            continue
        raw = _read_bound(source, binding)
        run = parse_json_bytes(raw)
        if run.get("campaign_id") == q["campaign_id"] and run.get("candidate_id") == q["candidate_id"] and run.get("candidate_revision") == sealed_parent.get("candidate_revision") and run.get("evaluation_baseline") == sealed_parent.get("evaluation_baseline"):
            from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
            artifacts.validate_evaluation_run(run)
            original_evaluators.append(run["evaluator"])
    if facts["guard_identity"]["source_scope"] == "formal":
        # 正式来源重验整条实际编排器链，不能以一份上传收据代替有效批准。
        from tools.official_client_capture.claude_gate_material_worker import _plain_file
        manifest_path = campaign_root / "campaign.json"
        manifest_raw, _metadata = _plain_file(manifest_path)
        _read_bound(source, {"path": manifest_path.relative_to(source.trusted_data_root).as_posix(), "sha256": sha256_bytes(manifest_raw), "bytes": len(manifest_raw)})
        real_chain = evolution.load_evolutions(campaign_root, parse_json_bytes(manifest_raw))
        real_raw = [canonical_sha256(row) for row in real_chain]
        if document not in real_chain:
            raise ControlError("材料后继演进原件不属于实际有效编排器登记链")
        for reference, entry in chain:
            path = campaign_root / entry["evolution_receipt"]["path"]
            raw, _metadata = _plain_file(path)
            _read_bound(source, {"path": path.relative_to(source.trusted_data_root).as_posix(), "sha256": entry["evolution_receipt"]["sha256"], "bytes": len(raw)})
            if canonical_sha256(parse_json_bytes(raw)) not in real_raw:
                raise ControlError("材料后继 Store 工具链包含非正式演进原件")
        from tools.official_client_capture.claude_qualification_material_source import managed_driver_module
        background = managed_driver_module("background_validation", current=True)
        latest = background.latest_deployment(source.trusted_data_root, "claude")
        if latest["receipt"] != Path(facts["control_deployment_binding"]["path"]).name or latest["sha256"] != facts["control_deployment_binding"]["sha256"]:
            raise ControlError("材料后继绑定的控制部署不是当前实际最新部署")
    elif not original_evaluators:
        # 明确回归来源也必须从已登记的前驱真实收据取得四摘要；不能自报兼容。
        for _reference, entry in chain:
            if entry["to_identity"] != old_reader["tool_identity"]:
                continue
            path = campaign_root / entry["evolution_receipt"]["path"]
            from tools.official_client_capture.claude_gate_material_worker import _plain_file
            raw, _metadata = _plain_file(path)
            previous = parse_json_bytes(_read_bound(source, {"path": path.relative_to(source.trusted_data_root).as_posix(), "sha256": entry["evolution_receipt"]["sha256"], "bytes": len(raw)}))
            original_evaluators.append(previous.get("to_evaluator_digests", {}))
    if not original_evaluators or any({key: row.get(key) for key in actual_evaluator} != actual_evaluator for row in original_evaluators):
        raise ControlError("材料后继原批准四项 evaluator 缺失或有变化，禁止续用原交付")


def _logical_material(document: Mapping[str, Any]) -> dict[str, Any]:
    """只消去真实来源的新凭证与副本坐标，材料、模式和全部读集保留。"""
    roots = {row["root_id"]: (row["root_kind"], row["source_locator"]) for row in document["roots"]}
    if len(roots.values()) != len(set(roots.values())):
        raise ControlError("材料后继逻辑根存在一对多映射")
    def coordinate(row):
        return (*roots[row["root_id"]], row["source_relative_path"])
    return {"parents": document["parents"], "domains": sorted([{**{key: value for key, value in row.items() if key != "source_root_id"}, "logical_root": roots[row["source_root_id"]]} for row in document["domains"]], key=lambda row: row["role"]),
        "roots": sorted(roots.values()), "files": sorted([(coordinate(row), row["content_sha256"], row["content_bytes"], row["source_mode"], row["reader_role"]) for row in document["files"]]),
        "reader_readset": sorted(coordinate(row) for row in document["reader_readset"]), "strict_readset": sorted(coordinate(row) for row in document["strict_readset"]), "reader_accesses": document["reader_accesses"]}


def _fixed_import_acquisitions(document: Mapping[str, Any], source: Any) -> dict[str, Any]:
    """原 import 的固定坐标与独立来源角色闭集；不扫描同名文件替代。"""
    directory = "control/qualification-imports/" + document["import_id"]
    if document["reader_identity_binding"]["path"] != directory + "/reader-identity.json" or [row["role"] for row in document["parents"]] != ["candidate", "rollback"] or [row["role"] for row in document["source_acquisition_bindings"]] != ["candidate", "rollback"] or [row["role"] for row in document["domains"]] != sorted(row["role"] for row in document["domains"]) or [row["root_id"] for row in document["roots"]] != sorted(row["root_id"] for row in document["roots"]):
        raise ControlError("材料后继前后 import 身份坐标或角色排序改变")
    result = {}
    for row in document["source_acquisition_bindings"]:
        role = row["role"]
        if row["binding"]["path"] != directory + "/" + role + "-acquisition.json":
            raise ControlError("材料后继 acquisition 不是固定角色原件坐标")
        acquisition = _exact(parse_json_bytes(_read_bound(source, row["binding"])), ACQUISITION_FIELDS, "material.successor.acquisition")
        if acquisition["source_context_binding"]["path"] != directory + "/" + role + "-source-context.json":
            raise ControlError("材料后继来源凭证不是该独立 import 的固定坐标")
        for name, binding in acquisition["source_original_bindings"].items():
            if binding["path"] != directory + "/source-originals/" + role + "/" + name + ".raw":
                raise ControlError("材料后继来源原件副本跨 import 或错角色")
        logical_roots = sorted([(root["root_kind"], root["source_locator"], root["allowed_relative_paths"], root["missing_relative_paths"]) for root in acquisition["roots"]])
        result[role] = {key: acquisition[key] for key in ("qualification_roots", "entry_references", "sealed_parent_binding", "formation_context")}
        result[role]["roots"] = logical_roots
    return result


@dataclass
class _SuccessorDeliveryLock:
    """只在编排器真实持锁期间存活；复制上下文不能延长此对象的生命。"""

    descriptor: int
    path: Path
    data_root: Path
    campaign_id: str
    candidate_id: str
    identity: tuple[Any, ...]
    owner: tuple[int, int]
    nonce: object = field(default_factory=object)
    active: bool = True

    def check(self) -> None:
        if (not self.active or _SUCCESSOR_DELIVERY_LOCK.get() is not self
                or self.owner != (os.getpid(), threading.get_ident())
                or _successor_file_identity(os.fstat(self.descriptor)) != self.identity
                or _successor_file_identity(self.path.lstat()) != self.identity):
            raise ControlError("材料语义复用的真实交付锁、线程或活动生命周期漂移")


def _successor_file_identity(metadata: Any) -> tuple[Any, ...]:
    return tuple(getattr(metadata, name) for name in
        ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns"))


_SUCCESSOR_DELIVERY_LOCK: ContextVar[Any] = ContextVar("claude_material_successor_delivery_lock", default=None)
_SUCCESSOR_CONSUMER: ContextVar[Any] = ContextVar("claude_material_successor_consumer", default=None)


@contextmanager
def _material_successor_delivery_lock(context: Any, descriptor: int, path: Path):
    """必须由既有交付锁在 flock 成功后登记，不供 JSON、开关或准备器调用。"""
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or _successor_file_identity(path.lstat()) != _successor_file_identity(metadata)):
        raise ControlError("材料语义复用必须绑定当前用户的同一私有交付锁原件")
    lock = _SuccessorDeliveryLock(descriptor, path, context.data_root, context.campaign_id,
        context.candidate_id, _successor_file_identity(metadata), (os.getpid(), threading.get_ident()))
    handle = _SUCCESSOR_DELIVERY_LOCK.set(lock)
    try:
        yield
    finally:
        lock.active = False
        _SUCCESSOR_DELIVERY_LOCK.reset(handle)


def _successor_store_metadata(root: Path) -> tuple[Any, ...]:
    """五个语义区域的完整实例与目录全集；锁目录不参与只读合同。"""
    metadata = root.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o700, 0o500}):
        raise ControlError("材料消费者 Store 根实例或私有模式漂移")
    records = [(str(root), _successor_file_identity(metadata)[:5], None)]
    def visit(path: Path) -> None:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ControlError("材料消费者 Store 原件含链接或所有者漂移")
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) not in {0o700, 0o500}:
                raise ControlError("材料消费者 Store 目录私有模式漂移")
            members = sorted(item.name for item in path.iterdir())
            records.append((str(path), _successor_file_identity(metadata), tuple(members)))
            for name in members:
                visit(path / name)
        elif stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) in {0o600, 0o400}:
            records.append((str(path), _successor_file_identity(metadata), None))
        else:
            raise ControlError("材料消费者 Store 原件类型或私有模式漂移")
    for name in ("store.json", "objects", "campaigns", "facts", "receipts"):
        visit(root / name)
    return tuple(records)


def _successor_projection_signature(projection: Any) -> str:
    """冻结全部可消费投影值；源句柄、token 和 reader 活动标志不授摘要权限。"""
    successor = projection.successor
    subjects = [{"parent_binding": dict(item.parent_binding), "base_relative": item.base_relative,
        "metadata_domains": item.metadata_domains, "coordinate_roots": item.coordinate_roots,
        "resolved_bindings": item.resolved_bindings} for item in projection.subject_projections]
    readers = [(sha256_bytes(item.specification_bytes), sha256_bytes(item.result_bytes))
        for item in projection.reader_replays]
    return canonical_sha256({"manifest": projection.manifest(), "parents": projection.parents, "subjects": subjects,
        "readers": readers, "verified_documents": projection.verified_documents,
        "reference": dict(successor.reference), "facts": dict(successor.facts),
        "object_binding": dict(successor.object_binding), "purpose": successor.purpose,
        "predecessor_materials_binding": dict(successor.predecessor_materials_binding),
        "successor_materials_binding": dict(successor.successor_materials_binding),
        "predecessor_archive_bindings": successor.predecessor_archive_bindings,
        "current_archive_bindings": successor.current_archive_bindings,
        "configuration": dict(successor.configuration),
        "source": {"deployment_sha256": successor.source.deployment_sha256,
            "configuration_sha256": successor.source.configuration_sha256,
            "trusted_data_root": str(successor.source.trusted_data_root)}})


def _successor_producer_signature(state: Mapping[str, Any]) -> str:
    return canonical_sha256({key: state[key] for key in
        ("status", "inventory", "registrations", "readiness", "head")})


@dataclass
class _SuccessorSemanticSlot:
    projection: Any
    snapshot: Any
    signature: str
    sources: dict[tuple[Any, ...], Any] = field(default_factory=dict)


@dataclass
class _SuccessorConsumer:
    """一次受锁消费者的最小语义槽；每个写相位都完成尾验并清空。"""

    lock: _SuccessorDeliveryLock
    store: Any
    configuration: Mapping[str, Any]
    plan_ref: Mapping[str, Any]
    selected: Mapping[str, Any]
    store_metadata: tuple[Any, ...]
    lock_nonce: object
    phase_nonce: object = field(default_factory=object)
    byte_nonce: object = field(default_factory=object)
    active: bool = True
    invalid: bool = False
    finishing: bool = False
    reading: bool = True
    providers: dict[int, Any] = field(default_factory=dict)
    joined: dict[int, Any] = field(default_factory=dict)
    slots: dict[str, _SuccessorSemanticSlot] = field(default_factory=dict)

    def check(self, store: Any, configuration: Mapping[str, Any], plan_ref: Any) -> None:
        self.lock.check()
        if (not self.active or _SUCCESSOR_CONSUMER.get() is not self
                or self.lock.nonce is not self.lock_nonce or (not self.reading and not self.finishing)
                or (self.invalid and not self.finishing) or store is not self.store
                or configuration != self.configuration
                or (plan_ref is not None and dict(plan_ref) != self.plan_ref)
                or describe_material_successor_bindings(store, configuration, plan_ref=self.plan_ref) != self.selected
                or _successor_store_metadata(store.root) != self.store_metadata):
            self.invalid = True
            raise ControlError("材料消费者的原 Plan、配置、关联头、Store 五区域或生命周期漂移")

    def check_source(self, source: Any) -> tuple[Any, ...]:
        from .delivery_execution import VerifiedSource, _revalidate_guard_continuity
        from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import _ProducerOriginals, _JoinedCompletedOriginals
        from tools.official_client_capture.claude_upgrade_guard_epochs import VerifiedGuardContinuity
        if (type(source) is not VerifiedSource or source.trusted_data_root != self.lock.data_root
                or source.configuration_sha256 != canonical_sha256(self.configuration)
                or source.deployment_sha256 != self.configuration["guard"]["deployment_receipt_binding"]["sha256"]
                or type(source.guard_continuity) is not VerifiedGuardContinuity):
            raise ControlError("材料语义复用缺同一真实来源类别、根、配置或连续性能力")
        originals = source.bytes_by_path
        if type(originals) is _JoinedCompletedOriginals:
            capability = self.joined.get(id(originals))
            if capability is None or capability[0] is not originals or capability[2] is not self.phase_nonce:
                raise ControlError("材料报告合并来源缺本次真实准入 token 能力")
            capability[1].revalidate()
            originals = originals.producer
        capability = self.providers.get(id(originals))
        if (type(originals) is not _ProducerOriginals or capability is None or capability[0] is not originals
                or capability[3] is not self.phase_nonce):
            raise ControlError("材料语义复用只接受本消费者登记的精确 producer 能力")
        executor, expected = capability[1:3]
        current = executor._producer_state(originals)
        if _successor_producer_signature(current) != expected:
            raise ControlError("材料消费者当前实际守卫、来源全集或 registry 头漂移")
        proof = _revalidate_guard_continuity(source, required=True)
        content, origin = parse_json_bytes(proof.source_core_bytes), parse_json_bytes(proof.source_origin_bytes)
        binding, pointer, _freeze = executor._producer_record(current, content, origin, originals)
        if (binding != proof.source_record_binding.as_value()
                or pointer != parse_json_bytes(proof.envelope_bytes)["source_json_pointer"]
                or proof.channel_service_instance_id != current["status"]["guard_identity"]["service_instance_id"]):
            raise ControlError("材料消费者来源不属于同一当前 producer 精确坐标")
        return id(originals), source.content_sha256, source.service_instance_id

    def has_source_capability(self, source: Any) -> bool:
        """只做复用资格判定；未登记来源沿原完整路径，不吞掉任何漂移异常。"""
        from .delivery_execution import VerifiedSource
        from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import _ProducerOriginals, _JoinedCompletedOriginals
        if type(source) is not VerifiedSource:
            return False
        originals = source.bytes_by_path
        if type(originals) is _JoinedCompletedOriginals:
            capability = self.joined.get(id(originals))
            if capability is None or capability[0] is not originals or capability[2] is not self.phase_nonce:
                return False
            originals = originals.producer
        capability = self.providers.get(id(originals))
        return (type(originals) is _ProducerOriginals and capability is not None
            and capability[0] is originals and capability[2] is not None and capability[3] is self.phase_nonce)

    def read(self, store: Any, successor_ref: Mapping[str, Any], configuration: Mapping[str, Any],
             source: Any, *, purpose: str, plan_ref: Any) -> Any:
        self.check(store, configuration, plan_ref)
        if dict(successor_ref) != self.selected["reference"]:
            raise ControlError("材料语义槽不能选择另一个材料后继")
        source_key = self.check_source(source)
        slot = self.slots.get(purpose)
        if slot is None:
            snapshots = []
            self.byte_nonce = object()
            projection = _validate_material_successor(store, successor_ref, configuration, source,
                purpose=purpose, plan_ref=self.plan_ref, pending_payload=None, _snapshot_sink=snapshots)
            slot = _SuccessorSemanticSlot(projection, snapshots[0], _successor_projection_signature(projection))
            self.slots[purpose] = slot
        else:
            if _successor_projection_signature(slot.projection) != slot.signature:
                raise ControlError("材料消费者内存投影值被改写")
            if not slot.snapshot.dependency_covered:
                # 实际目录闭集或产品来源能力不足时，逐次保持原完整语义体。
                self.byte_nonce = object()
                snapshots = []
                current = _validate_material_successor(store, successor_ref, configuration, source,
                    purpose=purpose, plan_ref=self.plan_ref, pending_payload=None, _snapshot_sink=snapshots)
                if (_successor_projection_signature(current) != slot.signature
                        or snapshots[0].records != slot.snapshot.records
                        or snapshots[0].metadata_records != slot.snapshot.metadata_records):
                    raise ControlError("材料消费者冷体重验的投影或完整原件实例漂移")
                slot.sources[source_key] = source
                return current
            snapshot = _SuccessorReadSnapshot(source)
            snapshot._artifact_bindings = {path: dict(binding) for path, binding in slot.snapshot._artifact_bindings.items()}
            snapshot.dependencies = dict(slot.snapshot.dependencies)
            snapshot.dependency_covered = True
            snapshot.revalidate_dependencies()
            for path, (raw, _identity) in slot.snapshot.records.items():
                _read_bound(replace(source, bytes_by_path=snapshot),
                    {"path": path, "sha256": sha256_bytes(raw), "bytes": len(raw)})
            for (path, _operation), (access, _identity) in slot.snapshot.metadata_records.items():
                snapshot.record_metadata(Path(path), access)
            if snapshot.records != slot.snapshot.records or snapshot.metadata_records != slot.snapshot.metadata_records:
                raise ControlError("材料消费者完整读集、精确绑定或元数据实例漂移")
            checked_source = replace(source, bytes_by_path=snapshot)
            _successor_state(store, configuration, slot.projection.successor.facts, checked_source)
            successor = parse_json_bytes(_read_bound(checked_source, slot.projection.successor.successor_materials_binding))
            from tools.official_client_capture.claude_gate_material_worker import verify_reader_identity
            verify_reader_identity(parse_json_bytes(_read_bound(checked_source, successor["reader_identity_binding"])))
            snapshot.finish(force_read=False)
        slot.sources[source_key] = source
        return replace(slot.projection, successor=replace(slot.projection.successor, source=source))

    def finish_phase(self) -> None:
        """所有已取得槽均走原完整体；失败也不可继续授予复用结果。"""
        failures = []
        self.finishing = True
        try:
            for purpose, slot in tuple(self.slots.items()):
                try:
                    source = next(iter(slot.sources.values()))
                    snapshots = []
                    # 即使前序消费者或快速复核抛错，也实际进入原完整体。
                    self.byte_nonce = object()
                    current = _validate_material_successor(self.store, self.selected["reference"], self.configuration,
                        source, purpose=purpose, plan_ref=self.plan_ref, pending_payload=None, _snapshot_sink=snapshots)
                    # 后续来源闭包可增加已存在的独立报告坐标；原语义依赖全集仍逐项重查。
                    slot.snapshot.revalidate_dependencies()
                    if (_successor_projection_signature(slot.projection) != slot.signature
                            or _successor_projection_signature(current) != slot.signature
                            or snapshots[0].records != slot.snapshot.records
                            or snapshots[0].metadata_records != slot.snapshot.metadata_records):
                        raise ControlError("材料消费者完整尾验的投影、原件实例或目录负向探测漂移")
                    for source in slot.sources.values():
                        self.check_source(source)
                except BaseException as error:
                    failures.append(error)
            try:
                self.check(self.store, self.configuration, self.plan_ref)
            except BaseException as error:
                failures.append(error)
            if failures:
                self.invalid = True
                if len(failures) == 1:
                    raise failures[0]
                raise BaseExceptionGroup("材料消费者多个完整尾验失败", failures)
        finally:
            self.finishing = False
            self.slots.clear()

    def clear_bytes(self) -> None:
        """结束本段字节能力时同时释放原字节，来源句柄不能延长缓存生命周期。"""
        for capability in self.providers.values():
            capability[0]._byte_records.clear()
        for capability in self.joined.values():
            capability[1]._byte_records.clear()


@contextmanager
def _material_successor_consumer_scope(store: Any, configuration: Mapping[str, Any], *,
                                       plan_ref: Mapping[str, Any], delivery_lock_fd: int | None):
    """只围住真实持锁的只读消费者；准备和 Store 写入继续原合同。"""
    lock = _SUCCESSOR_DELIVERY_LOCK.get()
    if lock is None:
        if delivery_lock_fd is not None:
            raise ControlError("材料消费者 FD 没有编排器真实活动交付锁能力")
        yield
        return
    lock.check()
    if delivery_lock_fd is not None and delivery_lock_fd != lock.descriptor:
        raise ControlError("材料消费者 FD 不属于同一交付锁")
    selected = describe_material_successor_bindings(store, configuration, plan_ref=plan_ref)
    if selected is None:
        yield
        return
    facts = selected["facts"]
    if (facts["campaign_id"] != lock.campaign_id or facts["candidate_id"] != lock.candidate_id
            or facts["execution_context"]["data_root"] != str(lock.data_root)
            or facts["plan_ref"] != dict(plan_ref)
            or facts["configuration_sha256"] != canonical_sha256(configuration)):
        raise ControlError("材料消费者交付锁与原 Plan、根、配置或有效关联不同")
    previous = _SUCCESSOR_CONSUMER.get()
    if previous is not None:
        previous.check(store, configuration, plan_ref)
        try:
            yield
        except BaseException:
            previous.invalid = True
            raise
        return
    # 配置用独立 JSON 值保留，调用方更改原 Mapping 不能改写比较基准。
    scope = _SuccessorConsumer(lock, store, json.loads(json.dumps(configuration)), dict(plan_ref),
        selected, _successor_store_metadata(store.root), lock.nonce)
    handle = _SUCCESSOR_CONSUMER.set(scope)
    original_error = None
    try:
        try:
            yield
        except BaseException as error:
            original_error = error
            scope.invalid = True
        try:
            scope.finish_phase()
        except BaseException as tail_error:
            if original_error is not None:
                raise original_error from tail_error
            raise
        if original_error is not None:
            raise original_error
    finally:
        scope.active = False
        scope.invalid = True
        scope.slots.clear()
        scope.clear_bytes()
        scope.providers.clear()
        scope.joined.clear()
        _SUCCESSOR_CONSUMER.reset(handle)


def _register_material_successor_provider(executor: Any, originals: Any, state: Mapping[str, Any] | None) -> None:
    """只有实际产品实例与已核通道原件能登记本段来源能力。"""
    scope = _SUCCESSOR_CONSUMER.get()
    if scope is None:
        return
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import Arm64DeliveryExecutor, _ProducerOriginals
    # 明确回归执行器可继承现场测量接缝，producer 鉴权方法必须仍是固定产品实现。
    fixture = (isinstance(executor, Arm64DeliveryExecutor) and executor.configuration["source_scope"] == "regression_fixture"
        and all(getattr(getattr(executor, name), "__func__", None) is getattr(Arm64DeliveryExecutor, name)
            for name in ("_producer_state", "_producer_record", "_status", "_identity")))
    if (type(executor) is not Arm64DeliveryExecutor and not fixture) or type(originals) is not _ProducerOriginals:
        raise ControlError("材料消费者来源登记不能由任意对象或 JSON 授权")
    scope.check(scope.store, executor.configuration, scope.plan_ref)
    context = {key: executor.context[key] for key in scope.selected["facts"]["execution_context"]}
    if (executor.data_root != scope.lock.data_root or originals.root != executor.data_root
            or context != scope.selected["facts"]["execution_context"]):
        raise ControlError("材料消费者 producer 不属于同一原配置与稳定执行上下文")
    expected = (originals, executor, None if state is None else _successor_producer_signature(state), scope.phase_nonce)
    previous = scope.providers.get(id(originals))
    if previous is not None and (previous[0] is not originals or previous[1] is not executor
            or (previous[2] is not None and previous[2] != expected[2])):
        raise ControlError("材料消费者已登记 producer 能力漂移")
    scope.providers[id(originals)] = expected


def _register_material_successor_joined(admission: Any, joined: Any) -> None:
    scope = _SUCCESSOR_CONSUMER.get()
    if scope is None:
        return
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import (
        VerifiedCompletedDeliveryAdmission, _COMPLETED_ADMISSION_TOKEN, _JoinedCompletedOriginals, _ProducerOriginals,
    )
    if (type(admission) is not VerifiedCompletedDeliveryAdmission or admission.token is not _COMPLETED_ADMISSION_TOKEN
            or type(joined) is not _JoinedCompletedOriginals or joined.current is not admission._originals
            or type(joined.current) is not _ProducerOriginals
            or admission.plan_ref != scope.plan_ref or admission.successor_ref != scope.selected["reference"]):
        raise ControlError("材料消费者合并原件缺同一真实完成准入 token 与精确 producer")
    # 父发布来源的产品 wrapper 沿原完整路径；没有精确 producer 能力只是不复用。
    if type(joined.producer) is not _ProducerOriginals or id(joined.producer) not in scope.providers:
        return
    # 写相位会使旧能力过期；这里只保留产品 owner 线索，并实际重新鉴权后登记。
    capability = scope.providers[id(joined.producer)]
    if capability[3] is not scope.phase_nonce:
        executor = capability[1]
        _register_material_successor_provider(executor, joined.producer, executor._producer_state(joined.producer))
    scope.joined[id(joined)] = (joined, joined.current, scope.phase_nonce)


def _material_successor_byte_nonce(originals: Any) -> object | None:
    """只为真实 owner 登记的本段原件供给字节接缝，不授材料投影权限。"""
    scope = _SUCCESSOR_CONSUMER.get()
    if scope is None:
        return None
    scope.lock.check()
    if (not scope.active or (not scope.reading and not scope.finishing)
            or (scope.invalid and not scope.finishing)):
        return None
    capability = scope.providers.get(id(originals))
    if capability is not None and capability[0] is originals and capability[3] is scope.phase_nonce:
        return scope.byte_nonce
    if any(value[1] is originals and value[2] is scope.phase_nonce for value in scope.joined.values()):
        return scope.byte_nonce
    return None


def _material_successor_provider_store(executor: Any, root: Path) -> Any | None:
    """固定 provider 的同根取件复用原 Store；runtime 独立句柄不经过此接缝。"""
    scope = _SUCCESSOR_CONSUMER.get()
    if scope is None:
        return None
    scope.check(scope.store, executor.configuration, scope.plan_ref)
    if root != scope.store.root or executor.data_root != scope.lock.data_root:
        raise ControlError("材料 provider 的 Store、根或配置漂移")
    return scope.store


@contextmanager
def _material_successor_write_phase():
    """诊断与报告落盘前完成全部尾验；写相位内没有语义复用能力。"""
    scope = _SUCCESSOR_CONSUMER.get()
    if scope is None:
        yield
        return
    scope.finish_phase()
    scope.clear_bytes()
    scope.phase_nonce = object()
    scope.byte_nonce = object()
    scope.joined.clear()
    scope.reading = False
    handle = _SUCCESSOR_CONSUMER.set(None)
    try:
        yield
    except BaseException:
        scope.invalid = True
        raise
    finally:
        scope.reading = True
        _SUCCESSOR_CONSUMER.reset(handle)


@dataclass(frozen=True)
class VerifiedMaterialSuccessor:
    """只有纯验证器构造；保存本次来源及用途，JSON 不会取得消费权。"""
    reference: Mapping[str, Any]
    facts: Mapping[str, Any]
    object_binding: Mapping[str, Any]
    predecessor_materials_binding: Mapping[str, Any]
    successor_materials_binding: Mapping[str, Any]
    predecessor_archive_bindings: tuple[Mapping[str, Any], ...]
    current_archive_bindings: tuple[Mapping[str, Any], ...]
    purpose: str
    store: Any
    configuration: Mapping[str, Any]
    source: Any
    token: object

    def revalidate(self, source: Any, *, purpose: str | None = None) -> Any:
        from .sealed_gate_bindings import _TOKEN
        if self.token is not _TOKEN or source.trusted_data_root != self.source.trusted_data_root or source.configuration_sha256 != self.source.configuration_sha256 or source.service_instance_id != self.source.service_instance_id:
            raise ControlError("材料后继投影跨 Store、来源或原配置消费")
        return validate_material_successor(self.store, self.reference, self.configuration, source,
            purpose=purpose or self.purpose, plan_ref=self.facts["plan_ref"])


def validate_material_successor(store: Any, successor_ref: Mapping[str, Any], configuration: Mapping[str, Any], source: Any, *, purpose: str, plan_ref: Mapping[str, Any] | None = None) -> Any:
    """前后实读完整合同；前驱不运行旧 reader，后继必须实用当前 reader。"""
    scope = _SUCCESSOR_CONSUMER.get()
    if scope is not None and not scope.finishing and purpose in {"formed_replay", "current_delivery"}:
        try:
            scope.check(scope.store, configuration, plan_ref)
            if store.root != scope.store.root:
                raise ControlError("材料消费者不能跨原 Store 根继续验证")
            # runtime 会另开同根只读句柄；它没有本实例的语义复用权限。
            if store is scope.store and scope.has_source_capability(source):
                return scope.read(store, successor_ref, configuration, source, purpose=purpose, plan_ref=plan_ref)
        except BaseException:
            scope.invalid = True
            raise
    return _validate_material_successor(store, successor_ref, configuration, source, purpose=purpose, plan_ref=plan_ref, pending_payload=None)


def _validate_material_successor(store: Any, successor_ref: Mapping[str, Any], configuration: Mapping[str, Any], source: Any, *, purpose: str, plan_ref: Mapping[str, Any] | None, pending_payload: Mapping[str, Any] | None, _snapshot_sink: Any = None) -> Any:
    """prepare 的未封存预检结果只在本调用内丢弃，公开验证仍要求实际 Store 原件。"""
    from .delivery_execution import VerifiedSource, _revalidate_guard_continuity
    from .sealed_gate_bindings import VerifiedGateProjection, _TOKEN
    if not isinstance(source, VerifiedSource) or source.trusted_data_root is None or purpose not in {"current_import", "current_delivery", "formed_replay"}:
        raise ControlError("材料后继消费缺既有真实来源、实际根或明确用途")
    scope = _SUCCESSOR_CONSUMER.get()
    if scope is not None:
        # 每次原完整体都开启新的读取段，包括同根 runtime 的独立 Store 句柄。
        scope.byte_nonce = object()
    selected = describe_material_successor_bindings(store, configuration, plan_ref=plan_ref)
    pending = pending_payload is not None
    if pending:
        if selected is not None:
            raise ControlError("材料后继已有完整关联，不能另建不同预检世界")
        from .canonical import canonical_json_bytes
        document = {"schema_version": "official-client-control-object/v1", "object_kind": "operational_evidence", "payload": dict(pending_payload)}
        raw = canonical_json_bytes(document)
        if successor_ref != {"object_kind": "operational_evidence", "sha256": sha256_bytes(raw)}:
            raise ControlError("材料后继准备对象引用与精确原字节不同")
        path = store.object_path(dict(successor_ref))
        selected = {"reference": dict(successor_ref), "facts": dict(pending_payload["entries"][0]["facts"]), "object_binding": {"path": path.relative_to(source.trusted_data_root).as_posix(), "sha256": sha256_bytes(raw), "bytes": len(raw)}}
    elif selected is None or selected["reference"] != dict(successor_ref):
        raise ControlError("材料后继不是该原配置与有效登记的唯一已封存关联")
    snapshot = _SuccessorReadSnapshot(source)
    if _snapshot_sink is not None and not pending:
        snapshot.capture_dependencies(configuration, selected["facts"])
    checked_source = replace(source, bytes_by_path=snapshot)
    if pending:
        object_document = document
    else:
        object_raw = _read_bound(checked_source, selected["object_binding"])
        object_document = parse_json_bytes(object_raw)
        if object_document != store.load_object(dict(successor_ref)):
            raise ControlError("材料后继关联不是 Store 实际文档原字节")
    _payload, facts = _successor_payload(object_document["payload"])
    _revalidate_guard_continuity(source, required=configuration["source_scope"] == "formal")
    plan, roles, chain = _successor_state(store, configuration, facts, checked_source)
    if object_document["payload"]["persona"] != plan["persona"]:
        raise ControlError("材料后继容器不属于原 Plan 的 Persona")
    predecessor = validate_import_shape(parse_json_bytes(_read_bound(checked_source, facts["predecessor_materials_binding"])))
    successor = validate_import_shape(parse_json_bytes(_read_bound(checked_source, facts["successor_materials_binding"])))
    if _fixed_import_acquisitions(predecessor, checked_source) != _fixed_import_acquisitions(successor, checked_source):
        raise ControlError("材料后继来源形成期、精确允许范围或缺失探测改变")
    if predecessor["import_id"] == successor["import_id"] or _logical_material(predecessor) != _logical_material(successor) or predecessor["source_origin"]["source_scope"] != successor["source_origin"]["source_scope"] or successor["source_origin"]["source_scope"] != configuration["source_scope"]:
        raise ControlError("材料后继不是相同原材料、完整读集和模式的独立新证明")
    old_reader = _validate_historical_reader(parse_json_bytes(_read_bound(checked_source, predecessor["reader_identity_binding"])))
    new_reader = parse_json_bytes(_read_bound(checked_source, successor["reader_identity_binding"]))
    if old_reader["identity_sha256"] == new_reader.get("identity_sha256") or new_reader.get("tool_identity") != chain[-1][1]["to_identity"]:
        raise ControlError("材料后继没有真正新 reader 或不属于当前有效七身份")
    old_acquisition = parse_json_bytes(_read_bound(checked_source, predecessor["source_acquisition_bindings"][0]["binding"]))
    old_source = replace(checked_source, deployment_sha256=old_acquisition["source_deployment_binding"]["sha256"])
    predecessor_projection = _validate_material_import(store, facts["predecessor_materials_binding"], old_source, roles,
        purpose="formed_replay", expected_source_scope=configuration["source_scope"], historical_anchor=True)
    for access in predecessor["reader_accesses"]:
        if access["type"] not in {"directory", "missing"}:
            continue
        logical = Path(access["logical_path"])
        matches = [row for row in predecessor["roots"] if logical.is_relative_to(Path(row["source_locator"]))]
        if len(matches) > 1:
            raise ControlError("前驱材料元数据逻辑根存在重叠")
        if matches:
            root = matches[0]
            physical = source.trusted_data_root / root["archive_root_relative"] / logical.relative_to(Path(root["source_locator"]))
            snapshot.record_metadata(physical, access)
    control_raw = _read_bound(checked_source, facts["control_deployment_binding"])
    registration_binding = _store_binding(store, source.trusted_data_root, chain[-1][0])
    _read_bound(checked_source, registration_binding)
    evolution_raw = _read_bound(checked_source, facts["evolution_receipt_binding"])
    _validate_successor_registration(store, facts, chain, old_reader, new_reader, control_raw, evolution_raw,
        predecessor_projection, checked_source, roles)
    new_source = replace(checked_source, deployment_sha256=facts["control_deployment_binding"]["sha256"])
    successor_projection = validate_qualification_material_import(store, facts["successor_materials_binding"], new_source, roles,
        purpose="current_import" if purpose == "current_import" else "current_delivery", expected_source_scope=configuration["source_scope"])
    additions = [selected["object_binding"], registration_binding, facts["evolution_receipt_binding"], facts["control_deployment_binding"]]
    merged = {}
    actual_reads = [{"path": path, "sha256": sha256_bytes(raw), "bytes": len(raw)} for path, (raw, _identity) in snapshot.records.items()]
    for row in [*predecessor_projection.archive_bindings, *successor_projection.archive_bindings, *additions, *actual_reads]:
        previous = merged.setdefault(row["path"], dict(row))
        if previous != dict(row):
            raise ControlError("材料后继当前归档闭包存在同坐标冲突")
    archive = tuple(merged[path] for path in sorted(merged))
    memory = VerifiedMaterialSuccessor(dict(successor_ref), dict(facts), selected["object_binding"], facts["predecessor_materials_binding"], facts["successor_materials_binding"], predecessor_projection.archive_bindings, archive, purpose, store, dict(configuration), source, _TOKEN)
    # 旧 proof 与旧 reader 的 JSON 只投影到其已核原归档闭包；不展开旧代码描述。
    historical = ((dict(facts["predecessor_materials_binding"]), tuple(predecessor_projection.archive_bindings)),
        (dict(predecessor["reader_identity_binding"]), ()),
        (dict(facts["successor_materials_binding"]), tuple(successor_projection.archive_bindings)),
        (dict(successor["reader_identity_binding"]), ()))
    result = VerifiedGateProjection(predecessor_projection.parents, predecessor_projection.domains, archive,
        tuple([*predecessor_projection.material_files, *successor_projection.material_files]), _TOKEN,
        predecessor_projection.subject_projections, successor_projection.reader_replays, memory, historical)
    snapshot.finish()
    expected_selection = None if pending else selected
    if describe_material_successor_bindings(store, configuration, plan_ref=plan_ref) != expected_selection or _successor_state(store, configuration, facts, checked_source)[2] != chain:
        raise ControlError("材料后继返回前关联、原事实或有效工具头漂移")
    _revalidate_guard_continuity(source, required=configuration["source_scope"] == "formal")
    if _snapshot_sink is not None and not pending:
        _snapshot_sink.append(snapshot)
    return None if pending else result


def resolve_material_successor(store: Any, configuration: Mapping[str, Any], source: Any, *, purpose: str, plan_ref: Mapping[str, Any] | None = None) -> Any:
    selected = describe_material_successor_bindings(store, configuration, plan_ref=plan_ref)
    if selected is None:
        return None
    return validate_material_successor(store, selected["reference"], configuration, source, purpose=purpose, plan_ref=plan_ref)
