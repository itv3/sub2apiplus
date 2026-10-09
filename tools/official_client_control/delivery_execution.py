"""Claude 隔离候选的真实交付合同。

这里共同校验新写入、收据构建、重放及 VC-6 消费；历史 v1 材料仍走原合同。
来源验证器默认不登记。自摘要正确的上传 JSON 不能替代受管 guard 通道取得的原件。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .canonical import canonical_sha256, expect_image_digest, expect_non_negative_int, expect_object, expect_sha256, expect_string, sha256_bytes
from .errors import ControlError
from .profile_observation_bindings import VerifiedProfileProjection, RUNTIME_IDENTITY_SCHEMA, HOST_IDENTITY_SCHEMA

OPERATIONAL_V2 = "official-client-operational-evidence/v2"
PLAN_V2 = "official-client-candidate-delivery-plan/v2"
FACT_V3 = "official-client-candidate-delivery-fact/v3"
PACKAGE_V2 = "official-client-candidate-delivery-package/v2"
RECEIPT_V4 = "official-client-candidate-delivery-receipt/v4"
EVIDENCE_TYPES = ("configuration", "stable_projection", "freeze", "rollback_state", "stage_execution", "zero_model_probes", "restore", "deployment_configuration")
PLAN_EXTRA_FIELDS = frozenset({"executor_configuration_ref", "stable_environment_projection_ref", "probe_recipe_ref", "rollback_state_ref", "environment_before_ref", "deployment_configuration_ref"})
PACKAGE_EXTRA_FIELDS = frozenset({"execution_evidence_refs", "restoration_evidence_refs", "operation_index_binding", "operation_index_origin", "publication_intent_bindings"})
RECEIPT_EXTRA_FIELDS = frozenset({"executor_configuration_ref", "stable_environment_projection_ref", "probe_recipe_ref", "rollback_state_ref", "deployment_configuration_ref", *PACKAGE_EXTRA_FIELDS})
ORIGIN_FIELDS = frozenset({"authority", "source_scope", "configuration_sha256", "service_instance_id", "operations_index_binding"})
COMMON_FIELDS = frozenset({"contract_version", "result", "campaign_id", "delivery_id", "origin"})
ENVIRONMENT_FIELDS = frozenset({"host_id", "architecture", "compose_sha256", "environment_sha256", "configuration_sha256", "network_sha256"})
GUARD_IDENTITY_FIELDS = frozenset({"contract_version", "authority", "source_scope", "host_id", "instance_id", "service_name", "service_instance_id", "boot_id", "pid", "process_start", "process_group_id", "independent_service", "service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"})
FREEZE_GATES = frozenset({"compose-diff", "dependency-connectivity", "entry-routing", "evidence-inventory", "health", "image-identity", "observation-window", "rollback-availability", "secret-scan"})
RESTORE_CHECKS = frozenset({"auth", "data", "dependencies", "health", "image-identity"})
TYPE_FIELDS = {
    "configuration": {"configuration", "configuration_sha256", "binding"},
    "stable_projection": {"projection", "projection_sha256", "binding"},
    "freeze": {"environment", "before_bindings", "gates", "guard_identity"},
    "rollback_state": {"candidate_ref", "build_receipt_ref", "acceptance_ref", "evidence_package_ref", "image_digest", "runtime_profile_sha256", "bindings"},
    "zero_model_probes": {"mode", "recipe", "request_records", "bindings"},
    "restore": {"operation_id", "target", "actual", "checks", "journal_binding", "terminal_binding", "bindings"},
    "deployment_configuration": {"configuration_binding", "deployment_sha256", "bindings"},
    "stage_execution": {"candidate_id", "stage", "run_nonce", "operation_id", "plan_ref", "configuration_ref", "stable_projection_ref", "probe_recipe_ref", "target", "actual", "environment", "checks", "observed_seconds", "live_request_count", "request_records", "guard_identity", "prepared_binding", "physical_intent_binding", "guard_terminal_binding", "journal_binding", "before_bindings", "during_bindings", "after_bindings", "restoration"},
}


@dataclass(frozen=True)
class VerifiedSource:
    """由受管来源验证器返回的通道结果；待验证包自己不能指定这个结果。"""

    content_sha256: str
    deployment_sha256: str
    bytes_by_path: Mapping[str, bytes]
    configuration_sha256: str
    service_instance_id: str
    trusted_data_root: Path | None = None
    profile_projection: VerifiedProfileProjection | None = None
    guard_continuity: Any = None
    gate_projection: Any = None


# 正式登记须在固定通道、部署身份与原件真实性现场验收后由受管源码完成。
# 测试仅在局部作用域登记夹具，不允许 CLI、环境变量或证据 JSON 动态加载验证器。
SOURCE_VERIFIERS: dict[str, Callable[[Any, Mapping[str, Any], Mapping[str, Any]], VerifiedSource]] = {}


@dataclass
class _SourceBatch:
    """只属于一次只读校验的原件快照，不能由证据文件创建。"""

    store: Any
    verifiers: dict[str, Any]
    sources: dict[str, VerifiedSource]
    active: bool = True


_SOURCE_BATCH: ContextVar[_SourceBatch | None] = ContextVar("claude_delivery_source_batch", default=None)


def _source_request(content: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    return {"content": {key: value for key, value in content.items() if key != "origin"}, "origin": dict(origin)}


def _graph_source_items(store: Any, payload: Mapping[str, Any],
                        frozen_configuration_ref: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    validate_operational_evidence(dict(payload))
    facts = payload["entries"][0]["facts"]
    items = [_source_request(facts, facts["origin"])]
    reference = facts["configuration_ref"] if payload["evidence_type"] == "stage_execution" else frozen_configuration_ref
    if reference is not None:
        original = evidence_facts(store, reference, "configuration")
        items.append(_source_request(original, original["origin"]))
    return items


def _plan_source_items(store: Any, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = []
    for key in (*sorted(PLAN_EXTRA_FIELDS), "environment_freeze_ref"):
        items.extend(_graph_source_items(store, store.load_object(plan[key])["payload"],
            plan["executor_configuration_ref"] if key in {"environment_before_ref", "environment_freeze_ref"} else None))
    return items


@contextmanager
def _source_batch_scope(store: Any, items: list[dict[str, Any]]):
    """只在精确预声明的只读图内承接来源；独立调用重新取得通道证明。"""
    unique = {canonical_sha256(item): item for item in items}
    previous = _SOURCE_BATCH.get()
    if previous is not None:
        if not previous.active or previous.store is not store or not set(unique) <= set(previous.sources):
            raise ControlError("批量来源被用于其他 Store 或未声明的来源")
        yield
        return
    verifiers = {item["origin"]["authority"]: SOURCE_VERIFIERS.get(item["origin"]["authority"]) for item in unique.values()}
    # 老来源登记继续走单项合同；批量能力只能随可信登记提供，不能从 JSON 选择。
    if not verifiers or any(not callable(getattr(verifier, "verify_batch", None)) for verifier in verifiers.values()):
        yield
        return
    sources = {}
    for authority, verifier in verifiers.items():
        selected = [(key, item) for key, item in unique.items() if item["origin"]["authority"] == authority]
        results = verifier.verify_batch(store, [item for _, item in selected])
        if not isinstance(results, list) or len(results) != len(selected):
            raise ControlError("批量来源验证器未返回精确逐项结果")
        for (key, item), source in zip(selected, results):
            _check_verified_source(source, item["content"], item["origin"])
            sources[key] = source
    batch = _SourceBatch(store, verifiers, sources)
    token = _SOURCE_BATCH.set(batch)
    try:
        yield
    finally:
        batch.active = False
        batch.sources.clear()
        _SOURCE_BATCH.reset(token)


def _exact(value: Any, fields: Any, label: str) -> dict[str, Any]:
    payload = expect_object(value, label)
    if set(payload) != set(fields):
        raise ControlError(f"{label} 字段不闭合")
    return payload


def _binding(value: Any, label: str) -> dict[str, Any]:
    from .contracts import validate_external_binding
    return validate_external_binding(value, label)


def _object_ref(value: Any, kind: str, label: str) -> dict[str, Any]:
    from .contracts import validate_object_ref
    reference = validate_object_ref(value, label)
    if reference["object_kind"] != kind:
        raise ControlError(f"{label} 引用类型错误")
    return reference


def _origin(value: Any) -> dict[str, Any]:
    origin = _exact(value, ORIGIN_FIELDS, "delivery.origin")
    expect_string(origin["authority"], "delivery.origin.authority")
    if origin["source_scope"] not in {"formal", "regression_fixture"}:
        raise ControlError("交付来源用途非法")
    expect_sha256(origin["configuration_sha256"], "delivery.origin.configuration_sha256")
    expect_string(origin["service_instance_id"], "delivery.origin.service_instance_id")
    if origin["operations_index_binding"] is not None:
        _binding(origin["operations_index_binding"], "delivery.origin.operations_index_binding")
    return origin


def typed_bindings(value: Any) -> list[dict[str, Any]]:
    """遍历标准三字段绑定；不依赖 private-* 名字，不遗漏失败／恢复／未发布操作。"""

    found: dict[str, dict[str, Any]] = {}
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if set(node) == {"path", "sha256", "bytes"}:
                binding = _binding(node, "delivery.binding")
                previous = found.get(binding["path"])
                if previous is not None and previous != binding:
                    raise ControlError("同一交付原件路径绑定了不同字节")
                found[binding["path"]] = binding
            else:
                for child in node.values():
                    visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
    visit(value)
    return [found[path] for path in sorted(found)]


def validate_publication_bindings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError("交付发布意图绑定必须是非空数组")
    rows = [_binding(row, "delivery.publication_intent_binding") for row in value]
    if [row["path"] for row in rows] != sorted({row["path"] for row in rows}):
        raise ControlError("交付发布意图绑定必须按路径排序且不重复")
    return rows


def _verified_source(store: Any, content: Mapping[str, Any], origin: Mapping[str, Any]) -> VerifiedSource:
    _origin(origin)
    verifier = SOURCE_VERIFIERS.get(str(origin["authority"]))
    if verifier is None:
        raise ControlError("Claude 真实交付来源尚未完成受管通道验收，不能发布或消费正式执行证明")
    core = {key: value for key, value in content.items() if key != "origin"}
    batch = _SOURCE_BATCH.get()
    if batch is None:
        verified = verifier(store, core, origin)
    else:
        if not batch.active or batch.store is not store or batch.verifiers.get(origin["authority"]) is not verifier:
            raise ControlError("本次批量来源的 Store、通道登记或生命周期已变化")
        verified = batch.sources.get(canonical_sha256(_source_request(content, origin)))
        if not isinstance(verified, VerifiedSource) or verified.trusted_data_root is None:
            raise ControlError("请求未列入本次批量来源，不能承接其他调用的证明")
        # 快照只省来源导出；每次消费仍对照真实原件，不能掩盖批量后字节漂移。
        live = replace(verified, bytes_by_path={})
        for path, raw in verified.bytes_by_path.items():
            _source_bytes(live, {"path": path, "sha256": sha256_bytes(raw), "bytes": len(raw)})
    _check_verified_source(verified, core, origin)
    if batch is not None and verified.guard_continuity is not None:
        from .canonical import parse_json_bytes
        from tools.official_client_capture.claude_upgrade_delivery_recovery_guard import owner_alive
        identity = parse_json_bytes(verified.guard_continuity.channel_identity_bytes)
        if not owner_alive({key: identity[key] for key in ("boot_id", "pid", "process_start", "process_group_id")}):
            raise ControlError("批量取证后的当前守卫实例已消失或 PID 被重用")
    for binding in typed_bindings(content):
        _source_bytes(verified, binding)
    return verified


def _check_verified_source(verified: VerifiedSource, core: Mapping[str, Any], origin: Mapping[str, Any]) -> None:
    _origin(origin)
    if not isinstance(verified, VerifiedSource):
        raise ControlError("交付来源验证器未返回受管原件结果")
    if verified.content_sha256 != canonical_sha256(core) or verified.configuration_sha256 != origin["configuration_sha256"] or verified.service_instance_id != origin["service_instance_id"]:
        raise ControlError("交付来源、部署与完整执行内容不一致")
    expect_sha256(verified.deployment_sha256, "delivery.verified_deployment_sha256")
    _revalidate_guard_continuity(verified, required=origin["source_scope"] == "formal")


def _source_bytes(source: VerifiedSource, binding: Mapping[str, Any]) -> bytes:
    parsed = _binding(dict(binding), "delivery.source_binding")
    raw = source.bytes_by_path.get(parsed["path"])
    if raw is None and source.trusted_data_root is not None:
        root = source.trusted_data_root
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ControlError("受管交付来源没有可信数据根")
        path = root
        for part in Path(parsed["path"]).parts:
            path = path / part
            if path.is_symlink():
                raise ControlError("交付原件路径包含符号链接")
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"交付原件缺失：{parsed['path']}")
        raw = path.read_bytes()
    if not isinstance(raw, bytes) or len(raw) != parsed["bytes"] or sha256_bytes(raw) != parsed["sha256"]:
        raise ControlError(f"受管来源原件缺失或字节漂移：{parsed['path']}")
    return raw


def _revalidate_guard_continuity(source: VerifiedSource, *, required: bool = False) -> Any:
    """只读重验来源验证器交回的纯合同对象，不能接受上传 JSON 或旗标。"""
    proof = source.guard_continuity
    if proof is None:
        if required:
            raise ControlError("当前真实守卫来源缺完整 epoch 连续性证明")
        return None
    from tools.official_client_capture.claude_upgrade_guard_epochs import VerifiedGuardContinuity
    if not isinstance(proof, VerifiedGuardContinuity):
        raise ControlError("guard 连续性必须为已验证的内存合同，不能由 JSON 授权")
    if (proof.source_content_sha256 != source.content_sha256
            or proof.configuration_sha256 != source.configuration_sha256
            or proof.source_service_instance_id != source.service_instance_id):
        raise ControlError("guard 连续性与来源内容、配置或原 producer 不一致")
    proof.revalidate(lambda item: _source_bytes(source, item), purpose="formed_replay")
    return proof


def _same_verified_guard_source_deployment(left: VerifiedSource, right: VerifiedSource) -> bool:
    """原实例比较继续保留；跨实例仅接受完整注册链和同一当前通道。"""
    left_proof, right_proof = _revalidate_guard_continuity(left), _revalidate_guard_continuity(right)
    if left_proof is None and right_proof is None:
        return left.service_instance_id == right.service_instance_id
    from tools.official_client_capture.claude_upgrade_guard_epochs import same_verified_guard_deployment
    return same_verified_guard_deployment(left_proof, right_proof)


def complete_raw_bindings(source: VerifiedSource, initial: Any) -> list[dict[str, Any]]:
    """跟随已绑定 JSON 原件，覆盖 journal 与意图内的下层原件，不能只归档第一层。"""

    return _complete_raw_bindings(source, initial, {})


def _complete_raw_bindings(source: VerifiedSource, initial: Any,
                           profile_documents: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """完整读验父文件后，仅在已验真配置明确声明的精确 Profile 坐标结束递归。"""

    import json
    guard_proof = _revalidate_guard_continuity(source)
    if source.profile_projection is not None:
        if not isinstance(source.profile_projection, VerifiedProfileProjection):
            raise ControlError("Profile 归档域必须来自纯来源校验器，不能由 JSON manifest 授权")
        source.profile_projection.revalidate(lambda row: _source_bytes(source, row), purpose="formed_replay")
    bindings = {row["path"]: row for row in typed_bindings(initial)}
    if guard_proof is not None:
        for item in guard_proof.read_bindings:
            previous = bindings.setdefault(item.path, item.as_value())
            if previous != item.as_value():
                raise ControlError("guard 连续性归档对同一路径引用不同原件")
    for row in bindings.values():
        if Path(row["path"]).name in {"profile-identity.json", "host-profile-identity.json"}:
            identity = _source_json(source, row)
            if identity.get("schema_version") in {RUNTIME_IDENTITY_SCHEMA, HOST_IDENTITY_SCHEMA}:
                if source.profile_projection is None:
                    raise ControlError("当前 Docker Profile 观测副本缺少已验证来源证明，不能降为宽松归档")
                source.profile_projection.require_identity_parent(row, _source_bytes(source, row))
    queue = list(bindings)
    while queue:
        path = queue.pop()
        raw = _source_bytes(source, bindings[path])
        if profile_documents.get(path) == bindings[path]:
            # Profile 的历史来源元数据仍由原资格图解释；本次归档保留其完整父字节。
            continue
        try:
            content = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(content, dict) and content.get("schema_version") in {RUNTIME_IDENTITY_SCHEMA, HOST_IDENTITY_SCHEMA}:
            if source.profile_projection is None:
                raise ControlError("当前 Docker Profile 观测副本缺少已验证来源证明，不能降为宽松归档")
            source.profile_projection.require_identity_parent(bindings[path], raw)
        if source.profile_projection is not None:
            projected = source.profile_projection.project(bindings[path], raw)
            if projected is not None:
                children = projected
            else:
                children = typed_bindings(content)
        else:
            children = typed_bindings(content)
        for row in children:
            old = bindings.get(row["path"])
            if old is not None and old != row:
                raise ControlError("交付原件递归读集对同一路径声明不同字节")
            if old is None:
                bindings[row["path"]] = row
                queue.append(row["path"])
    return [bindings[path] for path in sorted(bindings)]


def complete_delivery_raw_bindings(store: Any, plan: Mapping[str, Any], source: VerifiedSource,
                                  initial: Any) -> list[dict[str, Any]]:
    """交付归档的两份 Profile 边界只能由同 Plan 的受管冻结配置推出。

    不接受证据自报跳过字段，也不按文件名、schema 或相同摘要选择其他路径。
    配置原件和来源实例先验真；两份 Profile 的精确绑定始终完整读验并归档。
    其余 JSON（包括日志、资格报告和父发布意图）继续递归验证下层原件。
    """

    if plan.get("schema_version") != PLAN_V2:
        raise ControlError("交付归档 Profile 边界要求同一 Plan v2")
    reference = plan["executor_configuration_ref"]
    payload = store.load_object(dict(reference))["payload"]
    from tools.official_client_capture.claude_gate_material_worker import gate_material_reader_scope
    with gate_material_reader_scope(), _source_batch_scope(store, _graph_source_items(store, payload)):
        return _complete_delivery_raw_bindings(store, plan, source, initial)


def _complete_delivery_raw_bindings(store: Any, plan: Mapping[str, Any], source: VerifiedSource,
                                    initial: Any) -> list[dict[str, Any]]:
    """配置原件先验真，随后完整投影一次并立即构造归档闭包。"""
    facts = evidence_facts(store, plan["executor_configuration_ref"], "configuration")
    configuration_source = _verified_source(store, facts, facts["origin"])
    _validate_configuration_original(store, facts, configuration_source, current_execution=False)
    if (facts["campaign_id"] != plan["campaign_id"]
            or facts["delivery_id"] != plan["delivery_id"]
            or facts["result"] != "pass"
            or facts["configuration_sha256"] != plan["configuration_sha256"]
            or source.configuration_sha256 != plan["configuration_sha256"]
            or configuration_source.configuration_sha256 != source.configuration_sha256
            or not _same_verified_guard_source_deployment(configuration_source, source)
            or configuration_source.deployment_sha256 != source.deployment_sha256
            or configuration_source.trusted_data_root != source.trusted_data_root):
        raise ControlError("交付归档配置、Plan、受管根或来源实例不一致")
    configuration = _source_json(configuration_source, facts["binding"])
    if configuration != facts["configuration"] or canonical_sha256(configuration) != source.configuration_sha256:
        raise ControlError("交付归档冻结配置原件与已验证来源不一致")
    projection = source.profile_projection
    if projection is not None and (not isinstance(projection, VerifiedProfileProjection)
            or projection.configuration_sha256 != source.configuration_sha256
            or projection.deployment_sha256 != source.deployment_sha256):
        raise ControlError("交付归档 Profile 派生域不是同一冻结配置及部署的已验证来源")
    profiles: dict[str, dict[str, Any]] = {}
    for name, digest_key in (("candidate", "runtime_profile_sha256"),
                             ("rollback", "rollback_runtime_profile_sha256")):
        material = configuration[name]
        binding = _binding(material["profile_binding"], f"delivery.archive.{name}.profile_binding")
        if (binding["sha256"] != material["runtime_profile_sha256"]
                or binding["sha256"] != plan[digest_key]):
            raise ControlError("交付归档 Profile 原件不是同一冻结 Plan 的运行材料")
        previous = profiles.setdefault(binding["path"], binding)
        if previous != binding:
            raise ControlError("交付归档两份 Profile 对同一路径声明不同字节")
        _source_bytes(configuration_source, binding)
        _source_bytes(source, binding)
    from .qualification_material_import import projection_for_configuration
    from .sealed_gate_bindings import project_delivery_raw_bindings
    gate_projection = projection_for_configuration(store, configuration, configuration_source, purpose="formed_replay")
    proof_bindings = [*projection.proof_bindings, *projection.material_bindings()] if projection is not None else []
    return project_delivery_raw_bindings(source, [initial, list(profiles.values()), proof_bindings],
                                        gate_projection=gate_projection, profile_projection=projection)


def _source_json(source: VerifiedSource, binding: Mapping[str, Any]) -> dict[str, Any]:
    import json
    try:
        payload = json.loads(_source_bytes(source, binding))
    except (UnicodeDecodeError, ValueError) as error:
        raise ControlError("受管交付原件不是合法 JSON") from error
    return expect_object(payload, "delivery.source_json")


def publication_document(source: VerifiedSource, binding: Mapping[str, Any], campaign_id: str, delivery_id: str, operation_id: str) -> dict[str, Any]:
    """父发布意图只从通道已核实的数据根读取；guard 不反向引用 Store。"""

    import json
    parsed = _binding(dict(binding), "delivery.publication_intent_binding")
    expected = f"evidence/campaigns/{campaign_id}/control/delivery/{delivery_id}/publication/{operation_id}.json"
    if parsed["path"] != expected:
        raise ControlError("交付发布意图不在固定父编排器坐标")
    if source.trusted_data_root is None:
        # 回归夹具可在测试作用域直接提供原件；正式登记不能来自上传 JSON。
        raw = _source_bytes(source, parsed)
    else:
        root = source.trusted_data_root
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ControlError("受管交付来源没有可信数据根")
        path = root
        for part in Path(parsed["path"]).parts:
            path = path / part
            if path.is_symlink():
                raise ControlError("交付发布意图路径包含符号链接")
        if path.is_symlink() or not path.is_file():
            raise ControlError("交付发布意图原件缺失")
        raw = path.read_bytes()
        if len(raw) != parsed["bytes"] or sha256_bytes(raw) != parsed["sha256"]:
            raise ControlError("交付发布意图原件摘要漂移")
    try:
        return expect_object(json.loads(raw), "delivery.publication_intent")
    except (UnicodeDecodeError, ValueError) as error:
        raise ControlError("交付发布意图原件不是合法 JSON") from error


def _checks(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError(f"{label} 必须是非空数组")
    rows = []
    for row in value:
        item = _exact(row, {"id", "result", "facts"}, label)
        expect_string(item["id"], f"{label}.id")
        if item["result"] not in {"pass", "failed"}:
            raise ControlError(f"{label}.result 非法")
        expect_object(item["facts"], f"{label}.facts")
        rows.append(item)
    if len({row["id"] for row in rows}) != len(rows):
        raise ControlError(f"{label} 检查项重复")
    return rows


def _actual(value: Any, label: str) -> dict[str, Any]:
    actual = _exact(value, {"image", "profile"}, label)
    for key, validator in (("image", expect_image_digest), ("profile", expect_sha256)):
        item = expect_object(actual[key], f"{label}.{key}")
        if item.get("state") == "known":
            _exact(item, {"state", "value", "direct_binding"}, f"{label}.{key}")
            validator(item["value"], f"{label}.{key}.value")
            _binding(item["direct_binding"], f"{label}.{key}.direct_binding")
        elif item.get("state") == "unknown":
            _exact(item, {"state", "reason"}, f"{label}.{key}")
            expect_string(item["reason"], f"{label}.{key}.reason")
        else:
            raise ControlError(f"{label}.{key} 必须明确 known 或 unknown")
    return actual


def _target(value: Any) -> dict[str, Any]:
    target = _exact(value, {"image_digest", "runtime_profile_sha256"}, "delivery.target")
    expect_image_digest(target["image_digest"], "delivery.target.image_digest")
    expect_sha256(target["runtime_profile_sha256"], "delivery.target.runtime_profile_sha256")
    return target


def _bindings(value: Any, label: str, *, non_empty: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (non_empty and not value):
        raise ControlError(f"{label} 必须是原件绑定数组")
    rows = [_binding(row, label) for row in value]
    if len({row["path"] for row in rows}) != len(rows):
        raise ControlError(f"{label} 不得重复绑定原件")
    return rows


def _environment(value: Any) -> dict[str, Any]:
    from .contracts import expect_safe_id
    environment = _exact(value, ENVIRONMENT_FIELDS, "delivery.environment")
    expect_safe_id(environment["host_id"], "delivery.environment.host_id")
    expect_string(environment["architecture"], "delivery.environment.architecture")
    for key in ENVIRONMENT_FIELDS - {"host_id", "architecture"}:
        expect_sha256(environment[key], f"delivery.environment.{key}")
    return environment


def _guard_identity(value: Any, origin: Mapping[str, Any]) -> dict[str, Any]:
    from .contracts import expect_safe_id
    guard = _exact(value, GUARD_IDENTITY_FIELDS, "delivery.guard_identity")
    if guard["contract_version"] != "claude-upgrade-delivery-guard-identity/v1" or guard["host_id"] != "ARM64":
        raise ControlError("guard 身份合同或宿主非法")
    for key in ("authority", "service_instance_id", "source_scope"):
        if guard[key] != origin[key]:
            raise ControlError("guard 身份与独立来源不一致")
    for key in ("instance_id", "service_name"):
        expect_safe_id(guard[key], f"delivery.guard.{key}")
    for key in ("boot_id", "process_start"):
        expect_string(guard[key], f"delivery.guard.{key}")
    for key in ("pid", "process_group_id"):
        if type(guard[key]) is not int or guard[key] < 1:
            raise ControlError("guard 进程实例必须是正整数")
    if type(guard["independent_service"]) is not bool or (origin["source_scope"] == "formal" and not guard["independent_service"]):
        raise ControlError("正式 guard 必须证明独立服务生命周期")
    for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"):
        _binding(guard[key], f"delivery.guard.{key}")
    return guard


def _restoration(value: Any) -> dict[str, Any]:
    restoration = _exact(value, {"state", "bindings", "reason"}, "delivery.restoration")
    if restoration["state"] not in {"not_required", "verified", "restore_failed", "recovery_pending"}:
        raise ControlError("交付恢复状态非法")
    _bindings(restoration["bindings"], "delivery.restoration.bindings", non_empty=restoration["state"] != "not_required")
    expect_string(restoration["reason"], "delivery.restoration.reason")
    return restoration


def _requests(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ControlError("delivery.request_records 必须是数组")
    for row in value:
        _exact(row, {"method", "path", "request_binding", "response_binding"}, "delivery.request")
        expect_string(row["method"], "delivery.request.method")
        expect_string(row["path"], "delivery.request.path")
        _binding(row["request_binding"], "delivery.request.request_binding")
        if row["response_binding"] is not None:
            _binding(row["response_binding"], "delivery.request.response_binding")
        if row["method"] == "POST" and (row["path"] == "/v1/messages" or row["path"].startswith("/v1/messages/")):
            raise ControlError("交付探针发出了模型 POST，不能按零模型请求收口")
    return value


def allowed_probe_requests(recipe: Any) -> set[tuple[str, str]]:
    rows = expect_object(recipe, "delivery.probe_recipe").get("allowed_requests")
    if not isinstance(rows, list) or not rows:
        raise ControlError("冻结探针 recipe 没有明确请求闭集")
    pairs = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) not in ({"method", "path"}, {"method", "path", "authentication"}):
            raise ControlError("零模型探针 recipe 请求字段不闭合")
        if row["method"] != "GET" or row["path"] not in {"/health", "/v1/models"}:
            raise ControlError("交付 recipe 超出固定零模型请求范围")
        if "authentication" in row and row["authentication"] not in {"none", "candidate_private_ref"}:
            raise ControlError("交付 recipe 的鉴权引用非法")
        pairs.add((row["method"], row["path"]))
    return pairs


def validate_operational_evidence(value: Any) -> dict[str, Any]:
    """仅合同形状校验；来源与跨引用语义由 validate_evidence_graph 完成。"""

    payload = _exact(value, {"schema_version", "persona", "manifest_id", "evidence_type", "entries"}, "delivery.operational_v2")
    if payload["schema_version"] != OPERATIONAL_V2 or payload["evidence_type"] not in EVIDENCE_TYPES:
        raise ControlError("交付 operational evidence 的版本或类型不合法")
    from .contracts import validate_persona, expect_safe_id
    validate_persona(payload["persona"])
    expect_safe_id(payload["manifest_id"], "delivery.manifest_id")
    kind = payload["evidence_type"]
    entries = payload["entries"]
    if not isinstance(entries, list) or len(entries) != 1:
        raise ControlError("严格交付证据必须恰有一个对应类型条目")
    entry = _exact(entries[0], {"id", "facts"}, "delivery.entry")
    if entry["id"] != kind:
        raise ControlError("交付证据条目与类型不一致")
    facts = _exact(entry["facts"], COMMON_FIELDS | TYPE_FIELDS[kind], f"delivery.{kind}")
    if facts["contract_version"] != f"claude-delivery-{kind.replace('_', '-')}/v1":
        raise ControlError("交付证据子合同版本不匹配")
    if facts["result"] not in {"pass", "failed", "pending"}:
        raise ControlError("交付证据 result 非法")
    for key in ("campaign_id", "delivery_id"):
        expect_safe_id(facts[key], f"delivery.{key}")
    _origin(facts["origin"])
    typed_bindings(facts)
    if kind == "configuration":
        expect_object(facts["configuration"], "delivery.configuration")
        _binding(facts["binding"], "delivery.configuration.binding")
        if facts["configuration_sha256"] != canonical_sha256(facts["configuration"]):
            raise ControlError("交付 configuration 摘要不匹配")
    elif kind == "stable_projection":
        projection = _exact(facts["projection"], {"schema_version", "application_service", "compose", "containers"}, "delivery.projection")
        if projection["schema_version"] != "claude-upgrade-stable-environment-projection/v1":
            raise ControlError("交付稳定投影版本非法")
        expect_safe_id(projection["application_service"], "delivery.projection.application_service")
        for key in ("compose", "containers"):
            expect_object(projection[key], f"delivery.projection.{key}")
        _binding(facts["binding"], "delivery.projection.binding")
        if facts["projection_sha256"] != canonical_sha256(facts["projection"]):
            raise ControlError("交付稳定投影摘要不匹配")
    elif kind == "freeze":
        environment = _environment(facts["environment"])
        if environment["host_id"] != "ARM64" or environment["architecture"] not in {"arm64", "aarch64", "linux/arm64"}:
            raise ControlError("冻结交付环境仅允许 ARM64")
        _bindings(facts["before_bindings"], "delivery.freeze.before_bindings", non_empty=True)
        gates = _checks(facts["gates"], "delivery.freeze.gates")
        if {row["id"] for row in gates} != FREEZE_GATES:
            raise ControlError("交付冻结九门禁未闭合")
        if facts["result"] == "pass" and any(row["result"] != "pass" for row in gates):
            raise ControlError("交付冻结自报通过但门禁失败")
        _guard_identity(facts["guard_identity"], facts["origin"])
    elif kind == "rollback_state":
        from .contracts import validate_fact_ref, validate_receipt_ref
        for key in ("candidate_ref", "acceptance_ref"):
            validate_fact_ref(facts[key], f"delivery.rollback.{key}")
        receipt = validate_receipt_ref(facts["build_receipt_ref"], "delivery.rollback.build_receipt_ref")
        if receipt["receipt_kind"] != "candidate_build":
            raise ControlError("可信回退必须绑定构建收据")
        _object_ref(facts["evidence_package_ref"], "candidate_evidence_package", "delivery.rollback.evidence_package_ref")
        expect_image_digest(facts["image_digest"], "delivery.rollback.image_digest")
        expect_sha256(facts["runtime_profile_sha256"], "delivery.rollback.runtime_profile_sha256")
        _bindings(facts["bindings"], "delivery.rollback.bindings", non_empty=True)
    elif kind == "deployment_configuration":
        _binding(facts["configuration_binding"], "delivery.deployment.configuration_binding")
        expect_sha256(facts["deployment_sha256"], "delivery.deployment.deployment_sha256")
        _bindings(facts["bindings"], "delivery.deployment.bindings", non_empty=True)
    elif kind == "zero_model_probes":
        if facts["mode"] not in {"recipe", "observation"}:
            raise ControlError("零模型探针必须区分 recipe 与 observation")
        _requests(facts["request_records"])
        if facts["mode"] == "recipe" and facts["request_records"]:
            raise ControlError("冻结探针 recipe 不能包含已发请求")
        expect_object(facts["recipe"], "delivery.probe_recipe")
        allowed_probe_requests(facts["recipe"])
        _bindings(facts["bindings"], "delivery.probes.bindings", non_empty=True)
    elif kind in {"stage_execution", "restore"}:
        expect_safe_id(facts["operation_id"], "delivery.operation_id")
        _target(facts["target"])
        _actual(facts["actual"], "delivery.actual")
        _checks(facts["checks"], "delivery.checks")
        _binding(facts["journal_binding"], "delivery.journal_binding")
        if kind == "restore":
            _binding(facts["terminal_binding"], "delivery.terminal_binding")
            _bindings(facts["bindings"], "delivery.restore.bindings", non_empty=True)
        if kind == "stage_execution":
            for key in ("candidate_id", "run_nonce"):
                expect_safe_id(facts[key], f"delivery.{key}")
            if facts["stage"] not in {"candidate_active", "rollback_verified", "candidate_restored", "stable_observed"}:
                raise ControlError("交付执行阶段非法")
            if facts["environment"] is not None:
                _environment(facts["environment"])
            elif facts["result"] != "failed":
                raise ControlError("只有失败执行允许实测环境未知，不能用冻结计划冒充实测")
            for key in ("before_bindings", "during_bindings", "after_bindings"):
                _bindings(facts[key], f"delivery.{key}")
            for key in ("prepared_binding", "guard_terminal_binding"):
                _binding(facts[key], f"delivery.{key}")
            if facts["physical_intent_binding"] is not None:
                _binding(facts["physical_intent_binding"], "delivery.physical_intent_binding")
            _restoration(facts["restoration"])
            _requests(facts["request_records"])
            if type(facts["live_request_count"]) is not int or facts["live_request_count"] != 0:
                raise ControlError("交付 live_request_count 必须是整数零")
            expect_non_negative_int(facts["observed_seconds"], "delivery.observed_seconds")
            for key, expected in (("plan_ref", "candidate_delivery_plan"), ("configuration_ref", "operational_evidence"), ("stable_projection_ref", "operational_evidence"), ("probe_recipe_ref", "operational_evidence")):
                _object_ref(facts[key], expected, f"delivery.{key}")
            _guard_identity(facts["guard_identity"], facts["origin"])
    return payload


def evidence_facts(store: Any, reference: Mapping[str, Any], kind: str) -> dict[str, Any]:
    _object_ref(dict(reference), "operational_evidence", "delivery.evidence_ref")
    payload = store.load_object(dict(reference))["payload"]
    validate_operational_evidence(payload)
    if payload["evidence_type"] != kind:
        raise ControlError(f"交付证据类型必须是 {kind}")
    return payload["entries"][0]["facts"]


def _validate_qualification_originals(store: Any, payload: Mapping[str, Any], source: VerifiedSource,
                                      *, current_execution: bool,
                                      frozen_configuration_ref: Mapping[str, Any] | None = None) -> None:
    """来源已经验真，再按本次执行或形成期只读上下文消费资格原件。"""
    if payload["evidence_type"] not in {"freeze", "stage_execution"}:
        return
    from .qualification_bindings import DELIVERY_SCHEMA, MATERIAL_DELIVERY_SCHEMA, validate_delivery_report

    facts = payload["entries"][0]["facts"]
    if current_execution:
        from .receipts import _require_effective_tool
        campaign = store.load_campaign(facts["campaign_id"])
        if campaign.get("client") != "claude" or campaign.get("schema_version") != "official-client-control-campaign/v2":
            raise ControlError("当前资格消费必须属于 Claude Campaign v2")
        # 当前写入／派发权限由有效身份与受管树一致性决定，不能由 source_scope 字符串授予。
        _require_effective_tool(store, campaign)
    bindings = [row for row in typed_bindings(facts) if Path(row["path"]).name == "qualification.json"]
    if current_execution and facts["result"] == "pass" and len(bindings) != 1:
        raise ControlError("当前通过的交付缺少唯一资格报告原件")
    for binding in bindings:
        report = _source_json(source, binding)
        configuration = None
        if report.get("schema_version") in {DELIVERY_SCHEMA, MATERIAL_DELIVERY_SCHEMA}:
            configuration_binding = _binding(report.get("configuration_binding"), "qualification.configuration_binding")
            configuration = _source_json(source, configuration_binding)
            if canonical_sha256(configuration) != source.configuration_sha256:
                raise ControlError("资格报告配置原件与已验证来源的冻结配置摘要不一致")
            reference = frozen_configuration_ref
            if payload["evidence_type"] == "stage_execution":
                reference = facts["configuration_ref"]
                if frozen_configuration_ref is not None and reference != frozen_configuration_ref:
                    raise ControlError("资格报告阶段配置引用不是本次冻结配置")
            if reference is not None:
                # 只沿明确引用核一次 configuration 原件，不扫描文件名、摘要或递归重放交付图。
                original_payload = store.load_object(dict(reference))["payload"]
                original_facts = evidence_facts(store, reference, "configuration")
                if report.get("schema_version") == MATERIAL_DELIVERY_SCHEMA:
                    # 同次报告随后完整重建同一配置的第二根资格；这里仍独立
                    # 验配置原件及来源，只合并重复的资格图，不复用通过结论。
                    original_source = _verified_source(store, original_facts, original_facts["origin"])
                    _validate_configuration_original(store, original_facts, original_source,
                                                     current_execution=current_execution)
                else:
                    validate_evidence_graph(store, original_payload, current_execution=current_execution)
                if (original_facts["campaign_id"], original_facts["delivery_id"]) != (facts["campaign_id"], facts["delivery_id"]):
                    raise ControlError("资格报告配置引用跨 Campaign 或交付运行")
                if (original_facts["binding"] != configuration_binding
                        or original_facts["configuration"] != configuration
                        or original_facts["configuration_sha256"] != source.configuration_sha256):
                    raise ControlError("资格报告配置绑定与冻结配置引用的真实原件不一致")
        validate_delivery_report(report, store, current_execution=current_execution, configuration=configuration,
                                 trusted_data_root=source.trusted_data_root, source=source)
        if report["candidate"]["qualification"]["campaign_id"] != facts["campaign_id"]:
            raise ControlError("资格报告与本次交付 Campaign 不一致")


def validate_evidence_graph(store: Any, payload: Mapping[str, Any], *, current_execution: bool = False,
                            frozen_configuration_ref: Mapping[str, Any] | None = None) -> VerifiedSource:
    """一次图校验独占临时 reader 结果，成功或失败退出后均失效。"""
    from tools.official_client_capture.claude_gate_material_worker import gate_material_reader_scope
    with gate_material_reader_scope(), _source_batch_scope(store,
            _graph_source_items(store, payload, frozen_configuration_ref)):
        return _validate_evidence_graph(store, payload, current_execution=current_execution,
                                        frozen_configuration_ref=frozen_configuration_ref)


def _validate_configuration_original(store: Any, facts: Mapping[str, Any], source: VerifiedSource,
                                     *, current_execution: bool) -> dict[str, Any]:
    """配置的原件、形状与版本准入；资格图由对应消费入口完整验证。"""
    raw = _source_json(source, facts["binding"])
    if raw != facts["configuration"]:
        raise ControlError("交付配置与受管通道原件不一致")
    from tools.official_client_capture.claude_upgrade_arm64_delivery_executor import validate_configuration
    from .qualification_material_import import requires_qualification_materials
    validate_configuration(raw)
    campaign_path = Path(store.root) / "campaigns" / (facts["campaign_id"] + ".json")
    # 独立配置对象可先于 Campaign 封存，本身不授予执行权限；Plan
    # 必须有真实 Campaign，并在其准入处再次要求后继合同。
    if current_execution and (campaign_path.exists() or campaign_path.is_symlink()) and requires_qualification_materials(store, store.load_campaign(facts["campaign_id"])) and raw.get("schema_version") != "claude-upgrade-arm64-executor-configuration/v2":
        raise ControlError("当前第二根能力拒绝配置 v1 降级")
    return raw


def _validate_evidence_graph(store: Any, payload: Mapping[str, Any], *, current_execution: bool,
                             frozen_configuration_ref: Mapping[str, Any] | None) -> VerifiedSource:
    validate_operational_evidence(dict(payload))
    facts = payload["entries"][0]["facts"]
    source = _verified_source(store, facts, facts["origin"])
    if payload["evidence_type"] == "configuration":
        from .qualification_material_import import projection_for_configuration
        raw = _validate_configuration_original(store, facts, source, current_execution=current_execution)
        projection_for_configuration(store, raw, source, purpose="current_delivery" if current_execution else "formed_replay")
    elif payload["evidence_type"] == "stable_projection":
        if _source_json(source, facts["binding"]) != facts["projection"]:
            raise ControlError("稳定投影与来源原件不一致")
    _validate_qualification_originals(store, payload, source, current_execution=current_execution,
                                      frozen_configuration_ref=frozen_configuration_ref)
    return source


def requires_current_contract(store: Any, campaign: Mapping[str, Any]) -> bool:
    """版本下限由有效身份能力决定，不相信 payload／调用者声明。"""

    if campaign.get("client") != "claude" or campaign.get("schema_version") != "official-client-control-campaign/v2":
        return False
    from .receipts import effective_tool_identity
    identity = effective_tool_identity(store, dict(campaign))
    return identity is not None and identity["policy_version"] >= 4


def require_write_version(store: Any, campaign: Mapping[str, Any], payload: Mapping[str, Any], expected: str) -> None:
    if requires_current_contract(store, campaign) and payload.get("schema_version") != expected:
        raise ControlError("当前 Claude 真实交付能力拒绝降级合同；旧交付仅可按形成期只读重放")


def _replay_qualification_subgraph(store: Any, qualification: Mapping[str, Any], source: VerifiedSource | None = None,
                                  *, trusted_data_root: Path | None = None) -> dict[str, Any]:
    """只重放验收可达子图，不遍历本轮交付图或调用 Store 全重放触发来源 RPC。"""
    from collections import defaultdict
    from .contracts import iter_fact_refs, iter_object_refs, iter_receipt_refs
    from .gates import WorkflowGates
    from .receipts import FormedContext, replay_receipt
    from .qualification_bindings import QualificationBindingProjection, REPLAY_SCHEMA, validate_qualification_roots

    # source 的原调用语义保留。新增根只供已验证消费路径做只读闭包，不能取报告自报绝对路径。
    if source is not None and trusted_data_root is not None:
        if source.trusted_data_root is None or Path(source.trusted_data_root).resolve() != Path(trusted_data_root).resolve():
            raise ControlError("资格重放的显式数据根与受管来源不一致")
    data_root = source.trusted_data_root if source is not None else trusted_data_root
    if source is None and data_root is None:
        raise ControlError("只读资格闭包缺已验证的材料根")
    if source is None and (not Path(data_root).is_absolute() or Path(data_root).is_symlink() or not Path(data_root).is_dir()):
        raise ControlError("只读资格闭包只接受已有、非符号链接的可信绝对材料根")
    roots = validate_qualification_roots(store, qualification)
    campaign = store.load_campaign(qualification["campaign_id"])
    gates = WorkflowGates(store)
    history = store.list_facts(campaign["campaign_id"])
    context = FormedContext(store, campaign)
    pending = [(store.load_fact(qualification[key]), None) for key in ("candidate_ref", "validation_ref", "acceptance_ref")]
    pending.append((store.load_receipt(qualification["build_receipt_ref"]), qualification["build_receipt_ref"]))
    external_root = None
    if data_root is not None:
        from .canonical import resolve_relative
        relative = qualification["external_root_relative"]
        # 此字段定位已由受管来源确认的资格材料根；原根用精确的 "." 表示。
        # 文件绑定仍经原规范相对路径校验，不把根坐标例外扩展给任何原件路径。
        external_root = (Path(data_root).resolve()
                         if relative == "." else resolve_relative(data_root, relative))
        if not external_root.is_dir():
            raise ControlError("可信回退资格材料根不是现存目录")
    seen: set[tuple[str, str]] = set()
    reachable_facts: dict[str, dict[str, Any]] = {}
    projection = QualificationBindingProjection(store)
    objects, receipts, external = [], [], {}
    while pending:
        document, parent_ref = pending.pop()
        for binding in projection.bindings(document, parent_ref):
            if external_root is None:
                _source_bytes(source, binding)
            else:
                store._verify_external_binding(external_root, binding)
                projection.verify_external_mode(external_root, binding)
            if external_root is None and projection.manifest()["artifact_file_requirements"]:
                raise ControlError("严格 Claude 构建资格缺受管材料根，无法核验制品模式")
            previous = external.setdefault(binding["path"], dict(binding))
            if previous != binding:
                raise ControlError("可信回退外部读集对同一路径声明不同字节")
        for reference in iter_object_refs(document):
            key = ("object", canonical_sha256(reference))
            if key not in seen:
                seen.add(key)
                gates.validate_object_graph(reference)
                pending.append((store.load_object(reference), reference))
                objects.append(reference)
        for reference in iter_receipt_refs(document):
            key = ("receipt", canonical_sha256(reference))
            if key not in seen:
                seen.add(key)
                replay_receipt(store, reference)
                pending.append((store.load_receipt(reference), reference))
                receipts.append(reference)
        for reference in iter_fact_refs(document):
            key = ("fact", canonical_sha256(reference))
            if key not in seen:
                seen.add(key)
                fact = store.load_fact(reference)
                if fact["campaign_id"] != campaign["campaign_id"] or fact["fact_kind"] == "candidate_delivery_recorded":
                    raise ControlError("可信回退验收子图跨 Campaign 或反向引用交付执行")
                reachable_facts[canonical_sha256(reference)] = fact
                pending.append((fact, None))
        if "fact_kind" in document:
            reachable_facts[canonical_sha256(store.fact_ref(document))] = document
    for fact in reachable_facts.values():
        without_current: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in history:
            if store.fact_ref(item) != store.fact_ref(fact):
                without_current[item["fact_kind"]].append(item)
        coordinates = context.coordinates(fact)
        context.require_causality(store, coordinates, iter_fact_refs(fact["payload"]), "可信回退验收子图")
        gates._validate_fact_semantics(campaign, fact, without_current, context)
    return {"schema_version": REPLAY_SCHEMA, "campaign_id": qualification["campaign_id"],
            "candidate_id": qualification["candidate_id"], "qualification_roots": roots,
            "external_bindings_base": "external_root",
            "binding_projection": projection.manifest(),
            "fact_refs": sorted((store.fact_ref(fact) for fact in reachable_facts.values()), key=canonical_sha256),
            "object_refs": sorted(objects, key=canonical_sha256), "receipt_refs": sorted(receipts, key=canonical_sha256),
            "external_bindings": [external[key] for key in sorted(external)], "result": "pass"}


def _validate_rollback_qualification(store: Any, plan: Mapping[str, Any], configuration: Mapping[str, Any], rollback: Mapping[str, Any], source: VerifiedSource) -> None:
    """基准 Candidate、构建、验收、VC-5 与封存原件必须属于同一完整资格图。"""
    from .contracts import validate_fact_ref, validate_receipt_ref
    qualification = _exact(configuration["rollback"]["qualification"], {"store_relative", "external_root_relative", "campaign_id", "candidate_id", "candidate_ref", "build_receipt_ref", "validation_ref", "acceptance_ref", "sealed_result_binding"}, "delivery.rollback.qualification")
    for key in ("candidate_ref", "validation_ref", "acceptance_ref"):
        reference = validate_fact_ref(qualification[key], f"delivery.rollback.{key}")
        if reference["campaign_id"] != qualification["campaign_id"]:
            raise ControlError("可信回退资格事实跨 Campaign")
    validate_receipt_ref(qualification["build_receipt_ref"], "delivery.rollback.build_receipt_ref")
    for key in ("candidate_ref", "build_receipt_ref", "acceptance_ref"):
        if rollback[key] != qualification[key]:
            raise ControlError("回退执行证据与冻结资格引用不一致")
    campaign = store.load_campaign(qualification["campaign_id"])
    if campaign.get("client") != "claude" or campaign.get("schema_version") != "official-client-control-campaign/v2":
        raise ControlError("可信回退必须来自 Claude Campaign v2")
    candidate = store.load_fact(qualification["candidate_ref"])
    acceptance = store.load_fact(qualification["acceptance_ref"])
    validation = store.load_fact(qualification["validation_ref"])
    build = store.load_receipt(qualification["build_receipt_ref"])
    for fact, kind in ((candidate, "candidate_frozen"), (acceptance, "acceptance_recorded"), (validation, "validation_completed")):
        if fact["fact_kind"] != kind or fact["campaign_id"] != qualification["campaign_id"] or fact["payload"].get("candidate_id") != qualification["candidate_id"]:
            raise ControlError("可信回退资格事实类型、归属或 Candidate 不一致")
    candidate_payload = candidate["payload"]
    arm64_aliases = {"arm64", "aarch64", "linux/arm64"}
    same_architecture = candidate_payload["target_architecture"] == plan["architecture"] or {candidate_payload["target_architecture"], plan["architecture"]} <= arm64_aliases
    if candidate_payload["candidate_id"] == plan["candidate_id"] or candidate_payload["image_digest"] != rollback["image_digest"] or not same_architecture:
        raise ControlError("可信回退必须是同架构的独立真实 Candidate")
    expected = {"candidate_ref": qualification["candidate_ref"], "candidate_build_receipt_ref": qualification["build_receipt_ref"], "acceptance_ref": qualification["acceptance_ref"], "candidate_evidence_package_ref": rollback["evidence_package_ref"]}
    if candidate_payload["candidate_build_receipt_ref"] != qualification["build_receipt_ref"] or any(validation["payload"].get(key) != value for key, value in expected.items()) or validation["payload"].get("result") != "passed":
        raise ControlError("可信回退缺同一 Candidate 的完整 VC-5 验收链")
    for key in ("candidate_ref", "candidate_build_receipt_ref", "candidate_evidence_package_ref"):
        if acceptance["payload"].get(key) != expected[key]:
            raise ControlError("可信回退 Acceptance 与构建或证据包不一致")
    from .gates import WorkflowGates
    WorkflowGates(store)._candidate_build_matches(campaign, candidate_payload, build)
    if build["profile_identity"]["profile_digest"] != rollback["runtime_profile_sha256"]:
        raise ControlError("可信回退运行 Profile 与真实构建不一致")
    sealed = _source_json(source, qualification["sealed_result_binding"])
    sealed_store = expect_object(sealed.get("store"), "delivery.rollback.sealed.store")
    if sealed.get("schema_version") != "claude-upgrade-acceptance/v1" or sealed.get("candidate_id") != qualification["candidate_id"] or sealed.get("production_state") != "not_activated" or sealed_store.get("acceptance_ref") != qualification["acceptance_ref"] or sealed_store.get("validation_completed_ref") != qualification["validation_ref"]:
        raise ControlError("可信回退缺同一验收链的不可覆盖封存结果")
    _replay_qualification_subgraph(store, qualification, source)


def validate_plan(store: Any, plan: Mapping[str, Any], *, current_execution: bool = False) -> None:
    with _source_batch_scope(store, _plan_source_items(store, plan)):
        _validate_plan(store, plan, current_execution=current_execution)


def _validate_plan(store: Any, plan: Mapping[str, Any], *, current_execution: bool = False) -> None:
    campaign = store.load_campaign(plan["campaign_id"])
    if campaign.get("client") != "claude" or campaign.get("schema_version") != "official-client-control-campaign/v2":
        raise ControlError("真实交付仅属于明确 Claude 的 Campaign v2")
    if plan["schema_version"] != PLAN_V2 or plan["host_id"] != "ARM64" or plan["architecture"] not in {"arm64", "aarch64", "linux/arm64"}:
        raise ControlError("真实候选交付只允许 ARM64 隔离用途")
    from .qualification_material_import import requires_qualification_materials
    if current_execution and requires_qualification_materials(store, campaign):
        frozen = evidence_facts(store, plan["executor_configuration_ref"], "configuration")["configuration"]
        if frozen.get("schema_version") != "claude-upgrade-arm64-executor-configuration/v2":
            raise ControlError("当前交付准入要求配置 v2；旧配置对象不授予后继执行权限")
    expected = {
        "executor_configuration_ref": "configuration",
        "stable_environment_projection_ref": "stable_projection",
        "probe_recipe_ref": "zero_model_probes",
        "rollback_state_ref": "rollback_state",
        "environment_before_ref": "freeze",
        "environment_freeze_ref": "freeze",
        "deployment_configuration_ref": "deployment_configuration",
    }
    verified_references: set[tuple[str, str]] = set()
    for key, kind in expected.items():
        payload = store.load_object(plan[key])["payload"]
        facts = evidence_facts(store, plan[key], kind)
        if facts["campaign_id"] != plan["campaign_id"] or facts["delivery_id"] != plan["delivery_id"] or facts["result"] != "pass":
            raise ControlError("交付计划引用了其他运行或未通过的严格证据")
        # 字段类型与归属逐项核对；本次只读计划内，相同内容寻址对象只验图一次。
        # 配置留在最后验真，来源结果立即用于回退资格，不跨写入或物理阶段承接。
        reference_key = (plan[key]["object_kind"], plan[key]["sha256"])
        if kind == "configuration" or reference_key in verified_references:
            continue
        validate_evidence_graph(store, payload, current_execution=current_execution,
                                frozen_configuration_ref=plan["executor_configuration_ref"] if kind == "freeze" else None)
        verified_references.add(reference_key)
    configuration = evidence_facts(store, plan["executor_configuration_ref"], "configuration")
    projection = evidence_facts(store, plan["stable_environment_projection_ref"], "stable_projection")
    if configuration["configuration_sha256"] != plan["configuration_sha256"] or projection["projection_sha256"] != plan["environment_sha256"]:
        raise ControlError("交付计划配置或稳定投影摘要不一致")
    rollback = evidence_facts(store, plan["rollback_state_ref"], "rollback_state")
    if rollback["image_digest"] != plan["rollback_image_digest"] or rollback["runtime_profile_sha256"] != plan["rollback_runtime_profile_sha256"]:
        raise ControlError("交付回退状态与计划不一致")
    source = validate_evidence_graph(store, store.load_object(plan["executor_configuration_ref"])["payload"], current_execution=current_execution)
    _validate_rollback_qualification(store, plan, configuration["configuration"], rollback, source)


def _verify_actual(source: VerifiedSource, actual: Mapping[str, Any], target: Mapping[str, Any], *, passed: bool) -> None:
    for key, target_key in (("image", "image_digest"), ("profile", "runtime_profile_sha256")):
        item = actual[key]
        if item["state"] != "known":
            if passed:
                raise ControlError("通过的交付阶段必须有真实镜像和 Profile 实测原件")
            continue
        raw = _source_json(source, item["direct_binding"])
        if raw.get(target_key) != item["value"]:
            raise ControlError("交付实测身份与原件不一致")
        if passed and item["value"] != target[target_key]:
            raise ControlError("交付实测值未匹配阶段目标")


def validate_restore_evidence(store: Any, plan: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """恢复收口同时验证真实回退、原件、五检查以及不可覆盖 journal／guard 终态。"""
    validate_operational_evidence(dict(payload))
    if payload["evidence_type"] != "restore":
        raise ControlError("恢复消费必须使用严格 restore 证据")
    facts = payload["entries"][0]["facts"]
    source = validate_evidence_graph(store, payload)
    if facts["campaign_id"] != plan["campaign_id"] or facts["delivery_id"] != plan["delivery_id"]:
        raise ControlError("恢复证明跨交付引用")
    target = {"image_digest": plan["rollback_image_digest"], "runtime_profile_sha256": plan["rollback_runtime_profile_sha256"]}
    if facts["target"] != target or facts["origin"]["configuration_sha256"] != plan["configuration_sha256"]:
        raise ControlError("恢复目标或配置不是冻结的可信回退")
    passed = facts["result"] == "pass"
    _verify_actual(source, facts["actual"], target, passed=passed)
    checks = _checks(facts["checks"], "delivery.restore.checks")
    if {row["id"] for row in checks} != RESTORE_CHECKS or (passed and any(row["result"] != "pass" for row in checks)):
        raise ControlError("恢复五检查未闭合或存在失败")
    terminal = _source_json(source, facts["terminal_binding"])
    _exact(terminal, {"schema_version", "operation_id", "status", "prepared_sha256", "physical_intent_sha256", "physical_intent_binding", "restoration", "guard_identity"}, "delivery.restore.terminal")
    if terminal["schema_version"] != "claude-upgrade-delivery-guard-terminal/v1" or terminal["operation_id"] != facts["operation_id"]:
        raise ControlError("恢复 guard 终态操作或版本不一致")
    _guard_identity(terminal["guard_identity"], facts["origin"])
    if source.deployment_sha256 != terminal["guard_identity"]["deployment_receipt_binding"]["sha256"]:
        raise ControlError("恢复 guard 部署证明与受管来源不一致")
    restoration = _restoration(terminal["restoration"])
    expected_state = {"restored": "verified", "restore_failed": "restore_failed", "recovery_pending": "recovery_pending"}.get(terminal["status"])
    if facts["bindings"] != restoration["bindings"] or terminal["status"] not in ({"restored"} if passed else {"restore_failed", "recovery_pending"}) or restoration["state"] != expected_state:
        raise ControlError("恢复证据与 guard 终态或原件集合不一致")
    expect_sha256(terminal["prepared_sha256"], "delivery.restore.prepared_sha256")
    if terminal["physical_intent_binding"] is None:
        if terminal["physical_intent_sha256"] is not None:
            raise ControlError("恢复终态物理意图摘要缺少原件")
    elif _binding(terminal["physical_intent_binding"], "delivery.restore.physical_intent_binding")["sha256"] != terminal["physical_intent_sha256"]:
        raise ControlError("恢复终态物理意图绑定不一致")
    if passed:
        if restoration["state"] != "verified":
            raise ControlError("恢复通过必须有真实验证终态")
        observed = _source_json(source, restoration["bindings"][0])
        _exact(observed, {"target", "actual", "checks", "bindings", "result"}, "delivery.restore.observation")
        if observed["result"] != "pass" or any(observed[key] != facts[key] for key in ("target", "actual", "checks")):
            raise ControlError("恢复通过与独立实测原件不一致")
        _bindings(observed["bindings"], "delivery.restore.observed.bindings")
    journal = _source_json(source, facts["journal_binding"])
    _exact(journal, {"schema_version", "operation_id", "records"}, "delivery.restore.journal")
    if journal["schema_version"] != "claude-upgrade-delivery-journal/v1" or journal["operation_id"] != facts["operation_id"] or not isinstance(journal["records"], list) or not journal["records"]:
        raise ControlError("恢复 journal 操作、版本或记录不完整")
    previous = None
    for number, record in enumerate(journal["records"], 1):
        _exact(record, {"schema_version", "operation_id", "sequence", "event", "payload", "previous_sha256", "record_sha256"}, "delivery.restore.journal.record")
        if record["schema_version"] != "claude-upgrade-delivery-journal/v1" or record["operation_id"] != facts["operation_id"] or type(record["sequence"]) is not int or record["sequence"] != number or record["previous_sha256"] != previous or record["record_sha256"] != canonical_sha256({key: value for key, value in record.items() if key != "record_sha256"}):
            raise ControlError("恢复 journal 链断裂或原件摘要漂移")
        expect_string(record["event"], "delivery.restore.journal.event")
        expect_object(record["payload"], "delivery.restore.journal.payload")
        previous = record["record_sha256"]
    last = journal["records"][-1]
    if last["event"] != terminal["status"] or last["payload"] != {"terminal_binding": facts["terminal_binding"]}:
        raise ControlError("恢复 journal 未闭合相同 guard 终态")
    return facts


def validate_stage_execution(store: Any, plan: Mapping[str, Any], stage_payload: Mapping[str, Any], *, current_execution: bool = False) -> dict[str, Any]:
    if plan["schema_version"] != PLAN_V2 or stage_payload.get("schema_version") != FACT_V3:
        raise ControlError("当前交付阶段必须使用 Plan v2 与 StageFact v3")
    facts = evidence_facts(store, stage_payload["execution_evidence_ref"], "stage_execution")
    source = validate_evidence_graph(store, store.load_object(stage_payload["execution_evidence_ref"])["payload"], current_execution=current_execution)
    return _validate_stage_execution_semantics(store, plan, stage_payload, facts, source)


def _validate_stage_execution_semantics(store: Any, plan: Mapping[str, Any], stage_payload: Mapping[str, Any],
                                       facts: Mapping[str, Any], source: VerifiedSource) -> dict[str, Any]:
    """仅在同次完整验图后消费阶段语义；不缓存来源或接受外部证明的公共绕行入口。"""
    if plan["schema_version"] != PLAN_V2 or stage_payload.get("schema_version") != FACT_V3:
        raise ControlError("当前交付阶段必须使用 Plan v2 与 StageFact v3")
    stage = stage_payload["stage"]
    for key, expected in (("campaign_id", plan["campaign_id"]), ("delivery_id", plan["delivery_id"]), ("candidate_id", plan["candidate_id"]), ("stage", stage), ("plan_ref", stage_payload["delivery_plan_ref"]), ("configuration_ref", plan["executor_configuration_ref"]), ("stable_projection_ref", plan["stable_environment_projection_ref"]), ("probe_recipe_ref", plan["probe_recipe_ref"]), ("result", stage_payload["result"])):
        if facts[key] != expected:
            raise ControlError(f"交付执行证明的 {key} 与冻结链不一致")
    target = {"image_digest": plan["rollback_image_digest"] if stage == "rollback_verified" else plan["candidate_image_digest"], "runtime_profile_sha256": plan["rollback_runtime_profile_sha256"] if stage == "rollback_verified" else plan["runtime_profile_sha256"]}
    if facts["target"] != target or stage_payload["target_image_digest"] != target["image_digest"] or stage_payload["target_runtime_profile_sha256"] != target["runtime_profile_sha256"]:
        raise ControlError("交付目标身份与计划不一致")
    passed = facts["result"] == "pass"
    _verify_actual(source, facts["actual"], target, passed=passed)
    if passed and facts["environment"] != {key: plan[key] for key in ENVIRONMENT_FIELDS}:
        raise ControlError("交付稳定环境漂移")
    checks = _checks(facts["checks"], "delivery.checks")
    if sorted(row["id"] for row in checks) != sorted(plan["stage_checks"][stage]) or stage_payload["check_ids"] != plan["stage_checks"][stage]:
        raise ControlError("交付执行检查读集未闭合")
    if passed and any(row["result"] != "pass" for row in checks):
        raise ControlError("交付执行证明自报通过但检查失败")
    # 物理观测可通过而提交前语义复核失败；该失败由 guard 恢复终态证明，不能篡改实测检查项。
    if facts["observed_seconds"] != stage_payload["observed_seconds"] or (passed and stage == "stable_observed" and facts["observed_seconds"] < plan["observation_window_seconds"]):
        raise ControlError("交付观察窗与真实观测不一致")
    recipe = evidence_facts(store, plan["probe_recipe_ref"], "zero_model_probes")["recipe"]
    allowed = allowed_probe_requests(recipe)
    for row in facts["request_records"]:
        if (row["method"], row["path"]) not in allowed:
            raise ControlError("交付探针请求超出冻结 recipe")
        raw = _source_json(source, row["request_binding"])
        if raw.get("method") != row["method"] or raw.get("path") != row["path"]:
            raise ControlError("交付请求记录与原始字节不一致")
    if passed and not any(row["method"] == "GET" and row["path"] == "/v1/models" for row in facts["request_records"]):
        raise ControlError("通过阶段缺少经过实际鉴权与数据路径的零模型探针")
    prepared = _source_json(source, facts["prepared_binding"])
    intent = _source_json(source, facts["physical_intent_binding"]) if facts["physical_intent_binding"] is not None else None
    terminal = _source_json(source, facts["guard_terminal_binding"])
    journal = _source_json(source, facts["journal_binding"])
    for document in (prepared, *([intent] if intent is not None else []), terminal, journal):
        if document.get("operation_id") != facts["operation_id"]:
            raise ControlError("prepared／物理意图／guard／journal 操作坐标不一致")
    if passed:
        if intent is None or terminal.get("physical_intent_sha256") != facts["physical_intent_binding"]["sha256"] or intent.get("prepared_sha256") != facts["prepared_binding"]["sha256"] or terminal.get("status") != "execution_committed":
            raise ControlError("物理提交意图与独立 guard 终态未闭合")
    elif terminal.get("status") not in {"restored", "restore_failed", "recovery_pending"}:
        raise ControlError("失败交付操作缺少明确恢复终态")
    if source.deployment_sha256 != facts["guard_identity"]["deployment_receipt_binding"]["sha256"]:
        raise ControlError("受管 guard 部署证明与执行身份不一致")
    if not passed and facts["restoration"] is None:
        raise ControlError("失败操作缺少恢复原件，不能继续交付")
    return facts


def receipt_execution_inputs(package: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    return {**{key: plan[key] for key in PLAN_EXTRA_FIELDS if key != "environment_before_ref"}, **{key: package[key] for key in PACKAGE_EXTRA_FIELDS}}


def validate_delivery_package(store: Any, package: Mapping[str, Any], *, current_execution: bool = False) -> None:
    if package.get("schema_version") != PACKAGE_V2:
        raise ControlError("当前真实交付必须使用 Package v2")
    plan = store.load_object(package["delivery_plan_ref"])["payload"]
    items = _plan_source_items(store, plan)
    items.append(_source_request({"campaign_id": package["campaign_id"], "delivery_id": package["delivery_id"],
        "operation_index_binding": package["operation_index_binding"]}, package["operation_index_origin"]))
    for reference in [*package["execution_evidence_refs"], *package["restoration_evidence_refs"]]:
        items.extend(_graph_source_items(store, store.load_object(reference)["payload"]))
    with _source_batch_scope(store, items):
        _validate_delivery_package(store, package, current_execution=current_execution)


def _validate_delivery_package(store: Any, package: Mapping[str, Any], *, current_execution: bool = False) -> None:
    if package.get("schema_version") != PACKAGE_V2:
        raise ControlError("当前真实交付必须使用 Package v2")
    plan = store.load_object(package["delivery_plan_ref"])["payload"]
    validate_plan(store, plan, current_execution=current_execution)
    execution_refs = package["execution_evidence_refs"]
    stages_by_reference: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for reference in package["delivery_fact_refs"]:
        fact = store.load_fact(reference)
        if fact["payload"].get("schema_version") != FACT_V3:
            raise ControlError("当前交付阶段必须使用 Plan v2 与 StageFact v3")
        execution_reference = fact["payload"]["execution_evidence_ref"]
        if execution_reference not in execution_refs:
            raise ControlError("交付包未归档有效阶段的执行证明")
        stages_by_reference.setdefault(canonical_sha256(execution_reference), []).append((reference, fact))
    index_content = {"campaign_id": package["campaign_id"], "delivery_id": package["delivery_id"], "operation_index_binding": package["operation_index_binding"]}
    source = _verified_source(store, index_content, package["operation_index_origin"])
    if package["operation_index_origin"]["operations_index_binding"] != package["operation_index_binding"]:
        raise ControlError("完整操作索引没有绑定来源的同一快照")
    index = _source_json(source, package["operation_index_binding"])
    _exact(index, {"schema_version", "configuration_sha256", "operations"}, "delivery.operation_index")
    if index.get("schema_version") != "claude-upgrade-delivery-operations-index/v1" or index.get("configuration_sha256") != plan["configuration_sha256"]:
        raise ControlError("guard 操作索引与交付归档不一致")
    operations = index.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ControlError("guard 完整操作索引为空")
    by_operation = {}
    for reference in execution_refs:
        payload = store.load_object(reference)["payload"]
        core = evidence_facts(store, reference, "stage_execution")
        if core["operation_id"] in by_operation or core["campaign_id"] != package["campaign_id"] or core["delivery_id"] != package["delivery_id"]:
            raise ControlError("完整交付执行集合有重复操作或跨运行材料")
        by_operation[core["operation_id"]] = (reference, core, payload)
    restoration_cores = []
    for reference in package["restoration_evidence_refs"]:
        payload = store.load_object(reference)["payload"]
        core = evidence_facts(store, reference, "restore")
        validate_restore_evidence(store, plan, payload)
        if core["campaign_id"] != package["campaign_id"] or core["delivery_id"] != package["delivery_id"]:
            raise ControlError("恢复材料跨交付引用")
        restoration_cores.append(core)
    required_material: list[Any] = [index_content, index]
    publication_bindings = validate_publication_bindings(package["publication_intent_bindings"])
    if len(publication_bindings) != len(by_operation):
        raise ControlError("完整交付包遗漏或重复不可变发布意图")
    operation_ids = set()
    publications_by_reference: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for operation in operations:
        _exact(operation, {"operation_id", "status", "prepared_binding", "terminal_binding", "bindings"}, "delivery.operation")
        if operation["operation_id"] in operation_ids or operation["status"] not in {"execution_committed", "restored"}:
            raise ControlError("存在重复或未解决的交付操作")
        operation_ids.add(operation["operation_id"])
        pair = by_operation.get(operation["operation_id"])
        if pair is None:
            raise ControlError("交付包遗漏未发布或失败的物理操作")
        reference, core, _formed_payload = pair
        prepared = _source_json(source, operation["prepared_binding"])
        if prepared.get("context", {}).get("campaign_id") != package["campaign_id"] or prepared.get("context", {}).get("delivery_id") != package["delivery_id"] or prepared.get("operation_id") != operation["operation_id"] or core["prepared_binding"] != operation["prepared_binding"]:
            raise ControlError("完整操作索引与冻结交付或原执行不一致")
        terminal = _source_json(source, operation["terminal_binding"])
        if terminal.get("operation_id") != operation["operation_id"] or terminal.get("status") != operation["status"]:
            raise ControlError("完整操作索引未核最新 guard 终态")
        if terminal.get("prepared_sha256") != operation["prepared_binding"]["sha256"]:
            raise ControlError("最新恢复终态未绑定原修改前 prepared 原件")
        if operation["status"] == "restored" and not any(item["operation_id"] == operation["operation_id"] and item["result"] == "pass" and item["terminal_binding"] == operation["terminal_binding"] for item in restoration_cores):
            raise ControlError("最新恢复终态缺少对应真实恢复证据")
        matches = [row for row in publication_bindings if row["path"].endswith(f"/publication/{operation['operation_id']}.json")]
        if len(matches) != 1:
            raise ControlError("每个物理操作必须恰有一份固定坐标发布意图")
        publication = publication_document(source, matches[0], package["campaign_id"], package["delivery_id"], operation["operation_id"])
        _exact(publication, {"schema_version", "campaign_id", "delivery_id", "operation_id", "seed_binding", "execution_evidence_ref", "payload", "issued_at_utc", "producer_run", "formed_under", "idempotency_key", "ledger_action"}, "delivery.publication_intent")
        if publication["schema_version"] != "claude-upgrade-delivery-publication-intent/v1" or publication["campaign_id"] != package["campaign_id"] or publication["delivery_id"] != package["delivery_id"] or publication["operation_id"] != operation["operation_id"] or publication["execution_evidence_ref"] != reference or publication["payload"].get("execution_evidence_ref") != reference or publication["ledger_action"] != "catch_up_delivery_events":
            raise ControlError("交付发布意图与真实物理操作不一致")
        publications_by_reference[canonical_sha256(reference)] = (matches[0], publication)
        published = store.find_fact_by_idempotency_key(package["campaign_id"], publication["idempotency_key"])
        if published is None or published["fact_kind"] != "candidate_delivery_recorded" or any(published[key] != publication[key] for key in ("payload", "issued_at_utc", "producer_run", "formed_under", "idempotency_key")):
            raise ControlError("完整交付包存在未发布或错误 producer 的 Store 后处理意图")
        seed = _source_json(source, publication["seed_binding"])
        if any(seed.get(key) != publication[key] for key in ("issued_at_utc", "producer_run", "formed_under")) or seed.get("request") != prepared.get("request"):
            raise ControlError("交付补发更换了修改前冻结坐标")
        required_material.extend([operation, core, matches[0], publication])
    if operation_ids != set(by_operation):
        raise ControlError("交付包与 guard 全部操作索引不闭合，不能删除失败或未发布操作")
    # 保留原末尾完整验图位置。一个执行对象的有效事实和发布意图在这次
    # 完整验图后立即消费；不同对象、恢复图以及独立调用均重新完整验证。
    for reference in execution_refs:
        payload = store.load_object(reference)["payload"]
        graph_source = validate_evidence_graph(store, payload, current_execution=current_execution)
        core = evidence_facts(store, reference, "stage_execution")
        if by_operation.get(core["operation_id"]) != (reference, core, payload):
            raise ControlError("完整交付执行对象与装配时原件不一致")
        key = canonical_sha256(reference)
        for fact_reference, formed_fact in stages_by_reference.get(key, []):
            current_fact = store.load_fact(fact_reference)
            if current_fact != formed_fact:
                raise ControlError("有效交付阶段事实在完整验图前后漂移")
            _validate_stage_execution_semantics(store, plan, current_fact["payload"], core, graph_source)
        publication_binding, formed_publication = publications_by_reference[key]
        publication = publication_document(source, publication_binding, package["campaign_id"], package["delivery_id"], core["operation_id"])
        if publication != formed_publication:
            raise ControlError("交付发布意图在完整验图前后漂移")
        _validate_stage_execution_semantics(store, plan, publication["payload"], core, graph_source)
        published = store.find_fact_by_idempotency_key(package["campaign_id"], publication["idempotency_key"])
        if published is None or published["fact_kind"] != "candidate_delivery_recorded" or any(published[name] != publication[name] for name in ("payload", "issued_at_utc", "producer_run", "formed_under", "idempotency_key")):
            raise ControlError("完整交付包存在未发布或错误 producer 的 Store 后处理意图")
        required_material.append(payload)
    for reference in package["restoration_evidence_refs"]:
        payload = store.load_object(reference)["payload"]
        validate_evidence_graph(store, payload, current_execution=current_execution)
        required_material.append(payload)
    archive = store.load_object(package["private_archive_manifest_ref"])["payload"]
    by_path = {row["path"]: row for row in archive["entries"]}
    for binding in complete_delivery_raw_bindings(store, plan, source, required_material):
        if by_path.get(binding["path"]) != binding:
            raise ControlError("交付归档遗漏失败、恢复、提交或发布意图的原件")
        _source_bytes(source, binding)


def validate_delivery_receipt(store: Any, receipt: Mapping[str, Any], *, current: bool = False) -> None:
    if receipt.get("schema_version") != RECEIPT_V4:
        raise ControlError("当前 Claude 交付完成权要求 Receipt v4，历史收据不能冒充")
    package = store.load_object(receipt["candidate_delivery_package_ref"])["payload"]
    validate_delivery_package(store, package, current_execution=current)
    plan = store.load_object(package["delivery_plan_ref"])["payload"]
    expected = receipt_execution_inputs(package, plan)
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ControlError("交付收据未闭合执行配置、来源、操作及恢复输入")
    from .receipts import _build_candidate_delivery_receipt_unchecked
    rebuilt = _build_candidate_delivery_receipt_unchecked(store, receipt["campaign_id"], receipt["candidate_delivery_package_ref"], formed_under=receipt["formed_under"], schema=RECEIPT_V4)
    if canonical_sha256(rebuilt) != canonical_sha256(dict(receipt)):
        raise ControlError("交付收据核心字段与完整验收／恢复链的确定性重建不一致")
    if current:
        from .receipts import require_receipt_current
        require_receipt_current(store, dict(receipt))
