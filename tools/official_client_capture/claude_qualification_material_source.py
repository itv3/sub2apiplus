"""Claude 封存门禁的来源取得合同。

材料里的绝对路径只能和预先取得的受管坐标比较，不能授予新读取权。
正式 factory 从固定安装、参数和 VC-5/VC-4/Store 链取得权限；夹具必须
在局部登记中明确提供独立来源，不得把 fixture 改名为 formal。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import platform
import stat
from typing import Any, Callable, Mapping

from tools.official_client_control.canonical import canonical_sha256, parse_json_bytes, sha256_bytes
from tools.official_client_control.errors import ControlError
from .claude_gate_material_worker import VerifiedReaderIdentity, compute_gate_reader_identity, _plain_file


ACQUISITION_SCHEMA = "claude-qualification-source-acquisition/v1"
FORMAL_AUTHORITY = "claude-arm64-qualified-intake"
FIXTURE_AUTHORITY = "claude-regression-qualified-intake"
PURPOSES = frozenset({"current_import", "current_delivery", "formed_replay"})
ROOT_KINDS = frozenset({"gate_evidence", "unit_records", "unit_sources", "read_audit_materials"})
ROOT_FIELDS = ("candidate_ref", "build_receipt_ref", "validation_ref", "acceptance_ref")
_TOKEN = object()


def managed_driver_module(name: str, *, current: bool):
    """只选择固定安装源码；形成期也不能靠仓库根外模块替换冻结合同。"""
    import importlib.util, sys
    if name not in {"install", "parse_env", "vc5_admission", "background_validation"}:
        raise ControlError("受管驱动合同模块未登记")
    target = Path("/root/arm64-capture-driver")
    relative = name + ".py" if name == "install" else "driver/" + name + ".py"
    path = target / relative
    if path.exists() or path.is_symlink():
        raw, metadata = _plain_file(path)
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ControlError("固定驱动模块不是 root 私有源码原件")
        manifest_raw, _ = _plain_file(target / "manifest.json")
        manifest = parse_json_bytes(manifest_raw)
        rows = [row for row in manifest.get("files", []) if row.get("path") == relative]
        if len(rows) != 1 or rows[0]["sha256"] != sha256_bytes(raw):
            raise ControlError("固定驱动模块没有匹配实际安装清单")
        key = "_claude_qualification_driver_" + name
        module = sys.modules.get(key)
        if module is None:
            specification = importlib.util.spec_from_file_location(key, path)
            module = importlib.util.module_from_spec(specification)
            # 使用刚核清单的真实源码字节，不让 SourceFileLoader 消费
            # 未核 __pycache__；加载结束再查源坐标，拒绝执行期间漂移。
            exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
            if _plain_file(path)[0] != raw:
                raise ControlError("固定驱动源码在合同加载期间漂移")
            module._qualification_loaded_source_sha256 = sha256_bytes(raw)
            module._qualification_loaded_source_token = _TOKEN
            sys.modules[key] = module
        elif (Path(module.__file__) != path
              or getattr(module, "_qualification_loaded_source_token", None) is not _TOKEN
              or getattr(module, "_qualification_loaded_source_sha256", None) != sha256_bytes(raw)):
            raise ControlError("固定驱动模块缓存不是同坐标的已核原源码")
        return module
    raise ControlError("formal_intake_not_ready：缺固定驱动安装的纯合同模块；形成期不得替换为仓库实现")


@dataclass(frozen=True)
class RootCapability:
    root_id: str
    root_kind: str
    logical_root: Path
    physical_root: Path
    allowed_relative_paths: tuple[str, ...]
    missing_relative_paths: tuple[str, ...]
    reference_id: str
    reference_sha256: str
    token: object


@dataclass(frozen=True)
class SourceOriginal:
    """准备阶段已实读的来源原件；复制前必须重验同一坐标的全部字节。"""
    name: str
    path: Path
    raw: bytes


@dataclass(frozen=True)
class VerifiedIntakeContext:
    authority: str
    source_scope: str
    data_root: Path
    repository_root: Path
    provenance: Mapping[str, Any]
    roots: tuple[RootCapability, ...]
    reader_identity: VerifiedReaderIdentity
    token: object
    source_originals: tuple[SourceOriginal, ...] = ()


@dataclass(frozen=True)
class VerifiedQualificationSource:
    intake: VerifiedIntakeContext
    role: str
    qualification: Mapping[str, Any]
    parent_binding: Mapping[str, Any]
    parent: Mapping[str, Any]
    expected_subject: Mapping[str, Any]
    gate_root_id: str | None
    receipt_relative: str | None
    entry_references: Mapping[str, Any]
    formed_context: Any
    token: object


def _private_root(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise ControlError("来源根必须是规范绝对路径")
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ControlError("来源根或祖先含符号链接")
    metadata = root.stat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.geteuid():
        raise ControlError("来源根必须是当前受管用户的私有 0700 目录")
    return root


def _relative(value: str) -> str:
    from tools.official_client_control.canonical import validate_relative_path
    return validate_relative_path(value, "qualification.source_relative_path")


def freeze_root_capability(root: Path, kind: str, *, authority: str, reference_id: str,
                           reference_sha256: str, allowed_relative_paths: list[str],
                           missing_relative_paths: list[str] | None = None) -> RootCapability:
    """由已验 factory 冻结精确目录范围；此对象不会从 JSON 反序列化。"""
    root = _private_root(root)
    if kind not in ROOT_KINDS or authority not in {FORMAL_AUTHORITY, FIXTURE_AUTHORITY}:
        raise ControlError("来源根类型或 authority 非法")
    allowed = tuple(sorted({_relative(value) for value in allowed_relative_paths}))
    missing = tuple(sorted({_relative(value) for value in (missing_relative_paths or [])}))
    if set(allowed) & set(missing) or len(allowed) > 20000:
        raise ControlError("来源范围冲突或超出固定上限")
    total = 0
    for relative in allowed:
        raw, metadata = _plain_file(root / relative)
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ControlError("门禁来源原件不是私有文件")
        total += len(raw)
    if total > 128 * 1024 * 1024:
        raise ControlError("来源材料总长度超过固定上限")
    for relative in missing:
        if (root / relative).exists() or (root / relative).is_symlink():
            raise ControlError("来源负向探测坐标并非确实缺失")
    root_id = canonical_sha256({"authority": authority, "reference_id": reference_id,
                                "reference_sha256": reference_sha256, "source_locator": str(root), "root_kind": kind})
    return RootCapability(root_id, kind, root, root, allowed, missing, reference_id, reference_sha256, _TOKEN)


def qualification_roots(qualification: Mapping[str, Any]) -> dict[str, Any]:
    from .claude_upgrade_arm64_delivery_executor import _qualification_shape
    _qualification_shape(dict(qualification), "qualification.source")
    return {key: qualification[key] for key in ROOT_FIELDS}


def _check_four_roots(store: Any, qualification: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """只验资格四根的原合同；不回放 Plan、Package、交付收据或本轮 VC-6。"""
    from tools.official_client_control.receipts import replay_receipt
    q = dict(qualification)
    qualification_roots(q)
    replay_receipt(store, q["build_receipt_ref"])
    candidate = store.load_fact(q["candidate_ref"])
    validation = store.load_fact(q["validation_ref"])
    acceptance = store.load_fact(q["acceptance_ref"])
    for document, kind in ((candidate, "candidate_frozen"), (validation, "validation_completed"), (acceptance, "acceptance_recorded")):
        if document["fact_kind"] != kind or document["campaign_id"] != q["campaign_id"] or document["payload"].get("candidate_id") != q["candidate_id"]:
            raise ControlError("门禁来源资格四根跨候选或跨 Campaign")
    required = {"candidate_ref": q["candidate_ref"], "candidate_build_receipt_ref": q["build_receipt_ref"], "acceptance_ref": q["acceptance_ref"]}
    if validation["payload"].get("result") != "passed" or any(validation["payload"].get(key) != value for key, value in required.items()):
        raise ControlError("门禁来源缺完整已通过的资格同源链")
    if candidate["payload"].get("candidate_build_receipt_ref") != q["build_receipt_ref"] or any(acceptance["payload"].get(key) != value for key, value in required.items() if key != "acceptance_ref"):
        raise ControlError("门禁来源候选或验收没有绑定对应 Build")
    return candidate, acceptance


def replay_qualification_entry(store: Any, campaign_root: Path, qualification: Mapping[str, Any],
                               entry_references: Mapping[str, Any], *, formed_context: Any) -> dict[str, Any]:
    """精确历史 revision 的 VC-5→accept→VC-4，同一规则用于 candidate 和 rollback。"""
    from . import claude_upgrade_delivery as delivery
    from . import claude_upgrade_acceptance as acceptance
    from . import claude_upgrade_vc as vc
    _check_four_roots(store, qualification)
    manifest = delivery._orchestrator()._require_formal_campaign(campaign_root)
    path = delivery._campaign_path(campaign_root, entry_references["accept_result"]["path"], "来源 accept 原件")
    result = acceptance.load_stage_result(campaign_root, path, qualification["candidate_id"], manifest=manifest)
    revision = result["candidate_revision"]
    if formed_context is not None and formed_context.get("candidate_revision", revision) != revision:
        raise ControlError("来源形成期 revision 与精确 accept 原件不同")
    handoff = {"validation_completed_ref": qualification["validation_ref"], "acceptance_ref": qualification["acceptance_ref"],
               "candidate_evidence_package_ref": result["store"]["candidate_evidence_package_ref"],
               "candidate_image_digest": store.load_fact(qualification["candidate_ref"])["payload"]["image_digest"]}
    replayed = delivery._vc5_entry_replay(campaign_root, manifest, qualification["candidate_id"], revision, handoff)
    if replayed != dict(entry_references) or vc.vc_control_binding(campaign_root, path) != entry_references["accept_result"]:
        raise ControlError("来源形成期 entry 不是精确 VC-5/VC-4 资格链")
    return result


def _descriptor(path: Path) -> dict[str, Any]:
    raw, _metadata = _plain_file(path)
    return {"absolute_path": str(path), "sha256": sha256_bytes(raw), "bytes": len(raw)}


def _managed_admission_originals(config: Mapping[str, str], qualification: Mapping[str, Any]) -> tuple[SourceOriginal, ...]:
    """历史材料只核公开准入原件；不调用 consume，不读 token 或签发配置。"""
    admission = managed_driver_module("vc5_admission", current=True)
    root = _private_root(Path(config["RUNROOT"]))
    receipt_path, effective_path = root / "vc5-admission.json", root / "vc5-effective-parameters.json"
    receipt_raw, _ = _plain_file(receipt_path)
    effective_raw, _ = _plain_file(effective_path)
    receipt, effective = parse_json_bytes(receipt_raw), parse_json_bytes(effective_raw)
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    bindings = receipt.get("bindings") or {}
    if receipt.get("schema_version") != "official-client-vc5-admission/v1" or receipt.get("status") != "ready" or admission.digest(unsigned) != receipt.get("receipt_sha256") or receipt.get("admission_key") != admission.digest(bindings):
        raise ControlError("formal_intake_not_ready：公开 VC-5 准入原件未通过")
    if bindings.get("campaign") != qualification["campaign_id"] or bindings.get("candidate") != qualification["candidate_id"] or receipt.get("effective_parameters") != effective or bindings.get("effective_parameters_sha256") != admission.digest(effective):
        raise ControlError("formal_intake_not_ready：公开准入 Campaign、候选或有效参数错配")
    approval = receipt.get("approval_sha256")
    if not isinstance(approval, str) or len(approval) != 64 or any(char not in "0123456789abcdef" for char in approval):
        raise ControlError("formal_intake_not_ready：准入批准摘要非法")
    event_path = root / "vc5-admission-events" / (approval + ".json")
    event_raw, _ = _plain_file(event_path)
    event = parse_json_bytes(event_raw)
    if event.get("status") != "passed" or event.get("receipt_sha256") != receipt["receipt_sha256"] or event.get("approval_sha256") != approval or event.get("admission_key") != receipt["admission_key"] or event.get("approval", {}).get("seal_admission") is not True:
        raise ControlError("formal_intake_not_ready：准入通过事件或明确批准缺失")
    return (SourceOriginal("admission", receipt_path, receipt_raw), SourceOriginal("effective_parameters", effective_path, effective_raw), SourceOriginal("admission_event", event_path, event_raw))


def managed_source_context(data_root: Path, parameter_path: Path, qualification: Mapping[str, Any],
                           entry_references: Mapping[str, Any], *, formed_context: Any) -> tuple[dict[str, Any], tuple[SourceOriginal, ...]]:
    """按已验历史 entry 和原参数规则生成可审核的精确来源范围。

    rollback 使用它自己的参数及 accept 内冻结的 revision/baseline；从不
    调当前最大目录号，也不把 parent 里的根字符串作为授权来源。
    """
    parse_env = managed_driver_module("parse_env", current=True)
    from tools.official_client_control.store import ControlStore
    raw, _ = _plain_file(parameter_path)
    config = parse_env.parse(raw.decode("utf-8"))
    config.update(parse_env.derive(config))
    if config.get("CLIENT") != "claude" or Path(config["D"]) != data_root or config["NEW"] != qualification["campaign_id"] or config["CAND"] != qualification["candidate_id"] or parameter_path.parent != Path(config["RUNROOT"]):
        raise ControlError("formal_intake_not_ready：原参数不属于精确来源资格或固定 RUNROOT")
    store = ControlStore.open_read_only(data_root / qualification["store_relative"])
    campaign = data_root / "evidence/campaigns" / qualification["campaign_id"]
    parent = replay_qualification_entry(store, campaign, qualification, entry_references, formed_context=formed_context)
    gate_root = Path(config["G"]) / qualification["candidate_id"] / ("b" + str(parent["evaluation_baseline"]))
    if parent.get("external_gate", {}).get("evidence_root") != str(gate_root):
        raise ControlError("formal_intake_not_ready：历史 accept 门禁根不是原参数规则重算的根")
    roots = [("gate_evidence", gate_root)]
    # 只有参数里明确选择的辅助布局可进入待审核范围。旧 receipt 的任意
    # 绝对引用不能新增根；读阶段引用不在这些范围内仍然致命拒绝。
    for key, kind in (("VC5_TARGET_RECORD_STORE", "unit_records"), ("VC5_TARGET_REQUEST", "unit_sources")):
        if key in config:
            root = Path(config[key]).parent if key == "VC5_TARGET_REQUEST" else Path(config[key])
            roots.append((kind, root))
    unique = {}
    for kind, root in roots:
        _private_root(root)
        unique.setdefault(str(root), (kind, root))
    if any(left != right and (left.is_relative_to(right) or right.is_relative_to(left)) for _kind, left in unique.values() for _other, right in unique.values()):
        raise ControlError("formal_intake_not_ready：来源根范围重叠，需在执行门禁前使用分离的受管辅助布局")
    layout = []
    for kind, root in sorted(unique.values(), key=lambda row: str(row[1])):
        members = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ControlError("formal_intake_not_ready：来源范围含符号链接")
            if path.is_file():
                # 请求目录只列公开 JSON；凭据不因此变成归档材料。
                if kind == "unit_sources" and path.suffix != ".json":
                    continue
                members.append(path.relative_to(root).as_posix())
        if len(members) > 20000:
            raise ControlError("formal_intake_not_ready：来源范围超过固定上限")
        layout.append({"root_kind": kind, "source_locator": str(root), "allowed_relative_paths": members})
    originals = (SourceOriginal("parameter", parameter_path, raw), *_managed_admission_originals(config, qualification))
    context = {"schema_version": "claude-qualification-intake-context/v1", "client": "claude", "host_id": "ARM64",
        "data_root": str(data_root), "campaign_id": qualification["campaign_id"], "candidate_id": qualification["candidate_id"],
        "candidate_revision": parent["candidate_revision"], "evaluation_baseline": parent["evaluation_baseline"],
        "qualification_roots": qualification_roots(qualification), "entry_references": dict(entry_references),
        "parameter_descriptor": _descriptor(parameter_path), "admission_descriptors": [_descriptor(item.path) for item in originals[1:]],
        "source_layout": layout}
    return context, originals


def build_managed_qualification_intake_context(installed_session: Mapping[str, Any],
                                              public_source_context: Mapping[str, Any]) -> VerifiedIntakeContext:
    """正式 factory：核安装、精确形成期链及审核过的来源范围后才取得能力。"""
    if platform.system() != "Linux" or platform.machine().lower() not in {"arm64", "aarch64"} or os.geteuid() != 0:
        raise ControlError("formal_intake_not_ready：必须在 ARM64 的 root 受管安装内准备")
    install = managed_driver_module("install", current=True)
    latest_deployment = managed_driver_module("background_validation", current=True).latest_deployment
    from tools.official_client_control.contracts import TOOL_IDENTITY_FIELDS
    from . import claude_tool_identity_policy as policy_module
    fields = {"target", "data_root", "parameter_path", "public_context_binding", "approval_binding", "runtime_manifest", "tool_identity", "formed_context", "qualification", "entry_references"}
    if set(installed_session) != fields or installed_session["target"] != "/root/arm64-capture-driver":
        raise ControlError("formal_intake_not_ready：安装 session 不闭合或不是固定安装目标")
    target, data_root = Path(installed_session["target"]), _private_root(Path(installed_session["data_root"]))
    manifest = install.load_manifest(target)
    install.verify_tree(target, manifest, check_mode=True, require_root_owner=True)
    installed = install.verify_install(target, data_root, "claude")
    required_driver = {"driver/parse_env.py", "driver/round_context.py", "driver/phase_context.py", "driver/claude_vc5.py", "driver/qualification_materials.py"}
    if not required_driver <= {row["path"] for row in manifest["files"]}:
        raise ControlError("formal_intake_not_ready：安装清单缺完整来源准备接线")
    context, originals = managed_source_context(data_root, Path(installed_session["parameter_path"]), installed_session["qualification"], installed_session["entry_references"], formed_context=installed_session["formed_context"])
    if dict(public_source_context) != context:
        raise ControlError("formal_intake_not_ready：公开来源上下文与原参数及历史 entry 不符")
    context_binding = installed_session["public_context_binding"]
    context_id = canonical_sha256(context)
    if context_binding["path"] != f"control/qualification-intake/{context_id}/context.json":
        raise ControlError("formal_intake_not_ready：来源上下文不在固定受管位置")
    context_path = data_root / context_binding["path"]
    context_raw, _ = _plain_file(context_path)
    if _descriptor(context_path) != {"absolute_path": str(context_path), "sha256": context_binding["sha256"], "bytes": context_binding["bytes"]} or parse_json_bytes(context_raw) != context:
        raise ControlError("formal_intake_not_ready：来源公开上下文原件不相符")
    approval_binding = installed_session["approval_binding"]
    if approval_binding["path"] != f"control/qualification-intake/{context_id}/approval.json":
        raise ControlError("formal_intake_not_ready：来源范围批准不在固定受管位置")
    approval_path = data_root / approval_binding["path"]
    approval_raw, _ = _plain_file(approval_path)
    approval = parse_json_bytes(approval_raw)
    if set(approval) != {"schema_version", "context_binding", "context_sha256", "scope", "approved_by"} or approval["schema_version"] != "claude-qualification-intake-approval/v1" or approval["context_binding"] != context_binding or approval["context_sha256"] != context_id or approval["scope"] != "read-copy-exact-qualified-layout/v1" or not approval["approved_by"] or sha256_bytes(approval_raw) != approval_binding["sha256"] or len(approval_raw) != approval_binding["bytes"]:
        raise ControlError("formal_intake_not_ready：没有绑定精确来源范围的明确批准")
    repository = data_root
    policy = policy_module.load_policy(repository / "tools/official_client_capture" / policy_module.POLICY_FILENAME)
    memo = os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
    try:
        measured = policy_module.compute_identity(policy, repository)
    finally:
        if memo is not None:
            os.environ["CODEX_UPGRADE_IDENTITY_MEMO"] = memo
    identity = {key: measured[key] for key in TOOL_IDENTITY_FIELDS}
    if identity != installed_session["tool_identity"] or identity["client"] != "claude" or identity["policy_version"] < 12:
        raise ControlError("formal_intake_not_ready：实际双受管根工具身份缺第二根能力")
    deployment = latest_deployment(data_root, "claude")
    source_originals = (*originals, SourceOriginal("driver_manifest", target / install.MANIFEST_NAME, _plain_file(target / install.MANIFEST_NAME)[0]),
        SourceOriginal("install", data_root / "control" / installed["install_receipt"], _plain_file(data_root / "control" / installed["install_receipt"])[0]),
        SourceOriginal("deployment", data_root / "control" / deployment["receipt"], _plain_file(data_root / "control" / deployment["receipt"])[0]),
        SourceOriginal("public_context", context_path, context_raw), SourceOriginal("intake_approval", approval_path, approval_raw))
    from tools.official_client_control.store import ControlStore
    store = ControlStore.open_read_only(data_root / installed_session["qualification"]["store_relative"])
    formation = {"candidate_revision": context["candidate_revision"], "evaluation_baseline": context["evaluation_baseline"],
        "qualification_roots": context["qualification_roots"], "store_formed_under": store.load_fact(installed_session["qualification"]["candidate_ref"])["formed_under"]}
    provenance = {"public_context": context, "formation_context": formation,
        "original_descriptors": {item.name: _descriptor(item.path) for item in source_originals}}
    reference_sha = canonical_sha256(provenance)
    roots = tuple(freeze_root_capability(Path(row["source_locator"]), row["root_kind"], authority=FORMAL_AUTHORITY,
        reference_id="managed-qualified-layout:" + context_id, reference_sha256=reference_sha,
        allowed_relative_paths=row["allowed_relative_paths"]) for row in context["source_layout"])
    reader = compute_gate_reader_identity(repository, tool_identity=identity, formed_context=installed_session["formed_context"], runtime_manifest=installed_session["runtime_manifest"])
    if reader.document["runtime_manifest"]["native_libraries_certified"] is not True:
        raise ControlError("formal_intake_not_ready：Python 实际共享库身份未完成安装准备")
    return VerifiedIntakeContext(FORMAL_AUTHORITY, "formal", data_root, repository, provenance, roots, reader, _TOKEN, source_originals)


def prepare_qualification_source(store: Any, qualification: Mapping[str, Any], *, role: str,
                                  intake_context: VerifiedIntakeContext,
                                  entry_references: Mapping[str, Any] | None = None) -> VerifiedQualificationSource:
    if not isinstance(intake_context, VerifiedIntakeContext) or intake_context.token is not _TOKEN or role not in {"candidate", "rollback"}:
        raise ControlError("来源准备须使用受管 intake 能力和固定资格角色")
    candidate, acceptance = _check_four_roots(store, qualification)
    from .claude_upgrade_arm64_delivery_executor import replay_qualification_graph
    external = intake_context.data_root / qualification["external_root_relative"]
    replay_qualification_graph(store, dict(qualification), external, intake_context.data_root)
    binding = qualification["sealed_result_binding"]
    raw, _metadata = _plain_file(intake_context.data_root / binding["path"])
    if len(raw) != binding["bytes"] or sha256_bytes(raw) != binding["sha256"]:
        raise ControlError("封存资格父原件字节漂移")
    parent = parse_json_bytes(raw)
    source_entry = dict(entry_references or {})
    if intake_context.source_scope == "formal":
        if not source_entry:
            raise ControlError("formal_intake_not_ready：缺精确资格形成期 entry")
        campaign = intake_context.data_root / "evidence/campaigns" / qualification["campaign_id"]
        real_parent = replay_qualification_entry(store, campaign, qualification, source_entry, formed_context=intake_context.provenance["formation_context"])
        if real_parent != parent:
            raise ControlError("来源封存父不是形成期原 reader 验真的 accept 原件")
    if parent.get("schema_version") != "claude-upgrade-acceptance/v1" or parent.get("candidate_id") != qualification["candidate_id"] or parent.get("production_state") != "not_activated" or parent.get("store", {}).get("acceptance_ref") != qualification["acceptance_ref"] or parent.get("store", {}).get("validation_completed_ref") != qualification["validation_ref"]:
        raise ControlError("来源封存父与完整资格四根不同源")
    external_gate = parent.get("external_gate")
    gate_root_id = receipt_relative = None
    expected_subject = {}
    if external_gate is None:
        if intake_context.source_scope != "regression_fixture" or intake_context.provenance.get("fixture_contract") != "explicit-no-gate-rollback/v1" or role != "rollback":
            raise ControlError("正式资格或候选不得因缺 external_gate 降级免验")
    else:
        if not isinstance(external_gate, dict) or set(external_gate) != {"evidence_root", "receipt"}:
            raise ControlError("封存门禁父域字段不闭合")
        matches = [root for root in intake_context.roots if root.root_kind == "gate_evidence" and str(root.logical_root) == external_gate["evidence_root"]]
        if len(matches) != 1:
            raise ControlError("父域门禁坐标不是事先授权的受管来源根")
        capability = matches[0]
        gate_root_id = capability.root_id
        receipt_relative = _relative(external_gate["receipt"]["path"])
        if receipt_relative not in capability.allowed_relative_paths:
            raise ControlError("父域门禁收据不在精确来源范围")
        receipt_raw, _metadata = _plain_file(capability.physical_root / receipt_relative)
        if len(receipt_raw) != external_gate["receipt"]["bytes"] or sha256_bytes(receipt_raw) != external_gate["receipt"]["sha256"]:
            raise ControlError("父域门禁原收据字节漂移")
        from . import claude_upgrade_accept as accept
        campaign = store.load_campaign(qualification["campaign_id"])
        frozen_identity = {key: parent["candidate_identity"][key] for key in accept.EXTERNAL_GATE_IDENTITY_FIELDS}
        # Store Campaign 的总计划入口和 accept 父都绑定同一 Campaign；正式
        # 态总计划已经由 entry replay 验真，夹具仍须逐字段给齐真实 subject。
        manifest = {"campaign_id": qualification["campaign_id"], "campaign_mode": parent.get("campaign_mode"),
                    "campaign_purpose": parent.get("campaign_purpose"), "target_version": campaign.get("target_version")}
        expected = accept.expected_gate_subject(manifest, candidate["payload"], frozen_identity)
        expected_subject = {**expected, "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None}
    return VerifiedQualificationSource(intake_context, role, dict(qualification), dict(binding), parent, expected_subject,
                                       gate_root_id, receipt_relative, source_entry, intake_context.provenance.get("formation_context"), _TOKEN)


def _formal_resolver(store: Any, qualification: Mapping[str, Any], source_reference: Any, *, purpose: str) -> VerifiedQualificationSource:
    if not isinstance(source_reference, VerifiedQualificationSource) or source_reference.token is not _TOKEN or source_reference.intake.authority != FORMAL_AUTHORITY or source_reference.intake.source_scope != "formal":
        raise ControlError("formal_intake_not_ready：来源引用不是本安装 session 取得的能力")
    if source_reference.qualification != dict(qualification):
        raise ControlError("来源引用与本次精确资格四根不同")
    _check_four_roots(store, qualification)
    return source_reference


# 正式资格 resolver 具有实际验真实现；正式交付执行器 SOURCE_VERIFIERS 仍不登记。
QUALIFICATION_SOURCE_RESOLVERS: dict[tuple[str, str], Callable[..., VerifiedQualificationSource]] = {
    (FORMAL_AUTHORITY, purpose): _formal_resolver for purpose in PURPOSES
}


def resolve_qualification_source(store: Any, qualification: Mapping[str, Any], source_reference: Any,
                                  *, purpose: str) -> VerifiedQualificationSource:
    if purpose not in PURPOSES or not isinstance(source_reference, VerifiedQualificationSource) or source_reference.token is not _TOKEN:
        raise ControlError("JSON descriptor 不能代替已验证的来源 capability")
    handler = QUALIFICATION_SOURCE_RESOLVERS.get((source_reference.intake.authority, purpose))
    if handler is None:
        raise ControlError("来源 authority 尚无该用途的受管取得合同")
    result = handler(store, qualification, source_reference, purpose=purpose)
    if not isinstance(result, VerifiedQualificationSource) or result.token is not _TOKEN or result.qualification != dict(qualification):
        raise ControlError("来源 resolver 未返回对应资格的真实能力")
    return result
