"""封存门禁第二根的纯父域投影；所有归档消费者使用同一个严格闭包。

只有已验真父原件的精确 /external_gate 指针可以转换根坐标。未知 JSON
仍实读递归其标准三字段绑定；相同 schema、文件名或摘要不授予权限。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from .canonical import canonical_sha256, parse_json_bytes, sha256_bytes
from .errors import ControlError


PROJECTION_SCHEMA = "official-client-sealed-gate-binding-projection/v1"
_TOKEN = object()


@dataclass(frozen=True)
class VerifiedGateProjection:
    parents: tuple[Mapping[str, Any], ...]
    domains: tuple[Mapping[str, Any], ...]
    archive_bindings: tuple[Mapping[str, Any], ...]
    material_files: tuple[Mapping[str, Any], ...]
    token: object
    subject_projections: tuple[Any, ...] = ()
    reader_replays: tuple[Any, ...] = ()

    def project(self, parent_binding: Mapping[str, Any], raw: bytes) -> list[dict[str, Any]] | None:
        """单独调用也建立本次精确索引；索引不带来源或跨调用权限。"""
        return _gate_binding_projector(self)(parent_binding, raw)

    def manifest(self) -> dict[str, Any]:
        return {"schema_version": PROJECTION_SCHEMA, "domains": list(self.domains), "archive_bindings": list(self.archive_bindings), "material_files": list(self.material_files)}


def _binding_key(binding: Mapping[str, Any]) -> tuple[str, str, int]:
    return binding["path"], binding["sha256"], binding["bytes"]


def _material_key(row: Mapping[str, Any]) -> tuple[str, str, str, int]:
    return row["root_id"], row["source_relative_path"], row["content_sha256"], row["content_bytes"]


def _gate_binding_projector(proof: VerifiedGateProjection):
    """仅在一次闭包遍历内索引绑定；每份父字节仍实读，精确映射与重复拒绝不变。"""
    from .delivery_execution import typed_bindings
    from tools.official_client_capture.claude_qualification_material_source import (
        VerifiedQualificationSubjectProjection, _TOKEN as subject_token,
    )
    if not isinstance(proof, VerifiedGateProjection) or proof.token is not _TOKEN:
        raise ControlError("门禁投影不是纯校验器返回的受管结果")
    subject_index, material_index, coordinate_index, domain_index = {}, {}, {}, {}
    for item in proof.subject_projections:
        if not isinstance(item, VerifiedQualificationSubjectProjection) or item.token is not subject_token:
            raise ControlError("subject 投影不能由 JSON 或任意对象授信")
        subject_index.setdefault(_binding_key(item.parent_binding), []).append(item)
    for row in proof.material_files:
        material_index.setdefault(_binding_key(row["archive_binding"]), []).append(row)
        coordinate_index.setdefault(_material_key(row), []).append(row)
    for row in proof.domains:
        domain_index.setdefault(_binding_key(row["parent_binding"]), []).append(row)

    def project(parent_binding: Mapping[str, Any], raw: bytes) -> list[dict[str, Any]] | None:
        key = _binding_key(parent_binding)
        for item in subject_index.get(key, []):
            projected = item.project(parent_binding, raw)
            if projected is None:
                raise ControlError("subject 投影坐标在本次遍历期间漂移")
            return projected
        materials = material_index.get(key, [])
        if materials:
            if len(materials) != 1 or materials[0]["archive_binding"] != dict(parent_binding) or sha256_bytes(raw) != parent_binding["sha256"] or len(raw) != parent_binding["bytes"]:
                raise ControlError("门禁复制原件在严格投影时漂移或映射重复")
            try:
                material = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return []
            root_id = materials[0]["root_id"]
            output = []
            for binding in typed_bindings(material):
                matches = coordinate_index.get((root_id, *_binding_key(binding)), [])
                if len(matches) != 1 or _material_key(matches[0]) != (root_id, *_binding_key(binding)):
                    raise ControlError("复制原件未知 JSON 的下层绑定缺精确同根映射")
                output.append(dict(matches[0]["archive_binding"]))
            return output
        rows = domain_index.get(key, [])
        if not rows:
            return None
        if len(rows) != 1 or rows[0]["parent_binding"] != dict(parent_binding) or len(raw) != parent_binding["bytes"] or sha256_bytes(raw) != parent_binding["sha256"]:
            raise ControlError("门禁投影父原件在使用时漂移或存在角色冲突")
        document = parse_json_bytes(raw)
        row = rows[0]
        if document.get("schema_version") != row["parent_schema"] or canonical_sha256(document.get("external_gate")) != row["domain_sha256"] or row["domain_pointer"] != "/external_gate":
            raise ControlError("门禁投影不是已验真的精确父域")
        return typed_bindings({key: value for key, value in document.items() if key != "external_gate"}) + [dict(binding) for binding in proof.archive_bindings]

    return project


def project_delivery_raw_bindings(source: Any, initial: Any, *, gate_projection: VerifiedGateProjection | None,
                                  profile_projection: Any = None,
                                  profile_documents: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """完整实读父字节后投影；该函数不取来源、不 RPC、不重放交付上层图。"""
    from .delivery_execution import _source_bytes, typed_bindings, _revalidate_guard_continuity
    from .profile_observation_bindings import VerifiedProfileProjection, RUNTIME_IDENTITY_SCHEMA, HOST_IDENTITY_SCHEMA
    if profile_documents:
        raise ControlError("Profile 文件列表不能替代已验证来源投影")
    if profile_projection is not None:
        if not isinstance(profile_projection, VerifiedProfileProjection):
            raise ControlError("Profile 投影不能由 JSON 或任意对象授信")
        profile_projection.revalidate(lambda row: _source_bytes(source, row), purpose="formed_replay")
    guard_proof = _revalidate_guard_continuity(source)
    continuity_bindings = [item.as_value() for item in guard_proof.read_bindings] if guard_proof is not None else []
    bindings = {row["path"]: row for row in typed_bindings([initial, continuity_bindings])}
    # 在任何历史父域展开前逐份核身份，空投影也不能为陌生原件背书。
    for row in bindings.values():
        if row["path"].rsplit("/", 1)[-1] in {"profile-identity.json", "host-profile-identity.json"}:
            identity_raw = _source_bytes(source, row)
            identity = json.loads(identity_raw)
            if isinstance(identity, dict) and identity.get("schema_version") in {RUNTIME_IDENTITY_SCHEMA, HOST_IDENTITY_SCHEMA}:
                if profile_projection is None:
                    raise ControlError("当前 Docker Profile 观测副本缺少已验证来源证明")
                profile_projection.require_identity_parent(row, identity_raw)
    if gate_projection is not None:
        if not isinstance(gate_projection, VerifiedGateProjection) or gate_projection.token is not _TOKEN:
            raise ControlError("门禁投影类型或受管权限错误")
        for row in gate_projection.archive_bindings:
            previous = bindings.setdefault(row["path"], dict(row))
            if previous != dict(row):
                raise ControlError("门禁真实原件与初始归档绑定冲突")
    project_gate_binding = _gate_binding_projector(gate_projection) if gate_projection is not None else None
    pending = list(bindings)
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        binding = bindings[path]
        raw = _source_bytes(source, binding)
        projected = None
        if project_gate_binding is not None:
            projected = project_gate_binding(binding, raw)
        if projected is None and profile_projection is not None:
            projected = profile_projection.project(binding, raw)
        if projected is None:
            try:
                content = parse_json_bytes(raw)
            except ControlError:
                # 非 JSON 字节仍完整读验。合法数组里的绑定也要严格展开。
                try:
                    content = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
            if isinstance(content, dict) and content.get("schema_version") in {RUNTIME_IDENTITY_SCHEMA, HOST_IDENTITY_SCHEMA}:
                if profile_projection is None:
                    raise ControlError("Docker Profile 观测原件未匹配精确来源证明")
                profile_projection.require_identity_parent(binding, raw)
            projected = typed_bindings(content)
        for row in projected:
            old = bindings.setdefault(row["path"], row)
            if old != row:
                raise ControlError("交付投影闭包对同一路径声明不同字节")
            if row["path"] not in visited:
                pending.append(row["path"])
        if len(bindings) > 20000:
            raise ControlError("交付投影闭包超出固定文件数")
    return [bindings[path] for path in sorted(bindings)]
