"""受管 Docker Profile 观测副本的精确来源投影。

权限来自已经匹配完整 producer core 的守卫通道，以及该通道明确列出的
producer 原件、pointer 和证明绑定。证明的 schema、文件名或相同摘要本身
不授予权限。本模块只读取调用方提供的已验真字节，不调用 Store、RPC、
Plan 或 Package；同一个校验器供生产导出、客户端和归档消费者使用。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import io
from pathlib import PurePosixPath
import re
import tarfile
from typing import Any, Callable, Mapping

from .canonical import (canonical_sha256, expect_exact_keys, expect_object,
                        expect_safe_id, parse_json_bytes, sha256_bytes,
                        validate_external_binding)
from .errors import ControlError

PROOF_SCHEMA = "claude-upgrade-profile-observation-proof/v1"
RUNTIME_IDENTITY_SCHEMA = "claude-upgrade-runtime-profile-identity/v2"
HOST_IDENTITY_SCHEMA = "claude-upgrade-host-profile-identity/v1"
PROOF_FIELDS = {"schema_version", "configuration_binding", "configuration_sha256",
                "source_scope", "campaign_id", "candidate_id", "delivery_id",
                "collection_phase", "capture_coordinate", "role", "producer_identity",
                "source_profile_binding", "observed_profile_binding", "observation"}
COORDINATE_FIELDS = {"root_relative", "observation_relative", "operation_id",
                     "freeze_request_sha256", "freeze_sequence", "recovery_attempt"}
MATERIAL_FIELDS = {"collection_phase", "producer_binding", "observation_binding", "proof_references"}
REFERENCE_FIELDS = {"producer_pointer", "identity_binding", "identity_pointer", "proof_binding"}
RUNTIME_FIELDS = {"schema_version", "runtime_profile_sha256", "profile_binding", "profile_observation_proof_binding"}
HOST_FIELDS = {"schema_version", "active_profile_relative", "profile_binding",
               "profile_observation_proof_binding", "container_profile_identity_binding"}
CONTAINER_FIELDS = {"kind", "container_id", "container_profile_path", "application_service",
                    "container_inspect_binding", "image_inspect_binding", "profile_tar_binding",
                    "image_digest", "profile_sha256"}
HOST_OBSERVATION_FIELDS = {"kind", "active_profile_relative", "container_proof_binding", "profile_sha256"}
PRODUCER_IDENTITY_FIELDS = {"contract_version", "authority", "source_scope", "host_id", "instance_id", "service_name",
                            "service_instance_id", "boot_id", "pid", "process_start", "process_group_id", "independent_service",
                            "service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"}
PHASES = {"freeze", "before", "execution", "recovery"}


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    result = expect_object(value, label)
    expect_exact_keys(result, fields, label)
    return result


def _binding(value: Any) -> dict[str, Any]:
    return validate_external_binding(value, "profile.observation_binding")


def _raw(read_raw: Callable[[Mapping[str, Any]], bytes], binding: Any) -> bytes:
    parsed = _binding(binding)
    raw = read_raw(parsed)
    if not isinstance(raw, bytes) or len(raw) != parsed["bytes"] or sha256_bytes(raw) != parsed["sha256"]:
        raise ControlError("Profile 观测证明的原件缺失或完整字节漂移")
    return raw


def _json(read_raw: Callable[[Mapping[str, Any]], bytes], binding: Any) -> Any:
    return parse_json_bytes(_raw(read_raw, binding), "Profile 观测证明原件")


def _pointer(document: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith("/") or "~" in pointer:
        raise ControlError("Profile producer pointer 非法")
    node = document
    try:
        for part in pointer[1:].split("/"):
            if isinstance(node, list):
                if not part.isdigit() or str(int(part)) != part:
                    raise ValueError("数组坐标非法")
                node = node[int(part)]
            else:
                node = node[part]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ControlError("Profile producer pointer 不可达") from error
    return node


def extract_profile_tar(raw: bytes, container_path: str) -> bytes:
    """只读取唯一规范目标成员；禁止链接、重复、额外成员和伪目标路径。"""
    target = PurePosixPath(container_path)
    if not target.is_absolute() or str(target) != container_path or ".." in target.parts:
        raise ControlError("容器 Profile 目标路径非法")
    try:
        if not raw or len(raw) % tarfile.BLOCKSIZE:
            raise ControlError("容器 Profile tar 不是完整块原件")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
            archive.ignore_zeros = True
            members = archive.getmembers()
            if (len(members) != 1 or members[0].type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
                    or members[0].name != target.name or members[0].linkname
                    or raw[156:157] not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.XHDTYPE}
                    or set(members[0].pax_headers) - {"mtime", "atime", "ctime"}
                    or not 0 <= members[0].size <= 4 * 1024 * 1024):
                raise ControlError("容器 Profile tar 必须仅含唯一普通目标，禁止链接、重复或额外成员")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ControlError("容器 Profile tar 的目标不可读取")
            extracted = stream.read()
            if len(extracted) != members[0].size:
                raise ControlError("容器 Profile tar 目标长度漂移")
            parse_json_bytes(extracted, "容器 Profile 完整原字节")
            return extracted
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ControlError("容器 Profile 原 tar 损坏") from error


def capture_coordinate(configuration: Mapping[str, Any], phase: str, relative: str) -> dict[str, Any]:
    """从真实受管目录交叉推出用途坐标，不使用调用者猜测的 operation ID。"""
    if phase not in PHASES:
        raise ControlError("Profile 采集用途未登记")
    guard_root = configuration["paths"]["guard_root_relative"]
    path = PurePosixPath(relative)
    if str(path) != relative or path.is_absolute() or ".." in path.parts:
        raise ControlError("Profile 观测目录不是规范相对坐标")
    base = {"root_relative": None, "observation_relative": relative, "operation_id": None,
            "freeze_request_sha256": None, "freeze_sequence": None, "recovery_attempt": None}
    if phase == "freeze":
        match = re.fullmatch(re.escape(guard_root) + r"/freezes/([0-9a-f]{64})/([0-9]{6})/before", relative)
        if match is None or int(match[2]) < 1:
            raise ControlError("Profile freeze 坐标与真实采集目录不一致")
        base.update(root_relative=str(path.parent), freeze_request_sha256=match[1], freeze_sequence=int(match[2]))
    else:
        match = re.fullmatch(re.escape(guard_root) + r"/operations/([A-Za-z0-9._-]+)/(.+)", relative)
        if match is None:
            raise ControlError("Profile operation 坐标越出冻结守卫根")
        operation_id, suffix = match.groups()
        if not re.fullmatch(r"op-[0-9a-f]{32}", operation_id):
            raise ControlError("Profile operation ID 不是守卫固定坐标")
        base.update(root_relative=guard_root + "/operations/" + operation_id, operation_id=operation_id)
        if phase == "before" and suffix != "before":
            raise ControlError("Profile before 采集目录不一致")
        if phase == "execution" and not re.fullmatch(r"during/(?:window-[0-9]{6}/)?identity", suffix):
            raise ControlError("Profile execution 采集目录不一致")
        if phase == "recovery":
            recovery = re.fullmatch(r"restorations/([0-9]{6})/observed/(?:window-[0-9]{6}/)?identity", suffix)
            if recovery is None or int(recovery[1]) < 1:
                raise ControlError("Profile recovery 采集目录不一致")
            base["recovery_attempt"] = int(recovery[1])
    return base


def _producer_identity(value: Any, configuration: Mapping[str, Any]) -> dict[str, Any]:
    """保留形成期完整 producer；当前通道 PID 不能替换原生产实例。"""
    identity = _exact(value, PRODUCER_IDENTITY_FIELDS, "profile.producer_identity")
    expected = {"contract_version": "claude-upgrade-delivery-guard-identity/v1",
                "authority": "claude-arm64-recovery-guard", "source_scope": configuration["source_scope"],
                "host_id": "ARM64", "instance_id": configuration["instance_id"],
                "service_name": configuration["guard"]["service_name"]}
    if any(identity[key] != item for key, item in expected.items()):
        raise ControlError("Profile producer 的配置、用途或部署身份不一致")
    for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"):
        if identity[key] != configuration["guard"][key]:
            raise ControlError("Profile producer 的原部署绑定漂移")
    if configuration["source_scope"] == "formal" and identity["independent_service"] is not True:
        raise ControlError("正式 Profile 观测缺独立守卫服务来源")
    return identity


def _producer_material(configuration: Mapping[str, Any], context: Mapping[str, Any], material: Any,
                       read_raw: Callable[[Mapping[str, Any]], bytes]) -> tuple[dict[str, Any], Any, str]:
    item = _exact(material, MATERIAL_FIELDS, "profile.producer_material")
    phase = item["collection_phase"]
    if phase not in PHASES:
        raise ControlError("Profile producer 的采集用途非法")
    producer_binding = _binding(item["producer_binding"])
    producer = expect_object(_json(read_raw, producer_binding), "profile.producer")
    document = producer
    guard_root = configuration["paths"]["guard_root_relative"]
    if phase == "freeze":
        if item["observation_binding"] is not None or not re.fullmatch(re.escape(guard_root) + r"/freezes/[0-9a-f]{64}/[0-9]{6}/snapshot.json", producer_binding["path"]):
            raise ControlError("Profile freeze producer 不在冻结坐标")
        _exact(producer, {"environment", "rollback", "gates", "cores"}, "profile.freeze_snapshot")
        core = producer["cores"]["freeze"]
        if core["campaign_id"] != context["campaign_id"] or core["delivery_id"] != context["delivery_id"]:
            raise ControlError("Profile freeze producer 跨交付")
        identity = core["guard_identity"]
        pointer_prefix = "/cores/freeze/before_bindings/"
    elif phase in {"before", "execution"}:
        _exact(producer, {"schema_version", "operation_id", "configuration_sha256", "context", "request", "target", "rollback", "before_bindings", "guard_identity", "owner", "prepared_at_utc"}, "profile.prepared")
        if producer["schema_version"] != "claude-upgrade-delivery-prepared/v1" or producer["configuration_sha256"] != canonical_sha256(configuration):
            raise ControlError("Profile prepared 配置或合同不一致")
        stable_context = {key: value for key, value in context.items() if key not in {"segment", "run_nonce"}}
        if producer["context"] != stable_context:
            raise ControlError("Profile prepared 的冻结上下文漂移")
        expected_path = guard_root + "/operations/" + producer["operation_id"] + "/prepared.json"
        if producer_binding["path"] != expected_path:
            raise ControlError("Profile prepared 原父坐标漂移")
        identity = producer["guard_identity"]
        if phase == "before":
            if item["observation_binding"] is not None:
                raise ControlError("Profile before 不接受其它观测父")
            pointer_prefix = "/before_bindings/"
        else:
            observation = _binding(item["observation_binding"])
            if observation["path"] != expected_path.removesuffix("prepared.json") + "observation.json":
                raise ControlError("Profile execution 观测父坐标漂移")
            document = expect_object(_json(read_raw, observation), "profile.execution_observation")
            if document.get("stage") != producer["request"]["stage"] or document.get("before_bindings") != producer["before_bindings"] or document.get("result") not in {"pass", "failed"}:
                raise ControlError("Profile execution 观测不属于原 prepared")
            pointer_prefix = "/during_bindings/"
    else:
        _exact(producer, {"schema_version", "operation_id", "status", "prepared_sha256", "physical_intent_sha256", "physical_intent_binding", "restoration", "guard_identity"}, "profile.recovery_terminal")
        if producer["schema_version"] != "claude-upgrade-delivery-guard-terminal/v1" or producer["status"] != "restored":
            raise ControlError("Profile recovery 不接受未验真的恢复目标")
        identity = producer["guard_identity"]
        operation_root = guard_root + "/operations/" + producer["operation_id"] + "/"
        if not re.fullmatch(re.escape(operation_root) + r"terminals/[0-9]{6}.json", producer_binding["path"]):
            raise ControlError("Profile recovery 终态父坐标漂移")
        observation = _binding(item["observation_binding"])
        if (not re.fullmatch(re.escape(operation_root) + r"restorations/[0-9]{6}/result.json", observation["path"])
                or observation not in producer["restoration"]["bindings"]):
            raise ControlError("Profile recovery 缺原终态显式观测引用")
        document = _exact(_json(read_raw, observation), {"target", "actual", "checks", "bindings", "result"}, "profile.recovery_observation")
        if document["result"] != "pass" or document["target"] != {key: configuration["rollback"][key] for key in ("image_digest", "runtime_profile_sha256")}:
            raise ControlError("Profile recovery 不是冻结回退的通过原件")
        pointer_prefix = "/bindings/"
    identity = _producer_identity(identity, configuration)
    for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"):
        _raw(read_raw, identity[key])
    return identity, document, pointer_prefix


def select_profile_proof_references(document: Any, pointer_prefix: str,
                                    read_raw: Callable[[Mapping[str, Any]], bytes], *,
                                    configuration: Mapping[str, Any]) -> list[dict[str, Any]]:
    """只定位 producer 原合同中的显式 identity 引用；此函数不授予权限。"""
    references = []
    bindings = _pointer(document, pointer_prefix.rstrip("/"))
    if not isinstance(bindings, list):
        raise ControlError("Profile producer 的原件数组非法")
    by_path = {_binding(row)["path"]: _binding(row) for row in bindings}
    for index, binding in enumerate(bindings):
        name = PurePosixPath(binding["path"]).name
        if name not in {"profile-identity.json", "host-profile-identity.json"}:
            continue
        raw = expect_object(_json(read_raw, binding), "profile.producer_identity_observation")
        # FileRuntime 的历史文件现场无真实 Docker 副本，维持同源夹具原合同。
        if "profile_binding" not in raw:
            continue
        fields = RUNTIME_FIELDS if name == "profile-identity.json" else HOST_FIELDS
        _exact(raw, fields, "profile.producer_identity_observation")
        expected_schema = RUNTIME_IDENTITY_SCHEMA if name == "profile-identity.json" else HOST_IDENTITY_SCHEMA
        if raw["schema_version"] != expected_schema or by_path.get(raw["profile_binding"]["path"]) != raw["profile_binding"]:
            raise ControlError("当前 Docker Profile 观测副本缺精确 identity 原父")
        proof = raw["profile_observation_proof_binding"]
        if proof is None:
            if name == "host-profile-identity.json":
                raise ControlError("当前 Docker host Profile 缺同次来源证明")
            image_path = str(PurePosixPath(binding["path"]).with_name("image-identity.json"))
            if image_path in by_path:
                image_identity = expect_object(_json(read_raw, by_path[image_path]), "profile.image_identity")
                measured = (image_identity.get("image_digest"), raw.get("runtime_profile_sha256"))
                roles = {(configuration[role]["image_digest"], configuration[role]["runtime_profile_sha256"]) for role in ("candidate", "rollback")}
                if measured in roles:
                    raise ControlError("当前 Docker Profile 实测已匹配冻结角色但来源证明缺失")
            # 未匹配角色的真实观测保留严格域；不能取得计划目标的派生权限。
            continue
        if by_path.get(proof["path"]) != proof:
            raise ControlError("当前 Docker Profile 观测副本缺显式证明原件")
        references.append({"producer_pointer": pointer_prefix + str(index), "identity_binding": dict(binding),
                           "identity_pointer": "/profile_observation_proof_binding", "proof_binding": _binding(proof)})
    # 当前 Docker 原副本不能通过删 identity 或改合同降为 FileRuntime。
    for binding in bindings:
        if PurePosixPath(binding["path"]).name in {"runtime-profile.json", "host-active-profile.json"}:
            identity_name = "profile-identity.json" if PurePosixPath(binding["path"]).name == "runtime-profile.json" else "host-profile-identity.json"
            identity_path = str(PurePosixPath(binding["path"]).with_name(identity_name))
            if identity_path not in by_path:
                raise ControlError("当前 Docker Profile 观测副本缺显式 identity 与来源证明")
    return references


@dataclass(frozen=True)
class VerifiedProfileProjection:
    """校验器产生的内存能力；完整原字节和精确 proof 坐标均不可由 JSON 列表替代。"""

    configuration_sha256: str
    deployment_sha256: str
    proof_bindings: tuple[dict[str, Any], ...]
    producer_materials: tuple[dict[str, Any], ...]
    _configuration: Mapping[str, Any]
    _context: Mapping[str, Any]
    _documents: Mapping[str, tuple[dict[str, Any], bytes]]

    def material_bindings(self) -> list[dict[str, Any]]:
        """归档完整 producer 原父及相关观测；证明引用不能代替来源原件。"""
        found = {}
        for material in self.producer_materials:
            for value in (material["producer_binding"], material["observation_binding"]):
                if value is not None:
                    binding = _binding(value)
                    found[binding["path"]] = binding
        return [found[key] for key in sorted(found)]

    def revalidate(self, read_raw: Callable[[Mapping[str, Any]], bytes], *, purpose: str) -> "VerifiedProfileProjection":
        """归档、Package 和 Receipt 重新跑同一纯合同，不能只信先前的能力对象。"""
        checked = validate_profile_observation_projection(self._configuration, self._context, list(self.proof_bindings),
                                                          read_raw, purpose=purpose, producer_materials=list(self.producer_materials))
        if checked != self:
            raise ControlError("Profile 内存投影与完整纯来源复验不一致，不能附加未授权路径")
        return checked

    def project(self, parent_binding: Mapping[str, Any], raw: bytes) -> list[dict[str, Any]] | None:
        parsed = _binding(dict(parent_binding))
        verified = self._documents.get(parsed["path"])
        if verified is None:
            return None
        if verified[0] != parsed or verified[1] != raw:
            raise ControlError("Profile 派生域的精确父字节漂移")
        # 完整 Profile 父已归档；其历史来源仅由同角色原资格图按形成期解释。
        return []


def validate_profile_observation_projection(configuration: Mapping[str, Any], context: Mapping[str, Any],
                                            proof_bindings: list[dict[str, Any]],
                                            read_raw: Callable[[Mapping[str, Any]], bytes], *,
                                            purpose: str, producer_materials: list[dict[str, Any]]) -> VerifiedProfileProjection:
    """由已匹配 producer 材料授精确派生域；所有来源和消费者共用此纯合同。"""
    if purpose not in {"current_execution", "formed_replay"}:
        raise ControlError("Profile 投影消费目的未登记")
    if not isinstance(proof_bindings, list) or not isinstance(producer_materials, list):
        raise ControlError("Profile 来源合同缺显式证明及 producer 材料数组")
    if [canonical_sha256(row) for row in producer_materials] != sorted({canonical_sha256(row) for row in producer_materials}):
        raise ControlError("Profile producer 材料必须排序且不重复")
    configuration_binding = _binding(context["configuration_ref"])
    if _json(read_raw, configuration_binding) != configuration:
        raise ControlError("Profile 证明不是同一冻结配置原字节")
    configuration_sha256 = canonical_sha256(configuration)
    for key in ("campaign_id", "candidate_id", "delivery_id"):
        expect_safe_id(context[key], "profile." + key)
    if (context["campaign_id"], context["candidate_id"]) != (configuration["candidate"]["qualification"]["campaign_id"], configuration["candidate"]["qualification"]["candidate_id"]):
        raise ControlError("Profile 证明冻结上下文不属于当前 Candidate")
    authorized = {}
    for material in producer_materials:
        item = _exact(material, MATERIAL_FIELDS, "profile.producer_material")
        identity, document, prefix = _producer_material(configuration, context, item, read_raw)
        declared = item["proof_references"]
        if not isinstance(declared, list) or declared != select_profile_proof_references(document, prefix, read_raw, configuration=configuration):
            raise ControlError("Profile proof 引用不是 producer 原合同的完整显式闭集")
        for reference in declared:
            row = _exact(reference, REFERENCE_FIELDS, "profile.proof_reference")
            if not row["producer_pointer"].startswith(prefix) or _pointer(document, row["producer_pointer"]) != row["identity_binding"]:
                raise ControlError("Profile proof 的 producer pointer 未绑定精确原父")
            identity_document = _json(read_raw, row["identity_binding"])
            if row["identity_pointer"] != "/profile_observation_proof_binding" or _pointer(identity_document, row["identity_pointer"]) != row["proof_binding"]:
                raise ControlError("Profile proof 的 identity pointer 不可达")
            binding = _binding(row["proof_binding"])
            previous = authorized.setdefault(binding["path"], (binding, item, identity, row, identity_document))
            if previous != (binding, item, identity, row, identity_document):
                raise ControlError("同一 Profile proof 被不同 producer 授权")
    expected = [row[0] for _path, row in sorted(authorized.items())]
    if proof_bindings != expected:
        raise ControlError("Profile 来源证明集合遗漏、重复或含未授权原件")
    documents: dict[str, tuple[dict[str, Any], bytes]] = {}
    # 冻结配置的两个来源父也保留完整字节；历史内部坐标由原资格图解释。
    # 权限来自同一配置原件及本校验器，不能由归档调用者传入路径列表授予。
    for role in ("candidate", "rollback"):
        source_material = configuration[role]
        source_binding = _binding(source_material["profile_binding"])
        if source_binding["sha256"] != source_material["runtime_profile_sha256"]:
            raise ControlError("Profile 来源父不是冻结角色的完整字节")
        source_raw = _raw(read_raw, source_binding)
        previous = documents.setdefault(source_binding["path"], (source_binding, source_raw))
        if previous != (source_binding, source_raw):
            raise ControlError("Profile 两角色来源父对同一路径声明不同字节")
    validated_proofs: dict[str, dict[str, Any]] = {}
    visiting_proofs: set[str] = set()

    def validate_proof(binding: dict[str, Any]) -> dict[str, Any]:
        if binding["path"] in validated_proofs:
            return validated_proofs[binding["path"]]
        if binding["path"] in visiting_proofs:
            raise ControlError("Profile proof 的原父引用形成循环")
        visiting_proofs.add(binding["path"])
        _bound, material, producer, reference, identity_document = authorized[binding["path"]]
        proof = _exact(_json(read_raw, binding), PROOF_FIELDS, "profile.observation_proof")
        if (proof["schema_version"] != PROOF_SCHEMA or proof["configuration_binding"] != configuration_binding
                or proof["configuration_sha256"] != configuration_sha256 or proof["source_scope"] != configuration["source_scope"]
                or proof["producer_identity"] != producer or proof["collection_phase"] != material["collection_phase"]
                or any(proof[key] != context[key] for key in ("campaign_id", "candidate_id", "delivery_id"))):
            raise ControlError("Profile proof 与原 producer、冻结配置或用途不一致")
        coordinate = _exact(proof["capture_coordinate"], COORDINATE_FIELDS, "profile.capture_coordinate")
        if coordinate != capture_coordinate(configuration, proof["collection_phase"], coordinate["observation_relative"]):
            raise ControlError("Profile proof 的用途坐标不是实际冻结目录")
        parent_directory = str(PurePosixPath(material["producer_binding"]["path"]).parent)
        if proof["collection_phase"] == "freeze":
            if coordinate["root_relative"] != parent_directory:
                raise ControlError("Profile proof freeze 坐标与原 snapshot 不一致")
        elif proof["collection_phase"] in {"before", "execution"}:
            if coordinate["root_relative"] != parent_directory:
                raise ControlError("Profile proof operation 坐标与原 prepared 不一致")
        elif coordinate["root_relative"] != str(PurePosixPath(parent_directory).parent):
            raise ControlError("Profile proof recovery 坐标与原 terminal 不一致")
        role = proof["role"]
        if role not in {"candidate", "rollback"}:
            raise ControlError("Profile proof 角色未登记")
        source = _binding(proof["source_profile_binding"])
        observed = _binding(proof["observed_profile_binding"])
        if source != configuration[role]["profile_binding"] or source["sha256"] != configuration[role]["runtime_profile_sha256"]:
            raise ControlError("Profile proof 的原父不是冻结角色的精确来源")
        source_raw, observed_raw = _raw(read_raw, source), _raw(read_raw, observed)
        if source_raw != observed_raw:
            raise ControlError("Profile proof 的源父与观测副本完整字节不相同")
        observation = expect_object(proof["observation"], "profile.observation")
        directory = coordinate["observation_relative"]
        if observation.get("kind") == "container":
            _exact(observation, CONTAINER_FIELDS, "profile.container_observation")
            if (binding["path"] != directory + "/runtime-profile-proof.json"
                    or observed["path"] != directory + "/runtime-profile.json"
                    or reference["identity_binding"]["path"] != directory + "/profile-identity.json"
                    or identity_document["schema_version"] != RUNTIME_IDENTITY_SCHEMA
                    or identity_document["profile_binding"] != observed
                    or identity_document["runtime_profile_sha256"] != observed["sha256"]):
                raise ControlError("Profile container 证明与精确观测原父不一致")
            runtime = configuration["runtime"]
            if observation["application_service"] != runtime["application_service"] or observation["container_profile_path"] != runtime["profile_container_path"]:
                raise ControlError("Profile container 观测不在冻结服务／路径")
            for field, name in (("container_inspect_binding", runtime["application_service"] + "-inspect.json"),
                                ("image_inspect_binding", "application-image-inspect.json"),
                                ("profile_tar_binding", "runtime-profile.tar")):
                if _binding(observation[field])["path"] != directory + "/" + name:
                    raise ControlError("Profile Docker 原件不在同次精确采集目录")
            containers, images = _json(read_raw, observation["container_inspect_binding"]), _json(read_raw, observation["image_inspect_binding"])
            if not isinstance(containers, list) or len(containers) != 1 or not isinstance(images, list) or len(images) != 1:
                raise ControlError("Profile Docker inspect 原件必须是唯一对象")
            container, image = expect_object(containers[0], "profile.container"), expect_object(images[0], "profile.image")
            labels = container.get("Config", {}).get("Labels", {})
            actual_image = container.get("Config", {}).get("Image", "")
            image_digest = actual_image.rsplit("@", 1)[-1]
            if (container.get("Id") != observation["container_id"] or not re.fullmatch(r"[0-9a-f]{12,64}", observation["container_id"])
                    or labels.get("com.docker.compose.project") != runtime["compose_project"]
                    or labels.get("com.docker.compose.service") != runtime["application_service"]
                    or any(labels.get(key) != value for key, value in runtime["expected_labels"].items())
                    or image.get("Id") != container.get("Image") or actual_image not in image.get("RepoDigests", [])
                    or image.get("Os") != "linux" or image.get("Architecture") not in {"arm64", "aarch64"}
                    or observation["image_digest"] != image_digest or image_digest != configuration[role]["image_digest"]
                    or observation["profile_sha256"] != observed["sha256"]):
                raise ControlError("Profile container/image/Profile 实测没有共同闭合冻结角色")
            if extract_profile_tar(_raw(read_raw, observation["profile_tar_binding"]), runtime["profile_container_path"]) != observed_raw:
                raise ControlError("Profile tar 提取原字节与观测副本不一致")
        elif observation.get("kind") == "host":
            _exact(observation, HOST_OBSERVATION_FIELDS, "profile.host_observation")
            if (proof["collection_phase"] != "before" or binding["path"] != directory + "/host-active-profile-proof.json"
                    or observed["path"] != directory + "/host-active-profile.json"
                    or reference["identity_binding"]["path"] != directory + "/host-profile-identity.json"
                    or identity_document["schema_version"] != HOST_IDENTITY_SCHEMA
                    or identity_document["profile_binding"] != observed
                    or identity_document["active_profile_relative"] != configuration["paths"]["active_profile_relative"]
                    or observation["active_profile_relative"] != configuration["paths"]["active_profile_relative"]
                    or observation["profile_sha256"] != observed["sha256"]):
                raise ControlError("Profile host 证明没有绑定原 active 观测父")
            container_binding = _binding(observation["container_proof_binding"])
            if container_binding["path"] not in authorized or authorized[container_binding["path"]][0] != container_binding:
                raise ControlError("Profile host 缺同次已授权 container 原证明")
            container_proof = validate_proof(container_binding)
            if (container_proof["observation"]["kind"] != "container" or container_proof["capture_coordinate"] != coordinate
                    or container_proof["role"] != role or container_proof["producer_identity"] != producer
                    or container_proof["observed_profile_binding"]["sha256"] != observed["sha256"]
                    or identity_document["container_profile_identity_binding"] != authorized[container_binding["path"]][3]["identity_binding"]):
                raise ControlError("Profile host 与同次 container 原父或完整字节不一致")
        else:
            raise ControlError("Profile 观测类型未登记")
        old = documents.setdefault(observed["path"], (observed, observed_raw))
        if old != (observed, observed_raw):
            raise ControlError("Profile 观测父存在冲突来源")
        validated_proofs[binding["path"]] = proof
        visiting_proofs.remove(binding["path"])
        return proof

    for proof_binding in proof_bindings:
        validate_proof(proof_binding)
    return VerifiedProfileProjection(configuration_sha256, configuration["guard"]["deployment_receipt_binding"]["sha256"],
                                     tuple(deepcopy(proof_bindings)), tuple(deepcopy(producer_materials)),
                                     deepcopy(configuration), deepcopy(context), deepcopy(documents))
