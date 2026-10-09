"""门禁材料导入/acquisition 的纯闭集验证和真实复制视图重放。

输入 source 须先经既有通道或受管 guard 本地匹配验真。本层只消费
明确资格四根及导入原件，不能取得上层 Plan/Package/Receipt 权限。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
from typing import Any, Mapping

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
        expected = qualification_gate_subject(store, q, source.trusted_data_root, sealed,
            read_bindings=subject_bindings, read_projections=subject_projections)
        for binding in subject_bindings:
            # 对照来源能力的精确原字节，回传既有归档列表；形成期重放
            # 不得用当前磁盘的同名材料掩盖缺失或不同的历史原件。
            _read_bound(source, binding)
            all_bindings.append(binding)
        if canonical_sha256(expected) != domain["gate_subject_sha256"]:
            raise ControlError("门禁完整 subject 不是四根与原父推出的身份")
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


def projection_for_configuration(store: Any, configuration: Mapping[str, Any], source: Any, *, purpose: str) -> Any:
    """只有已经验真的同一冻结配置能选择本次第二根证明。"""
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import validate_configuration, MATERIAL_CONFIGURATION_SCHEMA
    validate_configuration(dict(configuration))
    if configuration.get("schema_version") != MATERIAL_CONFIGURATION_SCHEMA:
        return None
    if canonical_sha256(configuration) != source.configuration_sha256:
        raise ControlError("门禁投影配置摘要不是已验真的同一来源配置")
    return validate_qualification_material_import(store, configuration["qualification_materials_binding"], source,
        {name: configuration[name]["qualification"] for name in ("candidate", "rollback")}, purpose=purpose,
        expected_source_scope=configuration["source_scope"])
