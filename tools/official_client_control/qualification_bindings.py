"""Claude 验收资格的有界外部读集投影。

只有完整 Store 父合同承认的 CandidateBuildReceipt.build_identity.parameters
和 Snapshot.payload.document 属于父域元数据。两个域仍重放父语义并实读父原件；
它们内部的同形三字段来源记录不授权打开本轮数据根文件。其余外部绑定完整保留。
两个资格消费者与通用 Store.replay 共用本合同。通用重放只给本轮完整图
已证明属于 Claude 的父域使用投影；其它来源保留原严格形状扫描。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import stat
from typing import Any, Mapping

from . import contracts
from .canonical import (canonical_json_bytes, canonical_sha256, expect_exact_keys,
                        expect_non_negative_int, expect_object, expect_safe_id,
                        parse_json_bytes, resolve_relative, sha256_bytes, validate_external_binding)
from .errors import ControlError

PROJECTION_SCHEMA = "official-client-qualification-binding-projection/v1"
REPLAY_SCHEMA = "claude-upgrade-candidate-qualification-replay/v2"
DELIVERY_SCHEMA = "claude-upgrade-delivery-qualification/v2"
LEGACY_REPLAY_SCHEMA = "claude-upgrade-candidate-qualification-replay/v1"
LEGACY_DELIVERY_SCHEMA = "claude-upgrade-delivery-qualification/v1"
ROOT_FIELDS = ("candidate_ref", "validation_ref", "acceptance_ref", "build_receipt_ref")
REPLAY_FIELDS = {"schema_version", "campaign_id", "candidate_id", "fact_refs",
                 "object_refs", "receipt_refs", "external_bindings", "result"}
DELIVERY_FIELDS = {"schema_version", "candidate", "rollback", "qualification_replays",
                   "candidate_compose", "rollback_compose", "normalized_compose",
                   "allowed_changes", "result"}
DOMAIN_FIELDS = {"parent_ref", "parent_schema", "parent_sha256", "parent_bytes",
                 "domain_kind", "domain_pointer", "domain_sha256", "domain_bytes"}
STRICT_BUILD_PARAMETERS_SCHEMA = "sub2apiplus-candidate-build-parameters/v2"
STRICT_ARTIFACT_ROLES = {"container-healthcheck.sh", "docker-entrypoint.sh", "sub2api"}
DOMAINS = {
    "snapshot_document": ("object_kind", "snapshot", "/payload/document"),
    "candidate_build_parameters": ("receipt_kind", "candidate_build", "/build_identity/parameters"),
}


def _receipt_kind(schema: Any) -> str | None:
    for kind, versions in contracts.RECEIPT_SCHEMA_VERSIONS.items():
        if schema in versions:
            return kind
    if schema in {contracts.CANDIDATE_DELIVERY_RECEIPT_V3_SCHEMA,
                  contracts.CANDIDATE_DELIVERY_RECEIPT_V4_SCHEMA}:
        return "candidate_delivery"
    if schema == contracts.PREDECESSOR_IMPORT_RECEIPT_SCHEMA:
        return "predecessor_import"
    return None


class QualificationBindingProjection:
    """一次资格读取的投影；没有跨操作缓存，也不允许调用方声明任意元数据域。"""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._domains: dict[str, dict[str, Any]] = {}
        self._artifact_requirements: dict[str, dict[str, Any]] = {}
        self._semantic_parents: set[str] = set()

    def _parent_domain(self, document: dict[str, Any], reference: Any,
                       kind: str, domain: Any) -> None:
        """父域权限来自原 Store、schema、语义和内容寻址引用，不能由嵌套 schema 字符串授予。"""
        from .gates import WorkflowGates
        from .receipts import replay_receipt

        key_field, expected_kind, pointer = DOMAINS[kind]
        if key_field == "object_kind":
            parsed = contracts.validate_object_ref(reference, "qualification.parent_ref")
            parent = self.store.load_object(parsed)
            raw = self.store.object_bytes(parsed)
        else:
            parsed = contracts.validate_receipt_ref(reference, "qualification.parent_ref")
            parent = self.store.load_receipt(parsed)
            path = Path(self.store.root) / "receipts" / parsed["receipt_kind"] / (parsed["sha256"] + ".json")
            if path.is_symlink() or not path.is_file():
                raise ControlError("资格元数据父收据不是可信原件")
            raw = path.read_bytes()
            # 原读者再次核权限、摘要与完整合同，避免只靠一次裸文件读取。
            self.store.load_receipt(parsed)
        if parsed[key_field] != expected_kind or parent != document:
            raise ControlError("资格元数据父引用与经过完整合同校验的文档不一致")
        if sha256_bytes(raw) != parsed["sha256"] or parse_json_bytes(raw) != document:
            raise ControlError("资格元数据父原件摘要或内容漂移")
        parent_key = canonical_sha256(parsed)
        if parent_key not in self._semantic_parents:
            if key_field == "object_kind":
                WorkflowGates(self.store).validate_object_graph(parsed)
            else:
                replay_receipt(self.store, parsed)
            self._semantic_parents.add(parent_key)
        domain_raw = canonical_json_bytes(domain)
        row = {"parent_ref": dict(parsed), "parent_schema": document["schema_version"],
               "parent_sha256": sha256_bytes(raw), "parent_bytes": len(raw),
               "domain_kind": kind, "domain_pointer": pointer,
               "domain_sha256": sha256_bytes(domain_raw), "domain_bytes": len(domain_raw)}
        row_key = canonical_sha256({"parent_ref": parsed, "domain_pointer": pointer})
        previous = self._domains.setdefault(row_key, row)
        if previous != row:
            raise ControlError("资格元数据父域在同一次读取中发生变化")

    def bindings(self, value: Any, parent_ref: Any = None) -> list[dict[str, Any]]:
        """只从已知顶层合同导出外部读集；未知 Kind／结构严格拒绝。"""
        document = expect_object(value, "qualification.document")
        schema = document.get("schema_version")
        excluded: set[tuple[str, ...]] = set()
        if schema in {contracts.CAMPAIGN_SCHEMA, contracts.CAMPAIGN_V2_SCHEMA}:
            contracts.validate_campaign(document)
        elif schema in {contracts.FACT_SCHEMA, contracts.FACT_V2_SCHEMA}:
            # _sha256 是原 Store.list_facts 附带的已校验索引字段，不属于持久化事实合同。
            contracts.validate_fact_document({key: item for key, item in document.items() if key != "_sha256"})
        elif schema == contracts.OBJECT_SCHEMA:
            contracts.validate_object_document(document)
            if document["object_kind"] == "snapshot":
                self._parent_domain(document, parent_ref, "snapshot_document", document["payload"]["document"])
                excluded.add(("payload", "document"))
        else:
            receipt_kind = _receipt_kind(schema)
            if receipt_kind is None:
                raise ControlError("资格投影不接受未登记的顶层文档合同")
            contracts.validate_receipt_document(document, receipt_kind)
            if receipt_kind == "candidate_build":
                self._parent_domain(document, parent_ref, "candidate_build_parameters", document["build_identity"]["parameters"])
                excluded.add(("build_identity", "parameters"))
                # 生产角色是完整父收据中已校验的库存合同字段，不从文件路径猜用途。
                # 参数合同和生产库存互相约束：缺少 schema 也不能让真实库存降级。
                # 原 offline/backend 合成库存没有声明生产角色，仍保留原合同。
                parameters = document["build_identity"]["parameters"]
                entries = document["artifact_inventory"]["entries"]
                roles = [item["role"] for item in entries]
                if parameters.get("schema_version") == STRICT_BUILD_PARAMETERS_SCHEMA or STRICT_ARTIFACT_ROLES.intersection(roles):
                    if parameters.get("schema_version") != STRICT_BUILD_PARAMETERS_SCHEMA:
                        raise ControlError("真实 Claude 构建库存缺少已冻结的严格 v2 参数合同，不能降为合成库存")
                    if sorted(roles) != sorted(STRICT_ARTIFACT_ROLES):
                        raise ControlError("严格 Claude 构建资格必须覆盖三件真实制品闭集")
                    for item in entries:
                        binding = item["binding"]
                        requirement = {"parent_ref": dict(parent_ref), "relative_path": binding["path"],
                                       "content_sha256": binding["sha256"], "content_bytes": binding["bytes"],
                                       "required_mode": "0755"}
                        self._artifact_requirements[canonical_sha256(requirement)] = requirement
        found: dict[str, dict[str, Any]] = {}

        def visit(node: Any, coordinate: tuple[str, ...]) -> None:
            if coordinate in excluded:
                return
            if isinstance(node, dict):
                if set(node) == {"path", "sha256", "bytes"}:
                    binding = validate_external_binding(node, "qualification.external_binding")
                    old = found.setdefault(binding["path"], dict(binding))
                    if old != binding:
                        raise ControlError("资格外部读集对同一路径声明不同字节")
                    return
                for key, child in node.items():
                    visit(child, (*coordinate, key))
            elif isinstance(node, (list, tuple)):
                for index, child in enumerate(node):
                    visit(child, (*coordinate, str(index)))
        visit(document, ())
        return [found[key] for key in sorted(found)]

    def verify_external_mode(self, external_root: Path, binding: Mapping[str, Any]) -> None:
        """只核父生产合同导出的制品模式，原外部读者仍负责所有文件的路径、SHA 与大小。"""
        if any(row["relative_path"] == binding["path"] for row in self._artifact_requirements.values()):
            path = resolve_relative(external_root, binding["path"])
            if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o755:
                raise ControlError("严格 Claude 构建的冻结制品 0755 权限漂移")

    def manifest(self) -> dict[str, Any]:
        """记录父原字节与域规范摘要；字段形状不会再次被误认成外部文件绑定。"""
        return {"schema_version": PROJECTION_SCHEMA,
                "parent_domains": deepcopy(sorted(self._domains.values(), key=canonical_sha256)),
                "artifact_file_requirements": deepcopy(sorted(self._artifact_requirements.values(), key=canonical_sha256))}


def qualification_roots(qualification: Mapping[str, Any]) -> dict[str, Any]:
    roots = {key: dict(qualification[key]) for key in ROOT_FIELDS}
    for key in ROOT_FIELDS[:-1]:
        contracts.validate_fact_ref(roots[key], "qualification." + key)
        if roots[key]["campaign_id"] != qualification["campaign_id"]:
            raise ControlError("资格报告的根事实跨 Campaign")
    build = contracts.validate_receipt_ref(roots["build_receipt_ref"], "qualification.build_receipt_ref")
    if build["receipt_kind"] != "candidate_build":
        raise ControlError("资格报告的构建根类型错误")
    return roots


def validate_qualification_roots(store: Any, qualification: Mapping[str, Any]) -> dict[str, Any]:
    """独立核冻结四根的类型、归属和关联；不能用报告里的数组替代真实根图。"""
    from .gates import WorkflowGates

    roots = qualification_roots(qualification)
    expect_safe_id(qualification["candidate_id"], "qualification.candidate_id")
    campaign = store.load_campaign(qualification["campaign_id"])
    if campaign.get("schema_version") != contracts.CAMPAIGN_V2_SCHEMA or campaign.get("client") != "claude":
        raise ControlError("资格四根必须来自 Claude Campaign v2")
    facts = {}
    for key, kind in (("candidate_ref", "candidate_frozen"), ("validation_ref", "validation_completed"),
                      ("acceptance_ref", "acceptance_recorded")):
        fact = store.load_fact(roots[key])
        if (fact["fact_kind"] != kind or fact["campaign_id"] != qualification["campaign_id"]
                or fact["payload"].get("candidate_id") != qualification["candidate_id"]):
            raise ControlError("资格四根事实类型、Campaign 或 Candidate 归属错误")
        facts[key] = fact["payload"]
    build = store.load_receipt(roots["build_receipt_ref"])
    if build["campaign_id"] != qualification["campaign_id"] or build["candidate_id"] != qualification["candidate_id"]:
        raise ControlError("资格构建根跨 Campaign 或 Candidate")
    candidate, validation, acceptance = (facts[key] for key in ("candidate_ref", "validation_ref", "acceptance_ref"))
    expected = {"candidate_ref": roots["candidate_ref"], "candidate_build_receipt_ref": roots["build_receipt_ref"]}
    if (candidate.get("candidate_build_receipt_ref") != roots["build_receipt_ref"]
            or any(acceptance.get(key) != value or validation.get(key) != value for key, value in expected.items())
            or validation.get("acceptance_ref") != roots["acceptance_ref"] or validation.get("result") != "passed"
            or validation.get("candidate_evidence_package_ref") != acceptance.get("candidate_evidence_package_ref")):
        raise ControlError("资格四根未闭合同一构建、Candidate、Acceptance 和完整 VC-5")
    WorkflowGates(store)._candidate_build_matches(campaign, candidate, build)
    return roots


def _material_roots(qualification: Mapping[str, Any], trusted_data_root: Path | None,
                    external_root: Path | None) -> tuple[Path, Path]:
    """材料根只来自调用方已确认的路径；报告只能持有冻结的相对坐标，不能授予绝对根。"""
    if trusted_data_root is None:
        raise ControlError("v2 资格报告核验缺少调用方已验证的可信数据根")
    data = Path(trusted_data_root)
    if not data.is_absolute() or data.is_symlink() or not data.is_dir():
        raise ControlError("资格可信数据根必须是已有、非符号链接的绝对目录")
    data = data.resolve(strict=True)
    relative = qualification.get("external_root_relative")
    external = data if relative == "." else resolve_relative(data, relative)
    if not external.is_dir():
        raise ControlError("冻结资格材料根不是现存目录")
    if external_root is not None and Path(external_root).resolve(strict=True) != external:
        raise ControlError("资格材料根与冻结相对坐标不一致，禁止多根搜索")
    return data, external


def _reference_list(value: Any, validator: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ControlError(label + " 必须是数组")
    rows = [validator(row, label) for row in value]
    if [canonical_sha256(row) for row in rows] != sorted({canonical_sha256(row) for row in rows}):
        raise ControlError(label + " 必须按引用摘要排序且不重复")
    return rows


def validate_projection_manifest(value: Any, store: Any, object_refs: list[dict[str, Any]],
                                 receipt_refs: list[dict[str, Any]], *, external_root: Path | None = None) -> None:
    manifest = expect_object(value, "qualification.binding_projection")
    expect_exact_keys(manifest, {"schema_version", "parent_domains", "artifact_file_requirements"}, "qualification.binding_projection")
    if manifest["schema_version"] != PROJECTION_SCHEMA or not isinstance(manifest["parent_domains"], list):
        raise ControlError("资格父域清单版本或结构错误")
    for row in manifest["parent_domains"]:
        item = expect_object(row, "qualification.parent_domain")
        expect_exact_keys(item, DOMAIN_FIELDS, "qualification.parent_domain")
        if item["domain_kind"] not in DOMAINS:
            raise ControlError("资格父域种类未登记")
        expect_non_negative_int(item["parent_bytes"], "qualification.parent_bytes")
        expect_non_negative_int(item["domain_bytes"], "qualification.domain_bytes")
    projected = QualificationBindingProjection(store)
    for reference in object_refs:
        if reference["object_kind"] == "snapshot":
            projected.bindings(store.load_object(reference), reference)
    for reference in receipt_refs:
        if reference["receipt_kind"] == "candidate_build":
            projected.bindings(store.load_receipt(reference), reference)
    if manifest != projected.manifest():
        raise ControlError("资格父域清单与原父引用、字节、完整合同或规范摘要不一致")
    if manifest["artifact_file_requirements"] and external_root is None:
        raise ControlError("严格 Claude 制品模式核验缺少受管外部材料根")
    for requirement in manifest["artifact_file_requirements"]:
        binding = {"path": requirement["relative_path"], "sha256": requirement["content_sha256"], "bytes": requirement["content_bytes"]}
        store._verify_external_binding(external_root, binding)
        projected.verify_external_mode(external_root, binding)


def validate_replay_report(value: Any, qualification: Mapping[str, Any], store: Any,
                           *, legacy: bool = False, external_root: Path | None = None,
                           trusted_data_root: Path | None = None, binding_base: str = "data_root") -> None:
    report = expect_object(value, "qualification.replay_report")
    expected = REPLAY_FIELDS if legacy else REPLAY_FIELDS | {"qualification_roots", "binding_projection", "external_bindings_base"}
    expect_exact_keys(report, expected, "qualification.replay_report")
    if report["schema_version"] != (LEGACY_REPLAY_SCHEMA if legacy else REPLAY_SCHEMA) or report["result"] != "pass":
        raise ControlError("资格重放报告版本错误或未通过")
    for key in ("campaign_id", "candidate_id"):
        expect_safe_id(report[key], "qualification." + key)
        if report[key] != qualification[key]:
            raise ControlError("资格重放报告与冻结身份不一致")
    facts = _reference_list(report["fact_refs"], contracts.validate_fact_ref, "qualification.fact_refs")
    objects = _reference_list(report["object_refs"], contracts.validate_object_ref, "qualification.object_refs")
    receipts = _reference_list(report["receipt_refs"], contracts.validate_receipt_ref, "qualification.receipt_refs")
    if not isinstance(report["external_bindings"], list):
        raise ControlError("资格报告外部绑定必须是数组")
    bindings = [validate_external_binding(row, "qualification.external_bindings") for row in report["external_bindings"]]
    if [row["path"] for row in bindings] != sorted({row["path"] for row in bindings}):
        raise ControlError("资格报告外部绑定必须按路径排序且不重复")
    if not legacy:
        if binding_base not in {"data_root", "external_root"} or report["external_bindings_base"] != binding_base:
            raise ControlError("资格报告外部坐标基准错误，交付只能使用已冻结的数据根坐标")
        data, external = _material_roots(qualification, trusted_data_root, external_root)
        from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import _QualificationStoreView
        from .store import ControlStore
        from .delivery_execution import _replay_qualification_subgraph

        # 原 Store 先完整验链；单次只读索引仅复用已校验记录，结束再完整核原字节。
        reader = _QualificationStoreView(ControlStore.open_read_only(store.root))
        roots = validate_qualification_roots(reader, qualification)
        if report["qualification_roots"] != roots or roots["build_receipt_ref"] not in receipts:
            raise ControlError("资格报告未绑定完整冻结根集合")
        required = _replay_qualification_subgraph(reader, qualification, trusted_data_root=data)
        for key, declared in (("fact_refs", facts), ("object_refs", objects), ("receipt_refs", receipts)):
            if not {canonical_sha256(row) for row in required[key]} <= {canonical_sha256(row) for row in declared}:
                raise ControlError("资格报告遗漏冻结四根可达的必需 " + key)
        # 额外历史引用也必须走原读者和原语义，不能借「至少覆盖」夹入交付分支或任意合同。
        from .gates import WorkflowGates
        from .receipts import FormedContext, replay_receipt
        from collections import defaultdict
        campaign = reader.load_campaign(qualification["campaign_id"])
        history = reader.list_facts(qualification["campaign_id"])
        context = FormedContext(reader, campaign)
        declared_projection = QualificationBindingProjection(reader)
        raw_bindings: dict[str, dict[str, Any]] = {}

        def collect(document: dict[str, Any], reference: dict[str, Any] | None = None) -> None:
            for binding in declared_projection.bindings(document, reference):
                previous = raw_bindings.setdefault(binding["path"], binding)
                if previous != binding:
                    raise ControlError("资格报告声明引用对同一路径绑定不同字节")

        for reference in facts:
            if reference["campaign_id"] != qualification["campaign_id"]:
                raise ControlError("资格报告额外事实跨 Campaign")
            fact = reader.load_fact(reference)
            if reference not in required["fact_refs"]:
                without_current: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for item in history:
                    if reader.fact_ref(item) != reference:
                        without_current[item["fact_kind"]].append(item)
                from .contracts import iter_fact_refs
                context.require_causality(reader, context.coordinates(fact), iter_fact_refs(fact["payload"]), "资格报告额外事实")
                WorkflowGates(reader)._validate_fact_semantics(campaign, fact, without_current, context)
            collect(fact)
        for reference in objects:
            WorkflowGates(reader).validate_object_graph(reference)
            collect(reader.load_object(reference), reference)
        for reference in receipts:
            replay_receipt(reader, reference)
            collect(reader.load_receipt(reference), reference)
        validate_projection_manifest(report["binding_projection"], reader, objects, receipts, external_root=external)
        for key in ("parent_domains", "artifact_file_requirements"):
            if not {canonical_sha256(row) for row in required["binding_projection"][key]} <= {
                    canonical_sha256(row) for row in report["binding_projection"][key]}:
                raise ControlError("资格报告遗漏冻结四根可达的必需父域或制品要求")
        required_bindings = {}
        for binding in [*required["external_bindings"], *raw_bindings.values()]:
            # 内层子图使用 external_root 原坐标；只按冻结根单向转换到报告要求的基准。
            path = resolve_relative(external, binding["path"])
            base = data if binding_base == "data_root" else external
            row = {**binding, "path": path.relative_to(base).as_posix()}
            required_bindings[row["path"]] = row
        declared_bindings = {row["path"]: row for row in bindings}
        if any(declared_bindings.get(path) != row for path, row in required_bindings.items()):
            raise ControlError("资格报告遗漏或伪造冻结四根可达的真实外部绑定")
        base = data if binding_base == "data_root" else external
        for binding in bindings:
            path = resolve_relative(base, binding["path"])
            if not path.is_relative_to(external):
                raise ControlError("资格报告外部绑定离开冻结材料根，禁止多根搜索")
            reader._verify_external_binding(base, binding)
            declared_projection.verify_external_mode(external, {**binding, "path": path.relative_to(external).as_posix()})
        reader.verify_fact_snapshots()


def validate_delivery_report(value: Any, store: Any, *, current_execution: bool,
                             configuration: Mapping[str, Any] | None = None,
                             trusted_data_root: Path | None = None) -> None:
    """新执行必须用 v2；旧 v1 只保留形成期只读合同，不授予本轮写入／派发权。"""
    report = expect_object(value, "qualification.delivery_report")
    legacy = report.get("schema_version") == LEGACY_DELIVERY_SCHEMA
    expect_exact_keys(report, DELIVERY_FIELDS if legacy else DELIVERY_FIELDS | {"configuration_binding"},
                      "qualification.delivery_report")
    if report["schema_version"] not in {DELIVERY_SCHEMA, LEGACY_DELIVERY_SCHEMA} or (current_execution and legacy):
        raise ControlError("当前资格消费拒绝旧报告冒充后继合同")
    if not legacy:
        configuration_binding = validate_external_binding(report["configuration_binding"], "qualification.configuration_binding")
        if current_execution and configuration is None:
            raise ControlError("当前资格报告缺本次已核验的冻结配置")
        if trusted_data_root is not None:
            store._verify_external_binding(trusted_data_root, configuration_binding)
            original = parse_json_bytes(resolve_relative(trusted_data_root, configuration_binding["path"]).read_bytes(),
                                        "qualification.configuration_original")
            if configuration is not None and original != configuration:
                raise ControlError("资格报告配置绑定与本次冻结配置原件不一致")
    if report["result"] != "pass" or report["allowed_changes"] != ["application_image", "application_stage_label", "active_runtime_profile"]:
        raise ControlError("交付资格报告未通过或允许变化不闭合")
    for key in ("candidate_compose", "rollback_compose", "normalized_compose"):
        expect_object(report[key], "qualification." + key)
    replays = report["qualification_replays"]
    if not isinstance(replays, list) or len(replays) != 2:
        raise ControlError("交付资格报告必须包含候选与回退两份重放")
    for index, name in enumerate(("candidate", "rollback")):
        material = expect_object(report[name], "qualification." + name)
        if configuration is not None and material != configuration[name]:
            raise ControlError("交付资格报告与本次配置原件不一致")
        qualification = expect_object(material.get("qualification"), "qualification.frozen_roots")
        external_root = None
        if trusted_data_root is not None:
            relative = qualification.get("external_root_relative")
            external_root = trusted_data_root if relative == "." else resolve_relative(trusted_data_root, relative)
        validate_replay_report(replays[index], qualification, store, legacy=legacy,
                               external_root=external_root, trusted_data_root=trusted_data_root, binding_base="data_root")
