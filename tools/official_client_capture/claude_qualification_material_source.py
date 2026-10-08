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

from tools.official_client_control.canonical import canonical_sha256, parse_json_bytes, sha256_bytes, validate_external_binding
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


@dataclass(frozen=True)
class VerifiedQualificationSubjectProjection:
    """原 reader 验真的精确父字节；未登记原件仍走通用严格递归。

    相对坐标根来自冻结 Campaign 或 Store 资格根。仅完整父合同承认的
    元数据域可以不展开；域原字节、坐标、摘要在每次消费时再次比较。
    这个内存对象不写进 acquisition，不接受外部 JSON 声明权限。
    """
    parent_binding: Mapping[str, Any]
    base_relative: str
    metadata_domains: tuple[tuple[tuple[str, ...], str], ...]
    coordinate_roots: tuple[tuple[tuple[str, ...], str], ...]
    token: object

    def project(self, binding: Mapping[str, Any], raw: bytes) -> list[dict[str, Any]] | None:
        from copy import deepcopy
        from tools.official_client_control.delivery_execution import typed_bindings
        if self.token is not _TOKEN:
            raise ControlError("subject 父域投影不是原 reader 的受管结果")
        if dict(binding) != dict(self.parent_binding):
            return None
        if len(raw) != binding["bytes"] or sha256_bytes(raw) != binding["sha256"]:
            raise ControlError("subject 投影父坐标或原字节漂移")
        document = deepcopy(parse_json_bytes(raw))
        def owner(coordinate):
            node = document
            for key in coordinate[:-1]:
                node = node[int(key)] if isinstance(node, list) else node[key]
            return node, int(coordinate[-1]) if isinstance(node, list) else coordinate[-1]

        for coordinate, digest in self.metadata_domains:
            if not coordinate:
                if canonical_sha256(document) != digest:
                    raise ControlError("subject 原画像与完整 Snapshot 父域漂移")
                return []
            node, key = owner(coordinate)
            if canonical_sha256(node[key]) != digest:
                raise ControlError("subject 投影的完整父元数据域漂移")
            del node[key]
        relocated = []
        for coordinate, base in self.coordinate_roots:
            node, key = owner(coordinate)
            row = validate_external_binding(node[key], "subject.original_binding")
            relocated.append(validate_external_binding({**row, "path":
                (Path(base) / row["path"]).as_posix()}, "subject.relocated_binding"))
            del node[key]
        return relocated + [validate_external_binding({**row, "path":
            (Path(self.base_relative) / row["path"]).as_posix()}, "subject.projected_binding")
            for row in typed_bindings(document)]


def _prove_frozen_active_derivation(store: Any, external: Path, campaign: Path,
                                   manifest: Mapping[str, Any], build: Mapping[str, Any], *,
                                   retain: Callable[..., bytes],
                                   project_parent: Callable[..., None]) -> None:
    """重放精确 VC-4 的冻结 Active 派生父链，不寻找旧 Snapshot 或最新准备包。

    Active 是本次总计划冻结的历史输入文档。只有原 VC-3 批准、联合
    Store 原件、目标 Snapshot 和原纯派生函数同时证明完整文档时，才
    登记其两个精确副本的内存投影。证明材料沿调用方既有读集归档；
    画像内部的历史证据坐标始终不产生新的文件根权限。
    """
    from collections import Counter
    from types import SimpleNamespace
    from . import claude_upgrade_classify as classify
    from . import claude_upgrade_profile as profile
    from . import claude_upgrade_release as release
    from . import claude_upgrade_vc as vc
    from tools.official_client_control.gates import WorkflowGates
    from tools.official_client_control.qualification_bindings import QualificationBindingProjection

    active_names = ("active_release_catalog", "active_profile")
    if not any((manifest.get("inputs") or {}).get(name) is not None for name in active_names):
        # 没有冻结 Active 的旧来源不获得新投影，仍遵循原严格读取路径。
        return
    if any((manifest.get("inputs") or {}).get(name) is None for name in active_names):
        raise ControlError("冻结 Active 派生证明缺少成对的总计划输入")

    def bound(binding: Mapping[str, Any], expected: Path | None = None) -> tuple[Path, bytes]:
        if not isinstance(binding, Mapping) or set(binding) not in ({"path", "sha256"}, {"path", "sha256", "bytes"}):
            raise ControlError("冻结 Active 父链绑定字段不闭合")
        path = campaign / _relative(binding["path"])
        if expected is not None and path != expected:
            raise ControlError("冻结 Active 父链不是指定 revision 的精确坐标")
        raw = retain(path, binding["sha256"])
        if "bytes" in binding and binding["bytes"] != len(raw):
            raise ControlError("冻结 Active 父链原件字节数漂移")
        return path, raw

    projection = QualificationBindingProjection(store)

    def original(reference: Mapping[str, Any]) -> dict[str, Any]:
        """沿原 Store 合同核原件；其外部绑定仍只在既有 external 根读取。"""
        if "object_kind" in reference:
            document = store.load_object(dict(reference))
            path = store.object_path(dict(reference))
            WorkflowGates(store).validate_object_graph(dict(reference))
        else:
            document = store.load_fact(dict(reference))
            path = (Path(store.root) / "facts" / reference["campaign_id"] / reference["dimension"]
                / f"{reference['sequence']:08d}-{reference['sha256']}.json")
        raw = retain(path, reference["sha256"])
        expected = parse_json_bytes(raw)
        if "object_kind" not in reference:
            # 原 fact reader 返回已核过序链的文档，并明确增加引用摘要。
            # 完整视图必须恰为原文档加这一字段，不能裁剪其它差异。
            expected = {**expected, "_sha256": reference["sha256"]}
        if expected != document:
            raise ControlError("冻结 Active 父链 Store 视图与来源原字节不同")
        for binding in projection.bindings(document, reference):
            store._verify_external_binding(external, binding)
            projection.verify_external_mode(external, binding)
            retain(external / _relative(binding["path"]), binding["sha256"])
        domains = tuple(tuple(row["domain_pointer"].strip("/").split("/"))
            for row in projection.manifest()["parent_domains"] if row["parent_ref"] == reference)
        project_parent(path, external, domains)
        return document

    manifest_raw = retain(campaign / "campaign.json")
    if (parse_json_bytes(manifest_raw) != dict(manifest)
            or build["campaign_manifest_sha256"] != sha256_bytes(manifest_raw)
            or build["campaign_id"] != manifest["campaign_id"]):
        raise ControlError("冻结 Active 父链与精确 VC-4 总计划不同源")
    plan_path, plan_raw = bound(manifest["vc_control"]["campaign_plan"])
    plan = vc.vc_campaign_plan(campaign, manifest)
    if parse_json_bytes(plan_raw) != plan:
        raise ControlError("冻结 Active 的 VC 总计划原件视图漂移")
    vc4_path, vc4 = vc.replay_vc_checkpoint(campaign, plan, "VC-4", revision=build["candidate_revision"])
    if parse_json_bytes(retain(vc4_path)) != vc4:
        raise ControlError("冻结 Active 的 VC-4 checkpoint 原字节漂移")
    _build_path, build_raw = bound(vc4["stage_receipt"])
    if parse_json_bytes(build_raw) != dict(build):
        raise ControlError("冻结 Active 的 VC-4 不是当前精确构建原件")
    # 原 checkpoint reader 回溯到 VC-0 并读取每份阶段收据。全部原件
    # 纳入同一来源读集，不能只封存本次直接使用的 VC-2／VC-3。
    checkpoint_parents = [vc4_path]
    current = vc4
    while current["predecessor_checkpoint"] is not None:
        previous = current["predecessor_checkpoint"]
        path, raw = bound({key: previous[key] for key in ("path", "sha256")})
        current = parse_json_bytes(raw)
        checkpoint_parents.append(path)
        path, _raw = bound(current["stage_receipt"])
        checkpoint_parents.append(path)
    vc3_binding = vc4["predecessor_checkpoint"]
    if vc3_binding["phase"] != "VC-3":
        raise ControlError("冻结 Active 的直接父阶段不是 VC-3")
    vc3_path, vc3_raw = bound({key: vc3_binding[key] for key in ("path", "sha256")})
    vc3 = parse_json_bytes(vc3_raw)
    stage_path, stage_raw = bound(vc3["stage_receipt"], campaign / profile.STAGE_RESULT_RELATIVE)
    stage = parse_json_bytes(stage_raw)
    if (stage.get("schema_version") != profile.STAGE_RESULT_SCHEMA
            or stage.get("campaign_id") != manifest["campaign_id"]
            or stage.get("client") != "claude" or stage.get("stage") != "stage-profile"
            or stage.get("status") != "complete"
            or profile.REVISION_RE.fullmatch(str(stage.get("prepared_revision", ""))) is None):
        raise ControlError("冻结 Active 的精确 VC-3 阶段合同不闭合")
    for stage_key, build_key in (("profile_approval_fact_ref", "profile_approval_ref"),
                                 ("release_artifact_ref", "release_artifact_ref"),
                                 ("support_envelope_ref", "support_envelope_ref")):
        if stage["store"][stage_key] != build["store"][build_key]:
            raise ControlError("冻结 Active 的 VC-3 批准与精确 VC-4 Store 构建错配")
    if (stage["staged_release"]["sha256"] != build["catalog_stage"]["sha256"]
            or stage["staged_release"]["release_sha256"] != build["profile"]["release_sha256"]):
        raise ControlError("冻结 Active 的暂存 Release 与精确 VC-4 构建错配")

    # 先封闭所有读坐标，再调用原读者；显式 revision 禁止取后继准备包。
    revision = stage["prepared_revision"]
    result_path, result_raw = bound(stage["prepare_result"], profile.prepare_result_path(campaign, revision))
    result = parse_json_bytes(result_raw)
    expected_files = {*profile.PREPARE_FILES.values(), *profile.PREPARED_EXTRA_FILES.values()}
    if set(result.get("prepared") or {}) != expected_files:
        raise ControlError("冻结 Active 的批准准备包文件全集不闭合")
    prepared = {}
    prepared_paths = {}
    for name, binding in result["prepared"].items():
        path, raw = bound(binding, campaign / profile.PREPARED_ROOT / revision / name)
        prepared_paths[name], prepared[name] = path, raw
    if profile.load_prepare_result(campaign, manifest, revision) != result:
        raise ControlError("冻结 Active 的指定准备结果在原 reader 重放时漂移")
    draft_path, draft_raw = bound(result["draft_receipt"],
        campaign / profile.PREPARE_DRAFT_ROOT / revision / profile.DRAFT_RECEIPT_FILENAME)
    draft = parse_json_bytes(draft_raw)
    original_draft, draft_files = profile.load_revision(campaign, manifest["campaign_id"], revision)
    if original_draft != draft:
        raise ControlError("冻结 Active 的完整草案收据在原 reader 重放时漂移")
    for name, filename in profile.PREPARE_FILES.items():
        raw = retain(draft_path.parent / filename, draft["files"][name]["sha256"])
        if raw != draft_files[name] or raw != prepared[filename]:
            raise ControlError("冻结 Active 的草案与批准文件原字节不同")

    catalog_path, active_path = profile._active_paths(campaign, manifest, None, None)
    catalog_raw, active_raw = retain(catalog_path), retain(active_path)
    catalog = parse_json_bytes(catalog_raw)
    active_profile = parse_json_bytes(active_raw)
    if profile.profile_bytes(active_profile) != active_raw:
        raise ControlError("冻结 Active 画像不是原合同的内容寻址规范字节")
    active = profile._active_release(catalog, sha256_bytes(active_raw))
    identity = active_profile.get("identity") or {}
    if (identity.get("version") != manifest["baseline_version"]
            or active["version"] != manifest["baseline_version"]
            or identity.get("platform") != active["platform"]
            or profile.list_key_field(active_profile.get("rules")) != "spec_id"
            or sorted(row["spec_id"] for row in active_profile["rules"]) != sorted(manifest["required_rules"])):
        raise ControlError("冻结 Active 的版本、平台或基线规则全集不同源")
    for name, raw in (("active_release_catalog", catalog_raw), ("active_profile", active_raw)):
        if prepared[profile.PREPARED_EXTRA_FILES[name]] != raw:
            raise ControlError("冻结 Active 总计划输入与指定批准副本原字节不同")
    policy = parse_json_bytes(prepared[profile.PREPARE_FILES["policy"]])
    frozen_bindings = {name: profile._campaign_file_binding(campaign, path, name)
        for name, path in (("active_release_catalog", catalog_path), ("active_profile", active_path))}
    expected_inputs = {"policy": policy, **frozen_bindings}
    if (draft["inputs"] != expected_inputs
            or draft["inputs_sha256"] != profile._orchestrator()._fingerprint(expected_inputs)
            or result["inputs_sha256"] != draft["inputs_sha256"]):
        raise ControlError("冻结 Active 草案 policy、总计划输入与批准输入摘要不同源")

    vc2_binding = vc3["predecessor_checkpoint"]
    vc2_path, vc2_raw = bound({key: vc2_binding[key] for key in ("path", "sha256")})
    vc2 = parse_json_bytes(vc2_raw)
    class_path, class_raw = bound(vc2["stage_receipt"], campaign / classify.STAGE_RESULT_RELATIVE)
    classification = parse_json_bytes(class_raw)
    documents = {}
    for name, binding in classification["approved"].items():
        _path, raw = bound(binding)
        documents[name] = parse_json_bytes(raw)
    class_draft_path, class_draft_raw = bound(classification["draft_receipt"],
        campaign / classify.DRAFT_ROOT / classification["revision"] / classify.DRAFT_RECEIPT_FILENAME)
    if classify.load_stage_result(campaign, manifest) != classification:
        raise ControlError("冻结 Active 的精确 VC-2 分类原 reader 视图漂移")
    class_draft, class_files = classify.load_revision(campaign, manifest["campaign_id"], classification["revision"])
    if parse_json_bytes(class_draft_raw) != class_draft:
        raise ControlError("冻结 Active 的分类 revision 原 reader 与完整草案原件不同")
    for name, filename in classify.DRAFT_FILES.items():
        raw = retain(class_draft_path.parent / filename, class_draft["files"][name]["sha256"])
        _approved_path, approved_raw = bound(classification["approved"][name])
        if raw != class_files[name] or raw != approved_raw:
            raise ControlError("冻结 Active 的分类草案与批准副本原字节不同")
    refs = {key: classification["store"][key] for key in (
        "evidence_fact_ref", "evidence_package_ref", "rule_migration_ledger_ref",
        "atomic_assertion_ledger_ref", "classification_fact_ref")}
    classification_fact = original(refs["classification_fact_ref"])
    if classification_fact["payload"] != {key: refs[key] for key in (
            "evidence_fact_ref", "evidence_package_ref", "rule_migration_ledger_ref", "atomic_assertion_ledger_ref")}:
        raise ControlError("冻结 Active 的分类事实与精确 VC-2 引用不同源")
    migration = original(refs["rule_migration_ledger_ref"])["payload"]
    atomic = original(refs["atomic_assertion_ledger_ref"])["payload"]
    evidence = original(refs["evidence_package_ref"])["payload"]
    original(refs["evidence_fact_ref"])
    if sorted(classification["target_rules"]) != sorted(migration["target_spec_ids"]):
        raise ControlError("冻结 Active 的分类规则与原 Store 迁移台账不同源")
    approval = original(stage["store"]["profile_approval_fact_ref"])
    evidence_approval = original(result["store"]["evidence_approval_fact_ref"])
    expected_approval_inputs = {key: refs[key] for key in ("evidence_fact_ref", "evidence_package_ref", "classification_fact_ref")}
    if (approval["fact_kind"] != "profile_approved"
            or approval["payload"]["evidence_approval_ref"] != result["store"]["evidence_approval_fact_ref"]
            or stage["store"]["evidence_approval_fact_ref"] != result["store"]["evidence_approval_fact_ref"]
            or evidence_approval["fact_kind"] != "evidence_approved"
            or any(evidence_approval["payload"][key] != value for key, value in expected_approval_inputs.items())
            or any(result["store"][key] != value for key, value in expected_approval_inputs.items())):
        raise ControlError("冻结 Active 的准备批准与原证据、分类或画像批准不同源")

    binary = profile._active_binary(active_profile)
    configuration = manifest["configuration"]
    pairs = [(manifest["baseline_version"], manifest["target_version"]),
             (identity["platform"], configuration["platform"])]
    if binary is not None:
        pairs.append((binary, manifest["target_sha256"]))
    replacements = {old: new for old, new in pairs if old and old != new}
    for key, (old, new) in zip(profile.IDENTITY_CONDITION_KEYS, pairs + [(None, None)] * (3 - len(pairs))):
        if old and new and old != new:
            replacements[f"{key}={old}"] = f"{key}={new}"
    counts = Counter(row["owner_kind"] for row in atomic["assertions"])
    # 这个两字段引用是封存生成器元数据。其完整 policy 原字节另由
    # joint_source 的 prepare-files 证明，不据此打开仓库或任何外部根。
    projection_binding = policy["active"]["projection_policy"]
    if (set(projection_binding) != {"path", "sha256"}
            or profile.SHA256_RE.fullmatch(str(projection_binding["sha256"])) is None):
        raise ControlError("冻结 Active 的投影生成器元数据不闭合")
    _relative(projection_binding["path"])
    expected_policy = {
        "schema_version": profile.PROFILE_POLICY_SCHEMA, "campaign_id": manifest["campaign_id"],
        "baseline_version": manifest["baseline_version"], "target_version": manifest["target_version"],
        "campaign_purpose": manifest["campaign_purpose"],
        "target_identity": {"version": manifest["target_version"], "platform": configuration["platform"],
            "binary_sha256": manifest["target_sha256"], "entrypoint": str(configuration.get("entrypoint", "")),
            "privacy_mode": str(configuration.get("privacy_mode", ""))},
        "active": {"release_catalog": frozen_bindings["active_release_catalog"],
            "profile": frozen_bindings["active_profile"], **active, "binary_sha256": binary,
            "profile_schema_version": str(active_profile.get("schema_version", "")), "projection_policy": dict(projection_binding)},
        "classification": {"stage_result": {"path": class_path.relative_to(campaign).as_posix(), "sha256": sha256_bytes(class_raw)},
            "vc2_checkpoint": {"path": vc2_path.relative_to(campaign).as_posix(), "checkpoint_sha256": vc2["checkpoint_sha256"]},
            "revision": classification["revision"], "review_sha256": classification["review_sha256"], **refs},
        "rules": {"target_rules": classification["target_rules"], "affected_rules": classification["affected_rules"],
            "inherited_rules": classification["inherited_rules"], "atomic_assertion_count": len(atomic["assertions"]),
            "scenario_only_owner_ids": atomic["scenario_only_owner_ids"], "atomic_owner_counts": dict(sorted(counts.items()))},
        "identity_substitutions": [{"from": old, "to": new} for old, new in sorted(replacements.items())],
        "model_capability_paths": list(profile.MODEL_CAPABILITY_PATHS),
        "generator": {"module": "claude_upgrade_profile", "policy_schema": profile.PROFILE_POLICY_SCHEMA,
            "projection_module": "tools.official_client_capture.claude_upgrade_wire_projection",
            "projection_policy_schema": release.wire_projection.PROJECTION_POLICY_SCHEMA}}
    if policy != expected_policy:
        raise ControlError("冻结 Active 的完整 policy 不等于原分类、输入与生成语义")

    joint_path, joint_raw = bound(stage["joint_manifest"])
    joint = parse_json_bytes(joint_raw)
    if (joint.get("schema_version") != profile.JOINT_MANIFEST_SCHEMA
            or joint["campaign_id"] != manifest["campaign_id"]
            or joint["generation"] != stage["generation"] or joint["prepare_result"] != stage["prepare_result"]
            or joint["joint_sha256"] != stage["joint_sha256"]
            or profile._joint_sha256(joint["objects"], joint["approval_core"]) != joint["joint_sha256"]
            or any(approval["payload"].get(key) != value for key, value in joint["approval_core"].items())
            or any(stage["store"].get(key) != value for key, value in joint["approval_core"].items() if key.endswith("_ref"))):
        raise ControlError("冻结 Active 联合清单、原画像批准与精确 VC-3 不同源")
    joint_metadata = []
    joint_sources = []
    for index, item in enumerate(joint["objects"]):
        parent = original(item["ref"])
        if parent["object_kind"] != item["kind"] or parent["payload"] != item["payload"]:
            raise ControlError("冻结 Active 联合制品不是原 Store 完整父文档")
        if item["kind"] == "snapshot":
            joint_metadata.append(("objects", str(index), "payload", "document"))
        if item["name"] == "joint_source":
            joint_sources.append(parent["payload"])
    derivation_raw = prepared[profile.PREPARED_EXTRA_FILES["derivation"]]
    derivation = parse_json_bytes(derivation_raw)
    target_raw = prepared[profile.PREPARED_EXTRA_FILES["target_profile"]]
    expected_joint_source = profile._manifest_payload("operational_evidence", store.load_campaign(manifest["campaign_id"])["persona"],
        "claude-upgrade-vc3-joint-source", [
            {"id": "active-release", "facts": {key: active[key] for key in ("release_sha256", "profile_sha256", "bundle_sha256", "version", "platform")}},
            {"id": "classification", "facts": expected_policy["classification"]},
            {"id": "prepare-files", "facts": {name: sha256_bytes(raw) for name, raw in draft_files.items()}},
            {"id": "target-profile", "facts": {"profile_sha256": sha256_bytes(target_raw), "derivation_sha256": canonical_sha256(derivation)}}])
    if joint_sources != [expected_joint_source]:
        raise ControlError("冻结 Active 的完整草案原字节没有原联合来源证明")
    retain(Path(store.root) / "campaigns" / (manifest["campaign_id"] + ".json"))
    target_release = original(build["store"]["release_artifact_ref"])["payload"]
    snapshot = original(target_release["snapshot_ref"])["payload"]
    if (approval["payload"]["release_artifact_ref"] != build["store"]["release_artifact_ref"]
            or approval["payload"]["snapshot_ref"] != target_release["snapshot_ref"]
            or stage["store"]["snapshot_ref"] != target_release["snapshot_ref"]
            or snapshot["profile_digest"] != sha256_bytes(target_raw)
            or stage["target_profile_sha256"] != sha256_bytes(target_raw)
            or result["target_profile_sha256"] != sha256_bytes(target_raw)
            or joint["target_profile_sha256"] != sha256_bytes(target_raw)
            or snapshot["document"] != parse_json_bytes(target_raw)):
        raise ControlError("冻结 Active 的目标画像与原批准 Release／Snapshot 完整文档错配")
    attestation = canonical_sha256({"attestation": "claude-upgrade-vc3-active-derivation/v1",
        "active_release_sha256": active["release_sha256"], "active_profile_sha256": sha256_bytes(active_raw),
        "profile_schema_version": active_profile["schema_version"], "derivation_sha256": canonical_sha256(derivation)})
    if snapshot["compiler_attestation_sha256"] != attestation:
        raise ControlError("冻结 Active 的目标 Snapshot 派生证明错配")
    catalog_document = parse_json_bytes(prepared[profile.PREPARED_EXTRA_FILES["model_catalog"]])
    catalog_object = original(stage["store"]["model_capability_catalog_ref"])["payload"]
    catalog_rows = [row["facts"] for row in catalog_object["entries"] if row["id"] == "catalog"]
    if (len(catalog_rows) != 1 or catalog_rows[0]["document"] != catalog_document
            or catalog_rows[0]["catalog_sha256"] != sha256_bytes(prepared[profile.PREPARED_EXTRA_FILES["model_catalog"]])):
        raise ControlError("冻结 Active 的模型目录不是同一批准联合制品的原字节")
    # 只向原纯函数传它实际使用的冻结字段，不借当前 Store 状态重做
    # prepare/stage，也不构造包含虚假运行证据的 CatalogProjection。
    inputs = SimpleNamespace(campaign_id=manifest["campaign_id"], baseline_version=manifest["baseline_version"],
        target_version=manifest["target_version"], policy=policy, active_profile=active_profile, replacements=replacements,
        affected_rules=classification["affected_rules"], inherited_rules=classification["inherited_rules"],
        target_rules=classification["target_rules"], decisions={row["spec_id"]: row["migration_decision"] for row in migration["rules"]},
        planned_paths={row["spec_id"]: row["profile_paths"] for row in documents["profile_patches"]["rule_patches"]},
        evidence_ids=frozenset(row["evidence_id"] for row in evidence["evidence_items"]))
    model_identity = release.model_identity(SimpleNamespace(catalog=catalog_document,
        catalog_sha256=sha256_bytes(prepared[profile.PREPARED_EXTRA_FILES["model_catalog"]])))
    problems = profile._Problems()
    derived = profile.derive_target_profile(inputs,
        {name: parse_json_bytes(raw) for name, raw in draft_files.items()}, problems, model_identity)
    if (problems or derived is None or derived["target_bytes"] != target_raw
            or profile._serialize(derived["derivation"]) != derivation_raw
            or result["profile_diff_paths"] != derived["derivation"]["profile_diff_paths"]):
        raise ControlError("冻结 Active 到目标画像的原纯派生语义不能完整重放")

    # 到这里才给精确 Active 父和批准副本整文档投影。其它原件仍按
    # 自己的明确坐标展开；未知 JSON 和同 SHA 的其它路径没有此权限。
    project_parent(active_path, campaign, ((),))
    project_parent(prepared_paths[profile.PREPARED_EXTRA_FILES["active_profile"]], campaign, ((),))
    project_parent(prepared_paths[profile.PREPARED_EXTRA_FILES["target_profile"]], campaign, ((),))
    project_parent(draft_path, campaign, coordinate_roots=tuple(
        (("files", name), draft_path.parent) for name in profile.PREPARE_FILES))
    for name, filename in profile.PREPARE_FILES.items():
        project_parent(draft_path.parent / filename, campaign)
    classification_data = classify._capture()._host_data_root(campaign)
    for name, path in prepared_paths.items():
        if name not in {profile.PREPARED_EXTRA_FILES["active_profile"], profile.PREPARED_EXTRA_FILES["target_profile"]}:
            # 批准 Wire 的 VC-1 原件是 data 相对坐标，原件继续实读，
            # 不因批准包所在目录而重复加上 Campaign 前缀。
            base = classification_data if name == profile.PREPARED_EXTRA_FILES["target_wire"] else campaign
            project_parent(path, base)
    project_parent(result_path, campaign)
    project_parent(joint_path, campaign, tuple(joint_metadata))
    project_parent(stage_path, campaign)
    project_parent(class_path, campaign)
    # 分类原 collect_inputs／处置台账的来源坐标是宿主 data 根，
    # 只有 draft.files 自身使用同目录坐标；不把两类坐标混为一根。
    project_parent(class_draft_path, classification_data, coordinate_roots=tuple(
        (("files", name), class_draft_path.parent) for name in classify.DRAFT_FILES))
    for name, filename in classify.DRAFT_FILES.items():
        project_parent(class_draft_path.parent / filename, classification_data)
        project_parent(campaign / classification["approved"][name]["path"], classification_data)
    project_parent(plan_path, campaign)
    for path in checkpoint_parents:
        project_parent(path, campaign)


def _retain_candidate_evaluation_draft(
    campaign: Path, manifest: Mapping[str, Any], round_one: Mapping[str, Any], data_root: Path, *,
    retain: Callable[..., bytes], project_parent: Callable[..., None],
) -> None:
    """归档原读者验真的 seal 草稿；只转换两项已验真评估输入的坐标。

    草稿整体保持数据根坐标，只有原候选评估读者承认的两个 drafts 字段
    使用冻结 Campaign 根。其它标准绑定仍由严格闭包实读，不能借文件名、
    schema 或相同摘要取得转换权限。没有草稿引用的旧阶段不新增投影。
    """
    from . import claude_upgrade_compare as compare

    seal = round_one.get("seal")
    draft_binding = seal.get("seal_draft") if isinstance(seal, Mapping) else None
    if draft_binding is None:
        return
    source = compare.candidate_evaluation_source(campaign, manifest, round_one)
    draft_path = compare._bound_campaign_file(campaign, draft_binding, "来源候选 seal 草稿")
    # 先沿原绑定摘要保留字节，再登记投影；project_parent 的同坐标冲突
    # 检查负责拒绝这两次读取之间的漂移，不给新的首次读取结果背书。
    draft = parse_json_bytes(retain(draft_path, draft_binding["sha256"]))
    coordinates = []
    for name in ("capture_manifest", "capture_index"):
        binding = draft["drafts"][name]
        if any(binding[key] != source[name][key] for key in ("path", "sha256")):
            raise ControlError("候选评估草稿与原读者验真的精确输入不同源")
        path = compare._bound_campaign_file(campaign, binding, "来源候选评估输入 " + name)
        retain(path, binding["sha256"])
        coordinates.append((("drafts", name), campaign))
    project_parent(draft_path, data_root, coordinate_roots=tuple(coordinates))


def qualification_gate_subject(store: Any, qualification: Mapping[str, Any], data_root: Path,
                               parent: Mapping[str, Any], *, read_bindings: list[dict[str, Any]] | None = None,
                               read_projections: list[VerifiedQualificationSubjectProjection] | None = None) -> dict[str, Any]:
    """由封存 accept 的真实来源取得 subject，不让待验门禁为自己背书。

    Campaign 字段只读 evidence-seal 绑定的冻结总计划；五项候选身份复用
    VC-4 构建与固定首轮制品的原读取器。再与 Store 四根、暂存画像原字节
    逐项核同源。这里不选当前候选或最大评估轮次，candidate 和 rollback
    共用同一规则，也不要求正式 accept 增补从未存在的身份字段。
    """
    from . import claude_upgrade_accept as accept
    from . import claude_upgrade_acceptance as acceptance
    from . import claude_upgrade_compare as compare
    from . import claude_upgrade_release as release
    from . import claude_upgrade_store as campaign_store
    from . import claude_upgrade_vc as vc
    from tools.official_client_control.gates import WorkflowGates
    from tools.official_client_control.qualification_bindings import QualificationBindingProjection
    from tools.official_client_control.contracts import iter_fact_refs, iter_object_refs, iter_receipt_refs

    originals = {}
    projections = {}
    data_root = Path(data_root)
    if not data_root.is_absolute() or data_root.is_symlink() or not data_root.is_dir():
        raise ControlError("门禁 subject 缺已有可信数据根")
    def retain(path: Path, expected_sha256: str | None = None) -> bytes:
        """只回传已验真来源的精确原件，归档调用方沿既有绑定列表闭合。"""
        if not path.is_relative_to(data_root):
            raise ControlError("门禁 subject 原件越过对应可信数据根")
        raw, _metadata = _plain_file(path)
        if expected_sha256 is not None and sha256_bytes(raw) != expected_sha256:
            raise ControlError("门禁 subject 原件不在对应数据根或读取期间漂移")
        row = {"path": path.relative_to(data_root).as_posix(), "sha256": sha256_bytes(raw), "bytes": len(raw)}
        previous = originals.setdefault(row["path"], row)
        if previous != row:
            raise ControlError("门禁 subject 同坐标原件在读取期间漂移")
        return raw

    def project_parent(path: Path, base: Path, metadata: tuple[tuple[str, ...], ...] = (),
                       coordinate_roots: tuple[tuple[tuple[str, ...], Path], ...] = ()) -> None:
        """只登记本调用已经由原合同验真的父，不按文件名或 schema 授权。"""
        raw = retain(path)
        if not base.is_relative_to(data_root):
            raise ControlError("subject 绑定坐标根越过已有资格数据根")
        document = parse_json_bytes(raw)
        domains = []
        for coordinate in metadata:
            node = document
            for key in coordinate:
                node = node[int(key)] if isinstance(node, list) else node[key]
            domains.append((coordinate, canonical_sha256(node)))
        binding = originals[path.relative_to(data_root).as_posix()]
        roots = []
        for coordinate, root in coordinate_roots:
            if not root.is_relative_to(data_root):
                raise ControlError("subject 子绑定坐标根越过已有资格数据根")
            roots.append((coordinate, root.relative_to(data_root).as_posix()))
        row = VerifiedQualificationSubjectProjection(binding, base.relative_to(data_root).as_posix(), tuple(domains), tuple(roots), _TOKEN)
        previous = projections.setdefault(binding["path"], row)
        if previous != row:
            raise ControlError("subject 同一原件的父域或坐标根冲突")

    def retain_bindings(value: Any) -> None:
        """仅归一化原 reader 已校验的 Campaign 相对两字段绑定。"""
        if isinstance(value, Mapping):
            if set(value) == {"path", "sha256"}:
                path = compare._bound_campaign_file(campaign, value, "subject 来源原件")
                retain(path, value["sha256"])
            else:
                for child in value.values():
                    retain_bindings(child)
        elif isinstance(value, list):
            for child in value:
                retain_bindings(child)

    external = data_root if qualification["external_root_relative"] == "." else data_root / _relative(qualification["external_root_relative"])
    candidate, _acceptance = _check_four_roots(store, qualification)
    campaign = Path(data_root) / "evidence/campaigns" / qualification["campaign_id"]
    manifest_raw = retain(campaign / "campaign.json")
    manifest = acceptance._orchestrator()._require_formal_campaign(campaign)
    retain(campaign / "campaign.sha256")
    retain_bindings([manifest["inputs"], manifest["vc_control"]])
    campaign_store.require_store_campaign_matches_plan(manifest, store.load_campaign(qualification["campaign_id"]))
    parent_path = Path(data_root) / _relative(qualification["sealed_result_binding"]["path"])
    if not parent_path.is_relative_to(campaign):
        raise ControlError("门禁 subject 的 accept 原件不属于冻结 Campaign")
    if acceptance.load_stage_result(campaign, parent_path, qualification["candidate_id"], manifest=manifest) != dict(parent):
        raise ControlError("门禁 subject 的 accept 原件不是对应封存父")
    seal_path = compare._bound_campaign_file(campaign, parent["evidence_seal"], "来源验收证据封印")
    retain(parent_path, qualification["sealed_result_binding"]["sha256"])
    seal = parse_json_bytes(retain(seal_path, parent["evidence_seal"]["sha256"]))
    runtime = parent["runtime_audit"]
    retain_bindings([parent["comparison"], parent["assertions"],
        {key: runtime[key] for key in ("path", "sha256")}])
    expected_seal = {"schema_version": acceptance.EVIDENCE_SEAL_SCHEMA,
        "campaign_id": manifest["campaign_id"], "candidate_id": qualification["candidate_id"],
        "campaign_manifest_sha256": sha256_bytes(manifest_raw), "campaign_purpose": manifest["campaign_purpose"],
        "candidate_purpose": candidate["payload"]["candidate_purpose"], "production_state": "not_activated",
        "evaluation_baseline": parent["evaluation_baseline"], "candidate_external_gate": parent["external_gate"],
        "store": parent["store"]}
    if any(seal.get(key) != value for key, value in expected_seal.items()):
        raise ControlError("门禁 subject 的证据封印、冻结总计划与资格四根不同源")
    # 这里只认可已与 accept 原域逐字比较的封印父；原门禁的副本和
    # 读集仍由既有门禁投影负责，不能从 seal 的绝对根另取读取权。
    project_parent(seal_path, campaign, (("candidate_external_gate",),))
    build, build_binding = compare.load_candidate_build_receipt(
        campaign, manifest, qualification["candidate_id"], parent["candidate_revision"])
    stored_build = store.load_receipt(qualification["build_receipt_ref"])
    retain(compare._bound_campaign_file(campaign, build_binding, "subject 原构建"), build_binding["sha256"])
    # 构建读取器已经重放过这条精确 revision 的 checkpoint 链；归档
    # 仍须包含所有原 checkpoint 和它们绑定的阶段收据，不能只留摘要。
    checkpoint_path = vc.vc_checkpoint_path(campaign, "VC-4", revision=parent["candidate_revision"])
    stage_paths = []
    while True:
        checkpoint = parse_json_bytes(retain(checkpoint_path))
        retain_bindings(checkpoint["stage_receipt"])
        stage_paths.append(compare._bound_campaign_file(campaign, checkpoint["stage_receipt"], "subject VC 阶段"))
        previous_checkpoint = checkpoint["predecessor_checkpoint"]
        if previous_checkpoint is None:
            break
        checkpoint_path = vc._bound_file(campaign,
            {key: previous_checkpoint[key] for key in ("path", "sha256")}, "subject 前序 checkpoint")
    if (seal.get("build_receipt") != build_binding
            or build["campaign_manifest_sha256"] != sha256_bytes(manifest_raw)
            or build["candidate_purpose"] != manifest["campaign_purpose"]
            or parent["campaign_purpose"] != manifest["campaign_purpose"]
            or parent["candidate_purpose"] != manifest["campaign_purpose"]
            or build["store"]["candidate_frozen_ref"] != qualification["candidate_ref"]
            or build["store"]["candidate_build_receipt_ref"] != qualification["build_receipt_ref"]
            or candidate["formed_under"]["candidate_revision"] != parent["candidate_revision"]):
        raise ControlError("门禁 subject 的 VC-4 原构建没有绑定精确候选、revision 或冻结总计划")
    for key in ("profile_approval_ref", "release_artifact_ref", "support_envelope_ref"):
        if build["store"][key] != stored_build[key]:
            raise ControlError("门禁 subject 的 VC-4 与 Store 构建批准或 Release 错配")
    if (build["profile"]["profile_digest"] != stored_build["profile_identity"]["profile_digest"]
            or build["target_architecture"] != candidate["payload"]["target_architecture"]
            or any(build["image"][key] != stored_build["image"][key] for key in ("image_id", "reference", "manifest_digest"))
            or build["source"]["tree_sha256"] != candidate["payload"]["source_tree_sha256"]
            or any(build["build"][key] != stored_build["build_identity"][key] for key in ("build_id", "parameters", "toolchain"))):
        raise ControlError("门禁 subject 的 VC-4 与 Store 构建画像、镜像或源码身份不同源")
    # VC-4 原合同把宿主构建参数、binary 来源和 implementation_tests
    # 的外部证据根冻结为构建元数据。实际交付制品另由 Store 库存核字节；
    # 不把这些绝对路径升级为本轮数据根的开放权限。其它字段继续严格展开。
    project_parent(compare._bound_campaign_file(campaign, build_binding, "subject 原构建"),
        campaign, (("binary",), ("build", "parameters"), ("implementation_tests",)))
    binary = build["binary"]
    if not any(all(row["binding"][key] == binary[key] for key in ("sha256", "bytes"))
               for row in stored_build["artifact_inventory"]["entries"]):
        raise ControlError("门禁 subject 的 VC-4 二进制没有对应真实 Store 库存")
    for row in stored_build["artifact_inventory"]["entries"]:
        store._verify_external_binding(external, row["binding"])

    # 暂存 Release 只从固定 Campaign 坐标读；收据中的候选源码绝对路径
    # 不能授予新的宿主读取权。原 Release reader 逐文件复算画像原字节。
    release_path = campaign / release.STAGED_RELEASE_ROOT / release.STAGED_RECEIPT_FILENAME
    staged = release.load_staged_release(campaign, {"path": release_path.relative_to(campaign).as_posix(),
        "sha256": build["catalog_stage"]["sha256"]})
    retain(release_path, build["catalog_stage"]["sha256"])
    for row in staged["inventory"]:
        retain(campaign / release.STAGED_RELEASE_ROOT / _relative(row["path"]), row["sha256"])
    entry = staged["release_entry"]
    profile_relative = _relative(entry["profile"]["path"])
    profile_raw, _metadata = _plain_file(campaign / release.STAGED_RELEASE_ROOT / profile_relative)
    profile_digest = sha256_bytes(profile_raw)
    if (staged["campaign_id"] != manifest["campaign_id"] or staged["target_version"] != manifest["target_version"]
            or entry["version"] != manifest["target_version"] or entry["release_sha256"] != build["profile"]["release_sha256"]
            or entry["profile"]["sha256"] != profile_digest or profile_digest != build["profile"]["profile_digest"]
            or store.load_object(stored_build["release_artifact_ref"])["payload"]["profile_digest"] != profile_digest):
        raise ControlError("门禁 subject 的暂存画像原件与精确 VC-4／Store 构建不同源")
    identity = compare.candidate_gate_identity(campaign, manifest, qualification["candidate_id"], build)
    package_ref = {"object_kind": "candidate_evidence_package", "sha256": identity["candidate_package_digest"]}
    round_path = acceptance._orchestrator()._legacy_stage_path(campaign, "capture-candidate", qualification["candidate_id"])[1]
    round_one = parse_json_bytes(retain(round_path))
    if round_one["store"]["candidate_evidence_package_ref"] != package_ref:
        raise ControlError("门禁 subject 的固定首轮原件在读取期间漂移")
    retain_bindings([round_one["attempt"], round_one["assertion_context"],
        round_one["sealed_evidence_manifest"], round_one["client_evidence"]])
    # 纯 subject 推导沿原 reader 核身份；只有消费端请求归档读集时，
    # 才要求三根原件能在已有数据根内闭合。缺受管外根合同仍失败关闭。
    if read_bindings is not None or read_projections is not None:
        _retain_candidate_evaluation_draft(campaign, manifest, round_one, data_root,
            retain=retain, project_parent=project_parent)
        # 原首轮 reader 已完整重验两种 Mac 客户端；其主收据、来源包和
        # 捕获原件采用三个不同根。只沿已验真的精确字段转坐标，普通 JSON
        # 仍严格展开，不能把 verification 的摘要当作真实附件的替代品。
        from . import claude_client_receipt_contract as client_contract
        attempt_root = compare._bound_campaign_file(campaign, round_one["attempt"], "首轮 attempt").parent
        for client_binding in (round_one["client_evidence"].get("client_bindings") or {}).values():
            client_path = compare._bound_campaign_file(campaign, client_binding, "首轮客户端收据")
            client_receipt = parse_json_bytes(retain(client_path, client_binding["sha256"]))
            task_path = client_contract.bound_file(attempt_root, client_receipt["task"])
            task = parse_json_bytes(retain(task_path, client_receipt["task"]["sha256"]))
            bundle_manifest = client_contract.bound_file(attempt_root, client_receipt["bundle"])
            bundle_root = bundle_manifest.parent
            capture_root = Path(task["execution"]["capture_root"])
            retain(bundle_manifest, client_receipt["bundle"]["sha256"])
            project_parent(client_path, attempt_root, coordinate_roots=(
                (("verification", "source_manifest"), bundle_root),
                (("verification", "capture_manifest"), capture_root)))
            task_roots = [(("execution", "isolation_recipe"), bundle_root)]
            task_roots.extend((("client_artifacts", str(index), "official_source"), bundle_root)
                for index, _artifact in enumerate(task["client_artifacts"]))
            for index, reuse in enumerate(task["scenario_reuse"]):
                task_roots.extend((("scenario_reuse", str(index), name), Path(reuse["root"]))
                    for name in ("task", "source_manifest"))
            project_parent(task_path, attempt_root, coordinate_roots=tuple(task_roots))
            run_path = client_contract.bound_file(attempt_root, client_receipt["imported_run"])
            retain(run_path, client_receipt["imported_run"]["sha256"])
            project_parent(run_path, attempt_root, coordinate_roots=((("reservation",), capture_root),))
            accounting = client_contract.bound_file(attempt_root, client_receipt["accounting_binding"])
            retain(accounting, client_receipt["accounting_binding"]["sha256"])
            project_parent(accounting, attempt_root)
            source_manifest = parse_json_bytes(retain(bundle_manifest))
            project_parent(bundle_manifest, bundle_root)
            for row in source_manifest["files"]:
                path = client_contract.bound_file(bundle_root, row)
                raw = retain(path, row["sha256"])
                try:
                    parse_json_bytes(raw)
                except ControlError:
                    continue
                project_parent(path, bundle_root)
            # 捕获合同通过主清单、reservation、ready 与标准绑定闭合；不用
            # rglob 或动态根搜索补文件。相对绑定仍只在已冻结捕获根内解析。
            pending_capture = [capture_root / "capture-manifest.json"]
            visited_capture = set()
            from tools.official_client_control.delivery_execution import typed_bindings
            while pending_capture:
                path = pending_capture.pop()
                if path in visited_capture:
                    continue
                visited_capture.add(path)
                if len(visited_capture) > 20000:
                    raise ControlError("subject 首轮捕获闭包超过固定文件上限")
                raw = retain(path)
                try:
                    document = parse_json_bytes(raw)
                except ControlError:
                    continue
                project_parent(path, capture_root)
                pending_capture.extend(client_contract.bound_file(capture_root, row) for row in typed_bindings(document))
    WorkflowGates(store).validate_object_graph(package_ref)
    package = store.load_object(package_ref)["payload"]
    if (seal.get("candidate_package_digest") != package_ref["sha256"]
            or package["campaign_id"] != qualification["campaign_id"]
            or package["candidate_id"] != qualification["candidate_id"]
            or package["target_version"] != manifest["target_version"]
            or package["candidate_ref"] != qualification["candidate_ref"]
            or package["candidate_build_receipt_ref"] != qualification["build_receipt_ref"]):
        raise ControlError("门禁 subject 的固定首轮制品与精确候选或原构建不同源")
    # 首轮 package 可能早于本次验收 package；从它的真实 Store 引用图
    # 闭合原件，不能假定验收四根已经覆盖首轮。受保护父域仍只交给
    # 原 QualificationBindingProjection 重验；外部坐标统一转回数据根。
    store_projection = QualificationBindingProjection(store)
    pending = [dict(package_ref), dict(qualification["build_receipt_ref"])]
    seen = set()
    while pending:
        reference = pending.pop()
        key = canonical_sha256(reference)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > 20000:
            raise ControlError("subject 首轮 Store 引用图超过固定对象上限")
        if "object_kind" in reference:
            document = store.load_object(reference)
            path = store.object_path(reference)
        elif "receipt_kind" in reference:
            document = store.load_receipt(reference)
            path = store.receipt_path(reference)
        else:
            document = store.load_fact(reference)
            path = (Path(store.root) / "facts" / reference["campaign_id"] / reference["dimension"]
                / f"{reference['sequence']:08d}-{reference['sha256']}.json")
        retain(path, reference["sha256"])
        for binding in store_projection.bindings(document, reference):
            store._verify_external_binding(external, binding)
            store_projection.verify_external_mode(external, binding)
            retain(external / _relative(binding["path"]), binding["sha256"])
        domains = tuple(tuple(row["domain_pointer"].strip("/").split("/"))
            for row in store_projection.manifest()["parent_domains"] if row["parent_ref"] == reference)
        project_parent(path, external, domains)
        pending.extend(iter_fact_refs(document))
        pending.extend(iter_object_refs(document))
        pending.extend(iter_receipt_refs(document))
    release_artifact = store.load_object(stored_build["release_artifact_ref"])["payload"]
    snapshot_ref = release_artifact["snapshot_ref"]
    snapshot = store.load_object(snapshot_ref)["payload"]
    # 画像副本必须与精确 Release 的 Snapshot 完全文档相同；原资格
    # 投影已重放这个 Snapshot 父域。不能凭相同摘要给别处画像授权。
    if parse_json_bytes(profile_raw) != snapshot["document"]:
        raise ControlError("门禁 subject 暂存画像不是精确 Release Snapshot 的原文档")
    project_parent(campaign / release.STAGED_RELEASE_ROOT / profile_relative,
        campaign / release.STAGED_RELEASE_ROOT, ((),))
    from .capturelib.model import ConfigurationError
    try:
        _prove_frozen_active_derivation(store, external, campaign, manifest, build,
            retain=retain, project_parent=project_parent)
    except (ConfigurationError, KeyError, TypeError, ValueError) as error:
        raise ControlError("冻结 Active 派生来源证明不闭合：" + str(error)) from error
    # Campaign 内普通原件的坐标仍以 Campaign 为根；特定父域上面的
    # 显式证明优先。叶子未知 JSON 没有父合同证明，仍交给严格读者。
    campaign_parents = [campaign / "campaign.json", round_path,
        compare._bound_campaign_file(campaign, parent["comparison"], "subject 比较收据"),
        *stage_paths,
        *[data_root / row["path"] for row in originals.values()
          if row["path"].startswith(campaign.relative_to(data_root).as_posix() + "/control/vc/")]]
    for path in campaign_parents:
        if path.relative_to(data_root).as_posix() not in projections:
            project_parent(path, campaign)
    expected = accept.expected_gate_subject(manifest, candidate["payload"], identity)
    if read_bindings is not None:
        read_bindings.extend(originals[path] for path in sorted(originals))
    if read_projections is not None:
        read_projections.extend(projections[path] for path in sorted(projections))
    return {**expected, "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None}


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
        expected_subject = qualification_gate_subject(store, qualification, intake_context.data_root, parent)
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
