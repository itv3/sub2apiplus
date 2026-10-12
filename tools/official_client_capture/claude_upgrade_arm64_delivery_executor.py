"""Claude ARM64 隔离候选的固定交付执行器。

配置、数据根、凭据引用、Compose 服务及探针均显式冻结。只有独立守卫
服务能够执行应用切换；客户端封票、维持租约、接收原件并提交复核。
固定安装模块为正式编排与来源重放提供接线；缺现场服务或可信回退时拒绝。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import io
import json
import os
import platform
import re
import secrets
import signal
import stat
import subprocess
import tarfile
import threading
import time
from pathlib import Path, PurePosixPath
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, parse_json_bytes, validate_relative_path
from tools.official_client_control.contracts import DIMENSIONS, validate_fact_ref
from tools.official_client_control.errors import ControlError
from tools.official_client_control import profile_observation_bindings as profile_observations
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import claude_upgrade_guard_epochs as guard_epochs

class _LocalSourceOriginals(Mapping[str, bytes]):
    """只属于本次已鉴权清单的懒读原件；每次访问重新实读，未列入项直接拒绝。

    这不是裸 data_root 回退。清单与后续完整投影逐项闭合，原字节、祖先、
    文件类型、所有者、长度与 SHA 每次核对；不保留跨调用的原字节缓存。
    """

    def __init__(self, root: Path, rows: Any, digest: Any):
        from tools.official_client_control.canonical import validate_external_binding
        if not isinstance(rows, list) or not rows or canonical_sha256(rows) != digest:
            raise guard.GuardError("本地原件清单缺失或摘要漂移")
        if len(canonical_json_bytes(rows)) > guard.MAX_RPC_BYTES // 2:
            raise guard.GuardError("本地原件清单超出固定通道上限")
        self.root = root
        bindings = {}
        for row in rows:
            try:
                value = validate_external_binding(row, "本地原件绑定")
            except ControlError as error:
                raise guard.GuardError("本地原件绑定形状或路径非法") from error
            if value["path"] in bindings:
                raise guard.GuardError("本地原件清单路径重复")
            bindings[value["path"]] = MappingProxyType(dict(value))
        if list(bindings) != sorted(bindings):
            raise guard.GuardError("本地原件清单必须按精确路径排序")
        self.bindings = MappingProxyType(bindings)
        # 首次取得也逐件实读，不能拿服务端清单摘要代替客户端原件。
        for path in self.bindings:
            self[path]

    def __iter__(self):
        return iter(self.bindings)

    def __len__(self):
        return len(self.bindings)

    def __getitem__(self, path: str) -> bytes:
        if path not in self.bindings:
            raise guard.GuardError(f"本地原件不在本次已鉴权精确清单：{path}")
        if not self.root.is_absolute() or not self.root.is_dir() or any(item.is_symlink() for item in (self.root, *self.root.parents)):
            raise guard.GuardError("本地原件受管根或祖先发生漂移")
        actual = self.root
        for part in PurePosixPath(path).parts:
            actual = actual / part
            if actual.is_symlink():
                raise guard.GuardError("本地原件路径包含符号链接")
        raw = guard.read_bytes(actual)
        row = self.bindings[path]
        if len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise guard.GuardError("本地原件字节、长度或 SHA 漂移")
        return raw

    def get(self, path: str, default: Any = None) -> bytes:
        # 未列入项不能转入共享 _source_bytes 的裸根回退路径。
        return self[path]

    def require_closure(self, paths: set[str]) -> None:
        if set(self.bindings) != paths:
            raise guard.GuardError("本地原件清单与本次完整投影闭包不相同")


class _ProducerOriginals(Mapping[str, bytes]):
    """当前固定产品根据 producer 和材料合同取得的本次精确闭集。

    只有显式 add 的绑定能实读；get 对未知路径直接拒绝，使共享消费者
    不能回退裸数据根。首次与尾次同时核文件实例、模式、祖先及真实字节。
    """

    def __init__(self, root: Path):
        self.root = root
        self.bindings: dict[str, dict[str, Any]] = {}
        self.metadata: dict[str, Any] = {}
        self._artifact_bindings: dict[str, dict[str, Any]] = {}
        self._source_requirements: dict[str, dict[str, Any]] = {}
        self._byte_records: dict[str, Any] = {}
        self._subject_dependency_roots: tuple[Path, ...] | None = None
        self._subject_dependency_probes: tuple[Path, ...] = ()

    def _register_subject_artifacts(self, requirements: Mapping[str, Mapping[str, Any]]) -> None:
        """只登记已验 subject 父合同的制品模式；既有绑定和首读身份不重设。"""
        from tools.official_client_control.canonical import validate_external_binding
        for path, binding in requirements.items():
            row = dict(validate_external_binding(dict(binding), "producer 严格制品绑定"))
            if (path != row["path"] or path in self._source_requirements
                    or self._artifact_bindings.setdefault(path, row) != row
                    or (path in self.bindings and self.bindings[path] != row)):
                raise guard.GuardError("producer 严格制品要求存在同坐标冲突")
            if path in self.metadata and self.metadata[path][3] != (stat.S_IFREG | 0o755):
                raise guard.GuardError("producer 严格制品要求不能替换首读模式")

    def _register_subject_sources(self, requirements: Mapping[str, Mapping[str, Any]]) -> None:
        """只登记已验构建整树的精确源码模式；既有绑定和首读身份不重设。"""
        from tools.official_client_control.canonical import validate_external_binding
        for path, requirement in requirements.items():
            row = dict(validate_external_binding(dict(requirement["binding"]), "producer 源码绑定"))
            mode = requirement["mode"]
            current = {"binding": row, "mode": mode}
            if (path != row["path"] or type(mode) is not int or mode not in {0o400, 0o600, 0o644, 0o755}
                    or path in self._artifact_bindings or self._source_requirements.setdefault(path, current) != current
                    or (path in self.bindings and self.bindings[path] != row)):
                raise guard.GuardError("producer 源码要求存在同坐标或分类冲突")
            if path in self.metadata and self.metadata[path][3] != (stat.S_IFREG | mode):
                raise guard.GuardError("producer 源码要求不能替换首读模式")

    def add(self, binding: Mapping[str, Any]) -> bytes:
        from tools.official_client_control.canonical import validate_external_binding
        item = dict(validate_external_binding(dict(binding), "producer 原件"))
        previous = self.bindings.setdefault(item["path"], item)
        if previous != item:
            raise guard.GuardError("producer 闭集对同一路径引用不同字节")
        return self[item["path"]]

    def _path(self, relative: str) -> tuple[Path, tuple[Any, ...], tuple[Any, ...]]:
        validate_relative_path(relative, "producer 原件路径")
        if not self.root.is_absolute() or any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise guard.GuardError("producer 数据根或祖先含符号链接")
        path = self.root
        ancestors = []
        byte_ancestors = []
        for part in PurePosixPath(relative).parts:
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise guard.GuardError(f"producer 原件祖先类型、所有者或私有模式漂移：绑定={relative}，祖先={path}，实际模式={stat.S_IMODE(info.st_mode):04o}，实际类型={stat.S_IFMT(info.st_mode):06o}，实际uid={info.st_uid}，要求目录/uid={os.getuid()}/模式=0700")
            # 目录内容可因本次独立现场报告增加；实例、所有者与模式不可改变。
            ancestors.append((str(path), info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode))
            byte_ancestors.append((str(path), *(getattr(info, key) for key in
                ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns"))))
            path = path / part
            if path.is_symlink():
                raise guard.GuardError("producer 原件路径含符号链接")
        return path, tuple(ancestors), tuple(byte_ancestors)

    def __getitem__(self, relative: str) -> bytes:
        return self.read_with_metadata(relative)[0]

    def read_with_metadata(self, relative: str, *, force_read: bool = False) -> tuple[bytes, Any]:
        """来源和 snapshot 消费同一次受管实读；未知路径仍直接拒绝。"""
        if relative not in self.bindings:
            raise guard.GuardError("原件不在本次已核 producer 和材料清单：" + relative)
        return self._read_managed(relative, self.bindings[relative], force_read=force_read)

    def _read_managed(self, relative: str, item: Mapping[str, Any] | None, *, force_read: bool = False) -> tuple[bytes, Any]:
        from tools.official_client_control.qualification_material_import import _material_successor_byte_nonce
        path, ancestors, byte_ancestors = self._path(relative)
        info = path.lstat()
        artifact = self._artifact_bindings.get(relative)
        source_requirement = self._source_requirements.get(relative)
        modes = {source_requirement["mode"]} if source_requirement is not None else ({0o755} if artifact is not None else {0o400, 0o600})
        required_mode = f"{source_requirement['mode']:04o}" if source_requirement is not None else ("0755" if artifact is not None else "0400或0600")
        if source_requirement is not None and source_requirement["binding"] != item:
            raise guard.GuardError("producer 源码绑定与本次原件闭集不同")
        if artifact is not None and artifact != item:
            raise guard.GuardError("producer 严格制品绑定与本次原件闭集不同")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) not in modes:
            raise guard.GuardError(f"producer 原件类型、所有者或私有模式漂移：绑定={relative}，文件={path}，实际模式={stat.S_IMODE(info.st_mode):04o}，实际类型={stat.S_IFMT(info.st_mode):06o}，实际uid={info.st_uid}，要求普通文件/uid={os.getuid()}/模式={required_mode}")
        metadata = (ancestors, *(getattr(info, key) for key in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")))
        byte_identity = (byte_ancestors, *metadata[1:])
        nonce = _material_successor_byte_nonce(self)
        cached = self._byte_records.get(relative)
        reuse = nonce is not None and not force_read and cached is not None and cached[0] is nonce
        if reuse and (cached[2] != byte_identity or cached[3] != dict(item or {})):
            # 祖先实例／权限及文件本身不变、仅兄弟写入改变目录时间时，缓存不足便实读。
            stable_before = tuple(row[:6] for row in cached[2][0])
            stable_current = tuple(row[:6] for row in byte_ancestors)
            if (cached[2][1:] != byte_identity[1:] or stable_before != stable_current
                    or cached[3] != dict(item or {})):
                raise guard.GuardError("producer 同段原字节的精确绑定、祖先或完整元数据漂移")
            reuse = False
        raw = cached[1] if reuse else guard.read_bytes(path)
        current_path, current_ancestors, current_byte_ancestors = self._path(relative)
        current = current_path.lstat()
        after = (current_ancestors, *(getattr(current, key) for key in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")))
        if (metadata != after or self.metadata.setdefault(relative, metadata) != metadata
                or (item is not None and not reuse and
                    (len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]))):
            raise guard.GuardError("producer 原件文件实例、权限或字节漂移")
        if nonce is not None:
            self._byte_records[relative] = (nonce, raw, byte_identity, dict(item or {}))
        elif nonce is None:
            self._byte_records.clear()
        return raw, current

    def binding(self, path: Path) -> dict[str, Any]:
        """固定 producer 的取件接缝，同段绑定和 add 共用已核原字节。"""
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError as error:
            raise guard.GuardError("producer 绑定原件越出 data_root") from error
        guard.managed_path(self.root, relative)
        previous = self.bindings.get(relative)
        raw, _metadata = self._read_managed(relative, previous)
        item = previous or {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        self.bindings.setdefault(relative, dict(item))
        # 新坐标首次已经实读；同段随后 add 只复核绑定及完整元数据。
        if previous is None:
            from tools.official_client_control.qualification_material_import import _material_successor_byte_nonce
            nonce = _material_successor_byte_nonce(self)
            if nonce is not None:
                cached = self._byte_records[relative]
                self._byte_records[relative] = (nonce, raw, cached[2], dict(item))
        return dict(item)

    def get(self, path: str, default: Any = None) -> bytes:
        return self[path]

    def __contains__(self, path: object) -> bool:
        """成员选择只查本次显式绑定；取得字节仍由严格读取入口逐件验真。"""
        return path in self.bindings

    def __iter__(self):
        return iter(self.bindings)

    def __len__(self):
        return len(self.bindings)

    def revalidate(self) -> None:
        # 明确尾验逐件强制打开，不能承接中间 metadata 命中的原字节。
        for relative in sorted(self.bindings):
            self.read_with_metadata(relative, force_read=True)


_COMPLETED_ADMISSION_TOKEN = object()


@dataclass(frozen=True)
class VerifiedCompletedDeliveryAdmission:
    """本次当前产品实测的内存准入；报告 JSON 和调用方旗标不授准入权。"""

    environment: Mapping[str, Any]
    rollback: Mapping[str, Any]
    gates: tuple[Mapping[str, Any], ...]
    report_binding: Mapping[str, Any]
    archive_bindings: tuple[Mapping[str, Any], ...]
    successor_ref: Mapping[str, Any]
    plan_ref: Mapping[str, Any]
    token: object = field(repr=False)
    _tail: Any = field(repr=False, compare=False)
    _originals: Any = field(repr=False, compare=False)
    _configuration_sha256: str = field(repr=False)
    _data_root: Path = field(repr=False)

    def revalidate(self) -> None:
        if self.token is not _COMPLETED_ADMISSION_TOKEN:
            raise guard.GuardError("完成后准入不是固定当前产品的内存结果")
        self._tail()

    def compose_source(self, source: Any) -> Any:
        """仅本次实测 token 可以将精确当前报告闭集接入既有来源。"""
        from tools.official_client_control.delivery_execution import VerifiedSource
        if (self.token is not _COMPLETED_ADMISSION_TOKEN or not isinstance(source, VerifiedSource)
                or source.configuration_sha256 != self._configuration_sha256
                or source.trusted_data_root != self._data_root
                or getattr(source.gate_projection, "successor", None) is None
                or source.gate_projection.successor.reference != self.successor_ref
                or source.gate_projection.successor.facts["plan_ref"] != self.plan_ref):
            raise guard.GuardError("当前报告来源与同一 Plan、配置、数据根或材料后继不一致")
        self.revalidate()
        joined = _JoinedCompletedOriginals(source.bytes_by_path, self._originals)
        from tools.official_client_control.qualification_material_import import _register_material_successor_joined
        _register_material_successor_joined(self, joined)
        return replace(source, bytes_by_path=joined)


class _JoinedCompletedOriginals(Mapping[str, bytes]):
    """只覆盖真实准入对象的精确报告原件，未知路径仍委托原来源拒绝。"""

    def __init__(self, producer: Mapping[str, bytes], current: Mapping[str, bytes]):
        self.producer, self.current = producer, current
        for path in set(producer) & set(current):
            if producer[path] != current[path]:
                raise guard.GuardError("完成后报告与原来源对同一路径引用不同字节")

    def __getitem__(self, path: str) -> bytes:
        return self.current[path] if path in self.current else self.producer[path]

    def get(self, path: str, default: Any = None) -> bytes:
        return self[path]

    def __iter__(self):
        return iter(sorted(set(self.producer) | set(self.current)))

    def __len__(self):
        return len(set(self.producer) | set(self.current))


def _completed_live_signature(value: Mapping[str, Any]) -> str:
    """真实当前测量的稳定判据不含每次新建的原件坐标。"""
    return canonical_sha256({"environment": value["environment"], "rollback": value["rollback"],
        "actual": {key: {field: item for field, item in value["actual"][key].items() if field != "direct_binding"} for key in ("image", "profile")},
        "gates": [{"id": item["id"], "result": item["result"], "source": item["facts"]["source"]} for item in value["gates"]]})


def _completed_measurement_paths(value: Mapping[str, Any], relative_directory: str) -> list[str]:
    """固定测量的绑定闭集仅属于自己的输出目录；报告不能额外授予读件坐标。"""
    guard.exact(dict(value), {"environment", "rollback", "actual", "gates", "bindings"}, "完成后测量原件")
    bindings = sorted(guard.all_bindings(value), key=lambda item: item["path"])
    if value["bindings"] != bindings:
        raise guard.GuardError("完成后测量的显式绑定清单不是完整精确闭集")
    for gate_value in value["gates"]:
        guard.exact(gate_value, {"id", "result", "facts"}, "完成后九门禁")
        facts = guard.exact(gate_value["facts"], {"source", "bindings"}, "完成后门禁事实")
        if sorted(guard.all_bindings(facts["bindings"]), key=lambda item: item["path"]) != bindings:
            raise guard.GuardError("完成后门禁事实不是同次测量的精确绑定闭集")
    prefix = relative_directory + "/"
    if not bindings or any(not item["path"].startswith(prefix) for item in bindings):
        raise guard.GuardError("完成后测量绑定越出自己的固定输出目录")
    return [item["path"][len(prefix):] for item in bindings]


CONFIGURATION_SCHEMA = "claude-upgrade-arm64-executor-configuration/v1"
MATERIAL_CONFIGURATION_SCHEMA = "claude-upgrade-arm64-executor-configuration/v2"
EXECUTOR_VERSION = "claude-upgrade-arm64-delivery-executor/v1"
GUARD_IDENTITY_SCHEMA = "claude-upgrade-delivery-guard-identity/v1"
AUTHORITY = "claude-arm64-recovery-guard"
CONFIGURATION_FIELDS = frozenset({"schema_version", "client", "host_id", "instance_id", "isolation", "source_scope", "paths", "runtime", "candidate", "rollback", "probes", "guard", "limits"})
CONTEXT_FIELDS = frozenset({"campaign_id", "candidate_id", "delivery_id", "segment", "run_nonce", "data_root", "configuration_ref"})
GUARD_IDENTITY_FIELDS = frozenset({"contract_version", "authority", "source_scope", "host_id", "instance_id", "service_name", "service_instance_id", "boot_id", "pid", "process_start", "process_group_id", "independent_service", "service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"})
FREEZE_FIELDS = frozenset({"campaign_id", "candidate_id", "delivery_id", "candidate_image_digest", "architecture", "runtime_profile_sha256", "observation_window_seconds", "delivery_gates"})
DELIVERY_GATES = ("compose-diff", "dependency-connectivity", "entry-routing", "evidence-inventory", "health", "image-identity", "observation-window", "rollback-availability", "secret-scan")
EVIDENCE_TYPES = ("configuration", "stable_projection", "freeze", "rollback_state", "zero_model_probes", "deployment_configuration")
LOCAL_SOURCE_METADATA_FIELDS = frozenset({"content_sha256", "deployment_sha256", "configuration_sha256", "service_instance_id", "trusted_data_root", "guard_identity", "profile_configuration_binding", "guard_continuity", "profile_producer_materials", "profile_proof_bindings", "originals_schema"})
DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


def _integer(value: Any, label: str, minimum: int = 1, maximum: int = 86400) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise guard.GuardError(f"{label} 范围非法")
    return value


def _binding_shape(value: Any, label: str) -> dict[str, Any]:
    item = guard.exact(value, {"path", "sha256", "bytes"}, label)
    guard.relative_path(item["path"], label)
    if not isinstance(item["sha256"], str) or not guard.SHA256.fullmatch(item["sha256"]) or item["sha256"] == "0" * 64:
        raise guard.GuardError(f"{label} 摘要非法")
    _integer(item["bytes"], label + ".bytes", minimum=0, maximum=1024 * 1024 * 1024)
    return item


def _ref_shape(value: Any, category: str, kind: str, label: str) -> dict[str, Any]:
    key = category + "_kind"
    item = guard.exact(value, {key, "sha256"}, label)
    if item[key] != kind or not isinstance(item["sha256"], str) or not guard.SHA256.fullmatch(item["sha256"]) or item["sha256"] == "0" * 64:
        raise guard.GuardError(f"{label} 来源引用非法")
    return item


def _qualification_shape(value: Any, label: str) -> dict[str, Any]:
    from tools.official_client_control.contracts import validate_fact_ref, validate_receipt_ref
    item = guard.exact(value, {"store_relative", "external_root_relative", "campaign_id", "candidate_id", "candidate_ref", "build_receipt_ref", "validation_ref", "acceptance_ref", "sealed_result_binding"}, label)
    for name in ("store_relative", "external_root_relative"):
        guard.relative_path(item[name], label + "." + name)
    for name in ("campaign_id", "candidate_id"):
        if not isinstance(item[name], str) or not guard.SAFE_ID.fullmatch(item[name]):
            raise guard.GuardError(f"{label}.{name} 非法")
    for name in ("candidate_ref", "validation_ref", "acceptance_ref"):
        try:
            fact = validate_fact_ref(item[name], label + "." + name)
        except Exception as error:
            raise guard.GuardError(f"{label}.{name} 不符合标准事实引用") from error
        if fact["campaign_id"] != item["campaign_id"] or fact["dimension"] != "validation":
            raise guard.GuardError("资格事实必须属于冻结Campaign验收维度")
    try:
        receipt = validate_receipt_ref(item["build_receipt_ref"], label + ".build_receipt_ref")
    except Exception as error:
        raise guard.GuardError("构建收据不符合标准引用") from error
    if receipt["receipt_kind"] != "candidate_build":
        raise guard.GuardError("资格构建收据种类不符")
    _binding_shape(item["sealed_result_binding"], label + ".sealed_result_binding")
    return item


def validate_configuration(configuration: Any) -> dict[str, Any]:
    """仅纯合同校验；不读取文件、不查询主机、不改变现场。"""
    material_contract = isinstance(configuration, dict) and configuration.get("schema_version") == MATERIAL_CONFIGURATION_SCHEMA
    value = guard.exact(configuration, CONFIGURATION_FIELDS | ({"qualification_materials_binding"} if material_contract else set()), "执行器配置")
    if value["schema_version"] not in {CONFIGURATION_SCHEMA, MATERIAL_CONFIGURATION_SCHEMA} or value["client"] != "claude" or value["host_id"] != "ARM64" or value["isolation"] != "isolated_candidate" or value["source_scope"] not in {"formal", "regression_fixture"}:
        raise guard.GuardError("执行器仅允许 Claude ARM64 隔离候选配置")
    if material_contract:
        _binding_shape(value["qualification_materials_binding"], "资格门禁材料导入")
    if not isinstance(value["instance_id"], str) or not guard.SAFE_ID.fullmatch(value["instance_id"]) or value["instance_id"].lower().startswith(("production", "dmit", "vircs")):
        raise guard.GuardError("隔离实例 ID 非法")
    paths = guard.exact(value["paths"], {"guard_root_relative", "active_profile_relative", "compose_binding"}, "受管路径")
    guard.relative_path(paths["guard_root_relative"])
    guard.relative_path(paths["active_profile_relative"])
    _binding_shape(paths["compose_binding"], "Compose绑定")
    runtime = guard.exact(value["runtime"], {"docker_binding", "daemon_socket", "compose_project", "application_service", "dependency_services", "profile_container_path", "expected_labels"}, "运行时")
    docker = guard.exact(runtime["docker_binding"], {"absolute_path", "sha256", "bytes"}, "Docker可执行文件")
    if not isinstance(docker["absolute_path"], str) or not Path(docker["absolute_path"]).is_absolute() or str(PurePosixPath(docker["absolute_path"])) != docker["absolute_path"] or ".." in PurePosixPath(docker["absolute_path"]).parts:
        raise guard.GuardError("Docker必须使用规范绝对路径")
    if not isinstance(docker["sha256"], str) or not guard.SHA256.fullmatch(docker["sha256"]) or docker["sha256"] == "0" * 64:
        raise guard.GuardError("Docker可执行文件摘要非法")
    _integer(docker["bytes"], "Docker长度", maximum=1024 * 1024 * 1024)
    if not isinstance(runtime["daemon_socket"], str) or runtime["daemon_socket"] != "/var/run/docker.sock":
        raise guard.GuardError("Docker只允许冻结的本机Unix daemon，不接受DOCKER_HOST")
    for name in ("compose_project", "application_service"):
        if not isinstance(runtime[name], str) or not SERVICE_NAME.fullmatch(runtime[name]) or runtime[name].lower().startswith(("production", "dmit", "vircs")):
            raise guard.GuardError(f"{name} 非法")
    dependencies = runtime["dependency_services"]
    if not isinstance(dependencies, list) or not dependencies or dependencies != sorted(set(dependencies)) or any(not isinstance(item, str) or not SERVICE_NAME.fullmatch(item) for item in dependencies) or runtime["application_service"] in dependencies:
        raise guard.GuardError("依赖服务必须为冻结、唯一且排序的闭集")
    container_path = runtime["profile_container_path"]
    if not isinstance(container_path, str) or not container_path.startswith("/") or str(PurePosixPath(container_path)) != container_path or ".." in PurePosixPath(container_path).parts or not container_path.endswith(".json"):
        raise guard.GuardError("容器Profile路径非法")
    labels = runtime["expected_labels"]
    if not isinstance(labels, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in labels.items()) or labels.get("sub2api.delivery.isolation") != "isolated_candidate" or labels.get("sub2api.delivery.instance") != value["instance_id"]:
        raise guard.GuardError("应用服务缺固定候选隔离标签")
    for name in ("candidate", "rollback"):
        material = guard.exact(value[name], {"image_digest", "runtime_profile_sha256", "profile_binding", "override_binding", "qualification"}, name)
        if not isinstance(material["image_digest"], str) or not guard.IMAGE.fullmatch(material["image_digest"]) or material["image_digest"] == "sha256:" + "0" * 64:
            raise guard.GuardError(f"{name} 镜像digest非法")
        for field in ("profile_binding", "override_binding"):
            _binding_shape(material[field], name + "." + field)
        if material["runtime_profile_sha256"] != material["profile_binding"]["sha256"]:
            raise guard.GuardError(f"{name} 必须绑定真实Profile原件，不能用operational摘要占位")
        _qualification_shape(material["qualification"], name + ".qualification")
        if material["profile_binding"]["path"] == paths["active_profile_relative"]:
            raise guard.GuardError("允许变更的active Profile不能同时充当冻结来源")
    candidate, rollback = value["candidate"], value["rollback"]
    if candidate["image_digest"] == rollback["image_digest"] or candidate["qualification"]["candidate_id"] == rollback["qualification"]["candidate_id"]:
        raise guard.GuardError("回退必须是独立基准Candidate与不同真实镜像")
    for name in ("store_relative", "external_root_relative"):
        if candidate["qualification"][name] != rollback["qualification"][name]:
            raise guard.GuardError("本轮只接受同一受管Store的完整基准来源图")
    probes = guard.exact(value["probes"], {"base_url", "credential_binding", "expected_models", "group_id", "account_ids", "database", "redis", "timeout_seconds", "max_requests"}, "零模型探针")
    url = urlsplit(probes["base_url"] if isinstance(probes["base_url"], str) else "")
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1"} or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
        raise guard.GuardError("基础探针只连接冻结的宿主回环候选入口")
    try:
        _integer(url.port, "入口端口", maximum=65535)
    except ValueError as error:
        raise guard.GuardError("入口端口非法") from error
    _binding_shape(probes["credential_binding"], "候选专用Key绑定")
    models = probes["expected_models"]
    if not isinstance(models, list) or not models or any(not isinstance(item, str) or not MODEL_NAME.fullmatch(item) for item in models) or models != sorted(set(models)):
        raise guard.GuardError("期望模型须来自冻结真实数据，不允许空列表或重复")
    _integer(probes["group_id"], "候选分组ID", maximum=2**63 - 1)
    accounts = probes["account_ids"]
    if not isinstance(accounts, list) or not accounts or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in accounts) or accounts != sorted(set(accounts)):
        raise guard.GuardError("候选账号闭集非法")
    database = guard.exact(probes["database"], {"service", "user", "name", "api_key_id"}, "独立数据库")
    if database["service"] not in dependencies or any(not isinstance(database[item], str) or not DB_NAME.fullmatch(database[item]) for item in ("user", "name")):
        raise guard.GuardError("数据库服务或只读连接坐标非法")
    _integer(database["api_key_id"], "专用Key ID", maximum=2**63 - 1)
    redis = guard.exact(probes["redis"], {"service"}, "独立Redis")
    if redis["service"] not in dependencies or redis["service"] == database["service"]:
        raise guard.GuardError("独立Redis与数据库服务不闭合")
    _integer(probes["timeout_seconds"], "探针时限", maximum=120)
    _integer(probes["max_requests"], "最大HTTP探针数", minimum=3, maximum=3000)
    recovery = guard.exact(value["guard"], {"service_name", "service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding", "socket_relative", "channel_key_binding"}, "独立守卫")
    if not isinstance(recovery["service_name"], str) or not re.fullmatch(r"claude-delivery-[A-Za-z0-9_-]{1,80}\.service", recovery["service_name"]):
        raise guard.GuardError("守卫只接受Claude候选专属service名")
    for name in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding", "channel_key_binding"):
        _binding_shape(recovery[name], "guard." + name)
    guard.relative_path(recovery["socket_relative"])
    if not recovery["socket_relative"].startswith(paths["guard_root_relative"] + "/"):
        raise guard.GuardError("守卫socket必须在冻结私有guard根")
    limits = guard.exact(value["limits"], {"lease_seconds", "command_timeout_seconds", "stage_timeout_seconds", "observation_window_seconds", "poll_seconds"}, "守卫时限")
    for name in limits:
        _integer(limits[name], name, maximum=86400)
    if limits["poll_seconds"] > limits["lease_seconds"] or limits["command_timeout_seconds"] >= limits["stage_timeout_seconds"] or limits["observation_window_seconds"] + 3 * probes["timeout_seconds"] >= limits["stage_timeout_seconds"]:
        raise guard.GuardError("阶段时限不足以覆盖冻结观察窗与有界探针")
    return copy.deepcopy(value)


def validate_execution_context(context: Any, configuration: dict[str, Any]) -> dict[str, Any]:
    value = guard.exact(context, CONTEXT_FIELDS, "执行上下文")
    for name in ("campaign_id", "candidate_id", "delivery_id", "segment", "run_nonce"):
        if not isinstance(value[name], str) or not guard.SAFE_ID.fullmatch(value[name]):
            raise guard.GuardError(f"执行上下文.{name}非法")
    root = value["data_root"]
    if not isinstance(root, str) or not Path(root).is_absolute() or str(PurePosixPath(root)) != root or ".." in PurePosixPath(root).parts or any(part.lower() in {"production", "release", "releases", "dmit", "vircs"} for part in PurePosixPath(root).parts):
        raise guard.GuardError("data_root必须是显式隔离候选绝对根")
    _binding_shape(value["configuration_ref"], "原始配置绑定")
    if value["candidate_id"] != configuration["candidate"]["qualification"]["candidate_id"] or value["campaign_id"] != configuration["candidate"]["qualification"]["campaign_id"]:
        raise guard.GuardError("执行上下文与当前Candidate资格图不符")
    return copy.deepcopy(value)


def stable_environment_projection(compose: Any, inspections: Any, application_service: str) -> dict[str, Any]:
    """稳定投影只消去容器实例坐标和唯一应用阶段身份。

    Docker的实例ID、PID、时间、sandbox/endpoint实例坐标属于容器实例；
    IP、网络ID、依赖镜像、挂载、卷、环境、命令与安全设置保留。
    """
    if not isinstance(compose, dict) or not isinstance(inspections, dict) or application_service not in inspections:
        raise guard.GuardError("稳定投影缺完整Compose或应用实测")
    configuration = copy.deepcopy(compose)
    services = configuration.get("services")
    if not isinstance(services, dict) or set(services) != set(inspections):
        raise guard.GuardError("稳定投影服务闭集不符")
    services[application_service].pop("image", None)
    services[application_service].get("labels", {}).pop("sub2api.delivery.stage", None)
    containers = {}
    for service, document in inspections.items():
        if not isinstance(document, dict):
            raise guard.GuardError("容器inspect必须为对象")
        item = copy.deepcopy(document)
        container_id = item.pop("Id", "")
        for key in ("Created", "RestartCount", "ResolvConfPath", "HostnamePath", "HostsPath", "LogPath"):
            item.pop(key, None)
        state = item.get("State", {})
        for key in ("Pid", "StartedAt", "FinishedAt", "Health"):
            state.pop(key, None)
        network = item.get("NetworkSettings", {})
        for key in ("SandboxID", "SandboxKey"):
            network.pop(key, None)
        for endpoint in network.get("Networks", {}).values():
            endpoint.pop("EndpointID", None)
            if isinstance(endpoint.get("Aliases"), list):
                endpoint["Aliases"] = [alias for alias in endpoint["Aliases"] if alias not in {container_id, str(container_id)[:12]}]
            if isinstance(endpoint.get("DNSNames"), list):
                endpoint["DNSNames"] = [name for name in endpoint["DNSNames"] if name not in {container_id, str(container_id)[:12]}]
        config = item.get("Config", {})
        if config.get("Hostname") in {container_id, str(container_id)[:12]}:
            config["Hostname"] = "<container-instance>"
        if service == application_service:
            item.pop("Image", None)
            item.pop("GraphDriver", None)
            config.pop("Image", None)
            config.get("Labels", {}).pop("sub2api.delivery.stage", None)
        containers[service] = item
    return {"schema_version": "claude-upgrade-stable-environment-projection/v1", "application_service": application_service, "compose": configuration, "containers": containers}


def _external_executable(specification: dict[str, Any]) -> Path:
    path = Path(specification["absolute_path"])
    if path.is_symlink() or not path.is_file():
        raise guard.GuardError("冻结Docker可执行文件不存在或为符号链接")
    raw = path.read_bytes()
    info = path.stat()
    if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022 or not os.access(path, os.X_OK) or len(raw) != specification["bytes"] or hashlib.sha256(raw).hexdigest() != specification["sha256"]:
        raise guard.GuardError("冻结Docker可执行文件字节、所有者或权限漂移")
    return path


class _QualificationStoreView:
    """单次资格重放的已验证只读视图，不引入交付分支或跨操作缓存。

    每个 Campaign 首次读取仍由原 Store 完整校验事实链；本次重放内只索引
    已验证的记录，返回独立副本。成功前再次读验完整原链及 Campaign 原字节，
    任一变化都拒绝，不能用操作开始时的旧图签发通过证明。
    """

    def __init__(self, store: Any):
        if store.read_only is not True:
            raise guard.GuardError("验收资格视图必须基于只读Store")
        self._store = store
        self.root = store.root
        self.read_only = True
        self._campaign_snapshots: dict[str, tuple[bytes, dict[str, Any]]] = {}
        self._fact_snapshots: dict[str, tuple[list[dict[str, Any]], bytes]] = {}
        self._fact_index: dict[tuple[str, str, int], dict[str, Any]] = {}

    def load_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self._store.load_campaign(campaign_id)
        observed = (canonical_json_bytes(campaign),
                    self._file_source(self.root / "campaigns" / f"{campaign_id}.json"))
        previous = self._campaign_snapshots.setdefault(campaign_id, observed)
        if observed != previous:
            raise guard.GuardError("资格重放期间 Campaign 发生变化，必须重新完整核验")
        return campaign

    def _file_source(self, path: Path) -> dict[str, Any]:
        """实读 SHA 并记录原件坐标，不用 stat 命中代替字节校验。"""

        binding = guard.binding(self.root, path)
        information = path.lstat()
        if not stat.S_ISREG(information.st_mode):
            raise guard.GuardError("资格重放原件不是普通文件")
        return {"binding": binding, "metadata": {
            "device": information.st_dev, "inode": information.st_ino,
            "mode": stat.S_IMODE(information.st_mode), "uid": information.st_uid,
            "gid": information.st_gid, "mtime_ns": information.st_mtime_ns,
            "ctime_ns": information.st_ctime_ns,
        }}

    def _fact_signature(self, campaign_id: str, facts: list[dict[str, Any]]) -> bytes:
        """原链校验后的每个事实再绑定实际字节及路径元数据。"""

        sources = []
        for fact in facts:
            path = (self.root / "facts" / campaign_id / fact["dimension"]
                    / f"{fact['sequence']:08d}-{fact['_sha256']}.json")
            source = self._file_source(path)
            if source["binding"]["sha256"] != fact["_sha256"]:
                raise guard.GuardError("资格重放期间事实原字节发生变化")
            sources.append(source)
        return canonical_json_bytes({"facts": facts, "sources": sources})

    def _fact_snapshot(self, campaign_id: str) -> list[dict[str, Any]]:
        """完整原链只读验一次，操作结束再复核；原记录不交给调用方修改。"""

        if campaign_id not in self._fact_snapshots:
            self.load_campaign(campaign_id)
            facts = self._store.list_facts(campaign_id)
            self.load_campaign(campaign_id)
            self._fact_snapshots[campaign_id] = (facts, self._fact_signature(campaign_id, facts))
            for fact in facts:
                self._fact_index[(campaign_id, fact["dimension"], fact["sequence"])] = fact
        return self._fact_snapshots[campaign_id][0]

    def list_facts(self, campaign_id: str, dimension: str | None = None) -> list[dict[str, Any]]:
        if dimension == "deployment":
            return []
        if dimension is not None and dimension not in DIMENSIONS:
            # 非法维度继续走原拒绝，不能因索引过滤而静默返回空集。
            return self._store.list_facts(campaign_id, dimension)
        facts = self._fact_snapshot(campaign_id)
        return copy.deepcopy([fact for fact in facts if fact["dimension"] != "deployment"
                              and (dimension is None or fact["dimension"] == dimension)])

    def load_fact(self, reference: dict[str, Any]) -> dict[str, Any]:
        if isinstance(reference, dict) and reference.get("dimension") == "deployment":
            raise guard.GuardError("验收子图不接受交付或生产激活事实")
        parsed = validate_fact_ref(reference)
        self._fact_snapshot(parsed["campaign_id"])
        fact = self._fact_index.get((parsed["campaign_id"], parsed["dimension"], parsed["sequence"]))
        if fact is None:
            raise ControlError(f"事实引用不存在：{parsed}")
        if fact["_sha256"] != parsed["sha256"]:
            raise ControlError("事实引用摘要不匹配")
        return copy.deepcopy(fact)

    def verify_fact_snapshots(self) -> None:
        """成功前重做原链与 Campaign 校验，追加或任何漂移都不能承接旧索引。"""

        for campaign_id in sorted(self._campaign_snapshots):
            self.load_campaign(campaign_id)
            if campaign_id in self._fact_snapshots:
                observed = self._store.list_facts(campaign_id)
                if self._fact_signature(campaign_id, observed) != self._fact_snapshots[campaign_id][1]:
                    raise guard.GuardError("资格重放期间事实链发生变化，必须重新完整核验")
                self.load_campaign(campaign_id)

    def load_object(self, reference: dict[str, Any]) -> dict[str, Any]:
        if str(reference.get("object_kind", "")).startswith("candidate_delivery"):
            raise guard.GuardError("验收子图不接受交付对象")
        document = self._store.load_object(reference)
        if document.get("payload", {}).get("schema_version") == "official-client-operational-evidence/v2":
            raise guard.GuardError("验收子图不接受真实交付执行证据")
        return document

    def object_bytes(self, reference: dict[str, Any]) -> bytes:
        self.load_object(reference)
        return self._store.object_bytes(reference)

    def list_object_refs(self, kind: str) -> list[dict[str, Any]]:
        if kind.startswith("candidate_delivery"):
            return []
        references = []
        for reference in self._store.list_object_refs(kind):
            document = self._store.load_object(reference)
            if document.get("payload", {}).get("schema_version") != "official-client-operational-evidence/v2":
                references.append(reference)
        return references

    def load_receipt(self, reference: dict[str, Any]) -> dict[str, Any]:
        if reference.get("receipt_kind") == "candidate_delivery":
            raise guard.GuardError("验收子图不接受交付收据")
        return self._store.load_receipt(reference)

    def list_receipt_refs(self, kind: str) -> list[dict[str, Any]]:
        return [] if kind == "candidate_delivery" else self._store.list_receipt_refs(kind)

    def fact_ref(self, fact: dict[str, Any]) -> dict[str, Any]:
        return self._store.fact_ref(fact)

    def _verify_external_binding(self, external_root: Path, binding: dict[str, Any]) -> None:
        """每次委托原 Store 实读外部原件，不把事实索引权限扩展到外部缓存。"""
        self._store._verify_external_binding(external_root, binding)


def _qualification_external_binding(store: Any, external_root: Path, data_root: Path,
                                    item: Mapping[str, Any]) -> dict[str, Any]:
    """保留原 Store 实读，验证后只转换坐标，避免另一次绑定实读／哈希。"""
    from tools.official_client_control.canonical import validate_external_binding

    parsed = validate_external_binding(dict(item), "qualification.external_binding")
    path = guard.managed_path(external_root, parsed["path"])
    try:
        relative = path.relative_to(data_root).as_posix()
    except ValueError as error:
        raise guard.GuardError("资格外部原件越出本次受管数据根") from error
    guard.managed_path(data_root, relative)
    if any(parent.is_symlink() for root in (external_root, data_root) for parent in root.parents):
        raise guard.GuardError("资格外部原件的受管根祖先含符号链接")
    try:
        before = path.lstat()
    except OSError as error:
        raise guard.GuardError("资格外部原件不可读") from error
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
        raise guard.GuardError("资格外部原件不是当前所有者的普通文件")
    # 字节、长度、摘要及外部根仍由原 Store 读者逐次核验，不承接上次
    # 结果，也不将来源 Mapping 看作不变的缓存。下面只省去 guard.binding
    # 对同一原件的额外实读；前后路径状态必须仍指向同一个文件实例。
    store._verify_external_binding(external_root, parsed)
    guard.managed_path(external_root, parsed["path"])
    guard.managed_path(data_root, relative)
    if any(parent.is_symlink() for root in (external_root, data_root) for parent in root.parents):
        raise guard.GuardError("资格外部原件实读期间受管根祖先漂移")
    try:
        after = path.lstat()
    except OSError as error:
        raise guard.GuardError("资格外部原件实读后路径失效") from error
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns", "st_ctime_ns")
    if tuple(getattr(before, field) for field in fields) != tuple(getattr(after, field) for field in fields):
        raise guard.GuardError("资格外部原件实读期间字节、路径、所有者或权限漂移")
    return {**parsed, "path": relative}


def replay_qualification_graph(store: Any, qualification: dict[str, Any], external_root: Path, data_root: Path) -> dict[str, Any]:
    """形成期重放验收子图，不从guard反向重放本轮交付来源RPC。

    先核完整非deployment事实链，再展开资格根所有可达对象、收据和事实，
    逐条校验语义及外部字节。排除的是本轮物理交付分支，不排除验收证据。
    """
    from tools.official_client_control.contracts import iter_fact_refs, iter_object_refs, iter_receipt_refs
    from tools.official_client_control.gates import WorkflowGates
    from tools.official_client_control.receipts import replay_receipt
    from tools.official_client_control.qualification_bindings import QualificationBindingProjection, REPLAY_SCHEMA, validate_qualification_roots
    original_store = store
    store = _QualificationStoreView(store)
    qualification_root_refs = validate_qualification_roots(store, qualification)
    campaign = store.load_campaign(qualification["campaign_id"])
    facts = store.list_facts(qualification["campaign_id"])
    gates = WorkflowGates(store)
    gates.replay_campaign(campaign, facts)
    roots = [(store.load_fact(qualification[name]), None) for name in ("candidate_ref", "validation_ref", "acceptance_ref")]
    roots.append((store.load_receipt(qualification["build_receipt_ref"]), qualification["build_receipt_ref"]))
    seen: set[tuple[str, str]] = set()
    objects, receipts, references, external = [], [], [], {}
    projection = QualificationBindingProjection(store)
    pending = [(campaign, None), *((fact, None) for fact in facts), *roots]
    while pending:
        document, parent_ref = pending.pop()
        # v2 报告必须记录本身作为根或历史上下文读取的事实，不能只列字段里的反向引用。
        if "fact_kind" in document:
            reference = store.fact_ref(document)
            key = ("fact", reference["sha256"])
            if key not in seen:
                seen.add(key)
                references.append(reference)
        for item in projection.bindings(document, parent_ref):
            rebased = _qualification_external_binding(original_store, external_root, data_root, item)
            previous = external.get(item["path"])
            if previous is not None and previous != rebased:
                raise guard.GuardError("验收资格图的同一路径存在不同外部字节")
            projection.verify_external_mode(external_root, item)
            external[item["path"]] = rebased
        for reference in iter_object_refs(document):
            key = ("object", reference["sha256"])
            if key in seen:
                continue
            seen.add(key)
            gates.validate_object_graph(reference)
            pending.append((store.load_object(reference), reference))
            objects.append(reference)
        for reference in iter_receipt_refs(document):
            key = ("receipt", reference["sha256"])
            if key in seen:
                continue
            seen.add(key)
            replay_receipt(store, reference)
            pending.append((store.load_receipt(reference), reference))
            receipts.append(reference)
        for reference in iter_fact_refs(document):
            key = ("fact", reference["sha256"])
            if key in seen:
                continue
            seen.add(key)
            fact = store.load_fact(reference)
            if fact["dimension"] == "deployment":
                raise guard.GuardError("回退验收资格不得依赖本轮物理交付或生产激活事实")
            pending.append((fact, None))
            references.append(reference)
    store.verify_fact_snapshots()
    return {"schema_version": REPLAY_SCHEMA, "campaign_id": qualification["campaign_id"], "candidate_id": qualification["candidate_id"], "qualification_roots": qualification_root_refs, "external_bindings_base": "data_root", "binding_projection": projection.manifest(), "fact_refs": sorted(references, key=canonical_sha256), "object_refs": sorted(objects, key=canonical_sha256), "receipt_refs": sorted(receipts, key=canonical_sha256), "external_bindings": [external[key] for key in sorted(external)], "result": "pass"}


def guard_identity(configuration: dict[str, Any], context: dict[str, Any], *, formal: bool) -> dict[str, Any]:
    """从当前进程、实际service与已绑定安装字节取得身份，不接受自报pid。"""
    root = Path(context["data_root"])
    settings = configuration["guard"]
    for name in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding", "channel_key_binding"):
        guard.check_binding(root, settings[name])
    actual = guard.process_identity(os.getpid())
    if formal:
        if platform.system() != "Linux" or platform.machine().lower() not in {"arm64", "aarch64"} or configuration["source_scope"] != "formal":
            raise guard.GuardError("正式守卫必须实际运行在ARM64 Linux")
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != settings["executor_binding"]["sha256"] or hashlib.sha256(Path(guard.__file__).read_bytes()).hexdigest() != settings["guard_binding"]["sha256"]:
            raise guard.GuardError("实际已加载的守卫／执行器源码与部署绑定不符")
        service = settings["service_name"]
        command = "/usr/bin/systemctl" if Path("/usr/bin/systemctl").is_file() else "/bin/systemctl"
        result = subprocess.run([command, "show", service, "--property=MainPID,FragmentPath,KillMode,Restart"], check=True, capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        unit = Path(fields.get("FragmentPath", ""))
        if fields.get("MainPID") != str(os.getpid()) or not unit.is_absolute() or unit.is_symlink() or not unit.is_file() or hashlib.sha256(unit.read_bytes()).hexdigest() != settings["service_unit_binding"]["sha256"]:
            raise guard.GuardError("当前守卫不是冻结systemd独立服务的MainPID")
        if fields.get("KillMode") != "control-group" or fields.get("Restart") not in {"always", "on-failure"}:
            raise guard.GuardError("守卫服务必须终止整个旧子进程组并配置异常重启")
        cgroup = Path("/proc/self/cgroup").read_text()
        if not any(line.rsplit(":", 1)[-1].rstrip("/").endswith("/" + service) for line in cgroup.splitlines()):
            raise guard.GuardError("守卫不在独立service生命周期中")
        enabled = subprocess.run([command, "is-enabled", service], check=True, capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout.strip()
        if enabled != "enabled":
            raise guard.GuardError("守卫未配置宿主重启自动恢复")
        receipt = guard.read_bound_json(root, settings["deployment_receipt_binding"])
        machine = hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()
        expected = {"schema_version": "claude-upgrade-delivery-guard-deployment/v1", "host_id": "ARM64", "instance_id": configuration["instance_id"], "service_name": service, "machine_id_sha256": machine, "service_unit_sha256": settings["service_unit_binding"]["sha256"], "executor_sha256": settings["executor_binding"]["sha256"], "guard_sha256": settings["guard_binding"]["sha256"]}
        if any(receipt.get(key) != value for key, value in expected.items()) or not isinstance(receipt.get("restart_recovery_binding"), dict):
            raise guard.GuardError("缺完整受管安装／独立重启恢复部署收据")
        guard.check_binding(root, receipt["restart_recovery_binding"])
        from tools.official_client_capture.claude_upgrade_delivery_installation import validate_deployment_receipt
        validate_deployment_receipt(configuration)
    else:
        if configuration["source_scope"] != "regression_fixture":
            raise guard.GuardError("本机fixture身份不能声称formal")
    result = {"contract_version": GUARD_IDENTITY_SCHEMA, "authority": AUTHORITY, "source_scope": configuration["source_scope"], "host_id": "ARM64", "instance_id": configuration["instance_id"], "service_name": settings["service_name"], "service_instance_id": canonical_sha256({"configuration": canonical_sha256(configuration), "process": actual}), **actual, "independent_service": formal, **{key: settings[key] for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding")}}
    guard.exact(result, GUARD_IDENTITY_FIELDS, "guard身份")
    return result


class DockerComposeRuntime:
    """固定Docker命令、只读数据库和零模型HTTP；没有任意shell入口。"""

    def __init__(self, configuration: dict[str, Any], context: dict[str, Any]):
        self.configuration = validate_configuration(configuration)
        self.context = validate_execution_context(context, configuration)
        self.root = Path(context["data_root"])
        self.runtime = configuration["runtime"]
        self.probes = configuration["probes"]
        self.docker = _external_executable(self.runtime["docker_binding"])
        self._credential: bytes | None = None
        self._compose_cache: dict[str, Any] = {}
        self._profile_producer_identity: dict[str, Any] | None = None

    def bind_profile_producer(self, identity: dict[str, Any]) -> None:
        """仅固定 guard 构造时绑定真实生产实例；旧 proof 不按新实例改写。"""
        self._profile_producer_identity = copy.deepcopy(profile_observations._producer_identity(identity, self.configuration))

    def _profile_proof(self, directory: Path, phase: str, observed: dict[str, Any],
                       actual: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
        if self._profile_producer_identity is None:
            raise guard.GuardError("真实 Docker Profile 采集缺受管 producer 实例")
        if any(actual[key].get("state") != "known" for key in ("image", "profile")):
            return None
        roles = [name for name in ("candidate", "rollback")
                 if (actual["image"]["value"], actual["profile"]["value"]) ==
                 (self.configuration[name]["image_digest"], self.configuration[name]["runtime_profile_sha256"])]
        if len(roles) != 1:
            return None
        role = roles[0]
        source = self.configuration[role]["profile_binding"]
        if guard.read_bound_bytes(self.root, source) != guard.read_bound_bytes(self.root, observed):
            raise guard.GuardError("Profile 派生证明的源父与真实副本完整字节不一致")
        relative = directory.relative_to(self.root).as_posix()
        return {"schema_version": profile_observations.PROOF_SCHEMA,
                "configuration_binding": self.context["configuration_ref"], "configuration_sha256": canonical_sha256(self.configuration),
                "source_scope": self.configuration["source_scope"],
                **{key: self.context[key] for key in ("campaign_id", "candidate_id", "delivery_id")},
                "collection_phase": phase, "capture_coordinate": profile_observations.capture_coordinate(self.configuration, phase, relative),
                "role": role, "producer_identity": copy.deepcopy(self._profile_producer_identity),
                "source_profile_binding": source, "observed_profile_binding": observed, "observation": observation}

    def _command(self, arguments: list[str], *, checkpoint: Any = None, input_bytes: bytes | None = None) -> bytes:
        _external_executable(self.runtime["docker_binding"])
        prefix = [str(self.docker), "--host", "unix://" + self.runtime["daemon_socket"]]
        process = subprocess.Popen(prefix + arguments, shell=False, cwd=self.root, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        deadline = time.monotonic() + self.configuration["limits"]["command_timeout_seconds"]
        first = True
        try:
            while True:
                if checkpoint:
                    checkpoint()
                if time.monotonic() >= deadline:
                    raise guard.GuardError("固定Docker操作超时")
                try:
                    output, error = process.communicate(input=input_bytes if first else None, timeout=0.2)
                    if process.returncode != 0:
                        # 不将stderr或凭据回显进公共材料；调用坐标在固定原件中。
                        raise guard.GuardError(f"固定Docker操作失败，退出码{process.returncode}")
                    if len(output) > 32 * 1024 * 1024:
                        raise guard.GuardError("Docker原件超出冻结长度限制")
                    return output
                except subprocess.TimeoutExpired:
                    first = False
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            raise

    def _compose(self, material: str = "candidate") -> list[str]:
        base = guard.check_binding(self.root, self.configuration["paths"]["compose_binding"])
        override = guard.check_binding(self.root, self.configuration[material]["override_binding"])
        return ["compose", "--env-file", "/dev/null", "--project-name", self.runtime["compose_project"], "--file", str(base), "--file", str(override)]

    def _compose_document(self, material: str) -> dict[str, Any]:
        raw = self._command(self._compose(material) + ["config", "--format", "json"])
        value = parse_json_bytes(raw)
        if not isinstance(value, dict):
            raise guard.GuardError("Compose完整配置不是对象")
        services = value.get("services")
        expected = {self.runtime["application_service"], *self.runtime["dependency_services"]}
        if not isinstance(services, dict) or set(services) != expected:
            raise guard.GuardError("Compose服务闭集不符")
        app = services[self.runtime["application_service"]]
        if app.get("image", "").rsplit("@", 1)[-1] != self.configuration[material]["image_digest"]:
            raise guard.GuardError("Compose应用镜像没有冻结digest")
        if any(app.get("labels", {}).get(key) != item for key, item in self.runtime["expected_labels"].items()):
            raise guard.GuardError("Compose应用隔离标签不符")
        for name, service in services.items():
            if service.get("privileged") is True or service.get("network_mode") == "host" or service.get("pid") == "host" or service.get("ipc") == "host" or service.get("cap_add"):
                raise guard.GuardError("候选服务存在未允许的宿主权限")
            for mount in service.get("volumes", []):
                if not isinstance(mount, dict):
                    raise guard.GuardError("Compose挂载必须解析为完整对象")
                if mount.get("type") == "bind":
                    source = Path(mount.get("source", ""))
                    try:
                        relative = source.relative_to(self.root).as_posix()
                    except ValueError as error:
                        raise guard.GuardError("候选bind挂载越出隔离data_root") from error
                    guard.managed_path(self.root, relative)
                elif mount.get("type") != "volume":
                    raise guard.GuardError("未冻结挂载类型")
            if not service.get("networks") or any(not isinstance(network, dict) or not network.get("ipv4_address") for network in service["networks"].values()):
                raise guard.GuardError("所有候选服务必须冻结网络和固定IP")
        for volume in value.get("volumes", {}).values():
            if volume.get("external") or not str(volume.get("name", "")).startswith(self.runtime["compose_project"] + "_"):
                raise guard.GuardError("持久卷不是候选专属卷")
        for network in value.get("networks", {}).values():
            if network.get("external") or not str(network.get("name", "")).startswith(self.runtime["compose_project"] + "_"):
                raise guard.GuardError("网络不是候选专属网络")
        port = urlsplit(self.probes["base_url"]).port
        if not any(str(item.get("published", "")) == str(port) and item.get("host_ip") in {"127.0.0.1", "::1"} for item in app.get("ports", []) if isinstance(item, dict)):
            raise guard.GuardError("冻结HTTP入口没有映射到唯一应用容器")
        return value

    def check_qualification(self) -> dict[str, Any]:
        """只读重放同Store的两份资格图，禁止自报验收、旧Profile占位。"""
        from tools.official_client_capture.claude_gate_material_worker import gate_material_reader_scope
        # 生命周期覆盖首次导入和报告尾验。两次完整 subject 与用途判断仍
        # 重建，只允许同一活调用内承接已经逐份实核的纯 reader 结果。
        with gate_material_reader_scope():
            return self._check_qualification_in_scope()

    def _check_completed_qualification(self, source: Any) -> dict[str, Any]:
        """完成后 runtime 只消费本次真实来源，不用空 Mapping 冒充正式连续性。"""
        from tools.official_client_control.delivery_execution import VerifiedSource, _revalidate_guard_continuity
        from tools.official_client_capture.claude_gate_material_worker import gate_material_reader_scope
        if (not isinstance(source, VerifiedSource) or source.trusted_data_root != self.root
                or source.configuration_sha256 != canonical_sha256(self.configuration)
                or getattr(source.gate_projection, "successor", None) is None):
            raise guard.GuardError("完成后 runtime 资格缺同一当前真实材料来源")
        _revalidate_guard_continuity(source, required=True)
        with gate_material_reader_scope():
            return self._check_qualification_in_scope(completed_source=source)

    def _check_qualification_in_scope(self, *, completed_source: Any = None) -> dict[str, Any]:
        from tools.official_client_control.store import ControlStore
        from tools.official_client_control.receipts import replay_receipt
        from tools.official_client_control.qualification_bindings import DELIVERY_SCHEMA, MATERIAL_DELIVERY_SCHEMA, validate_delivery_report
        first = self.configuration["candidate"]["qualification"]
        store = ControlStore.open_read_only(guard.managed_path(self.root, first["store_relative"]))
        # 第二根在凭据、Compose 或任何现场探针之前只读验真；当前有效
        # 身份达到12时禁止写入/派发旧配置，历史消费另走形成期合同。
        from tools.official_client_control.qualification_material_import import requires_qualification_materials, projection_for_configuration
        materials = self.configuration.get("qualification_materials_binding")
        if requires_qualification_materials(store, store.load_campaign(first["campaign_id"])) and materials is None:
            raise guard.GuardError("当前第二根能力要求配置 v2 和真实导入证明，禁止降级")
        source = None
        if materials is not None:
            from tools.official_client_control.delivery_execution import VerifiedSource
            source = completed_source if completed_source is not None else VerifiedSource("0" * 64,
                self.configuration["guard"]["deployment_receipt_binding"]["sha256"], {},
                canonical_sha256(self.configuration), "qualification-readonly", self.root)
            projection = projection_for_configuration(store, self.configuration, source, purpose="current_delivery")
            source = replace(source, gate_projection=projection)
        reports = []
        for name in ("candidate", "rollback"):
            material = self.configuration[name]
            q = material["qualification"]
            candidate = store.load_fact(q["candidate_ref"])
            validation = store.load_fact(q["validation_ref"])
            acceptance = store.load_fact(q["acceptance_ref"])
            build = store.load_receipt(q["build_receipt_ref"])
            replay_receipt(store, q["build_receipt_ref"])
            reports.append(replay_qualification_graph(store, q, guard.managed_path(self.root, q["external_root_relative"]), self.root))
            if candidate["fact_kind"] != "candidate_frozen" or validation["fact_kind"] != "validation_completed" or acceptance["fact_kind"] != "acceptance_recorded":
                raise guard.GuardError("资格事实类型不符")
            if any(fact["campaign_id"] != q["campaign_id"] for fact in (candidate, validation, acceptance)):
                raise guard.GuardError("资格图跨Campaign裸引用")
            for fact in (candidate, validation, acceptance):
                if fact["payload"].get("candidate_id") != q["candidate_id"]:
                    raise guard.GuardError("资格图Candidate ID不一致")
            expected = {"candidate_ref": q["candidate_ref"], "candidate_build_receipt_ref": q["build_receipt_ref"], "acceptance_ref": q["acceptance_ref"]}
            if any(validation["payload"].get(key) != item for key, item in expected.items()) or validation["payload"].get("result") != "passed":
                raise guard.GuardError("资格图缺完整VC-5验收与封存链")
            if candidate["payload"].get("image_digest") != material["image_digest"] or build.get("image", {}).get("image_digest") != material["image_digest"] or build.get("profile_identity", {}).get("profile_digest") != material["runtime_profile_sha256"]:
                raise guard.GuardError("资格图镜像／真实Profile与配置不符")
            if acceptance["payload"].get("candidate_ref") != q["candidate_ref"] or acceptance["payload"].get("candidate_build_receipt_ref") != q["build_receipt_ref"]:
                raise guard.GuardError("验收来源没有绑定真实构建")
            sealed = guard.read_bound_json(self.root, q["sealed_result_binding"])
            sealed_store = sealed.get("store") if isinstance(sealed.get("store"), dict) else {}
            if sealed.get("schema_version") != "claude-upgrade-acceptance/v1" or sealed.get("candidate_id") != q["candidate_id"] or sealed_store.get("acceptance_ref") != q["acceptance_ref"] or sealed_store.get("validation_completed_ref") != q["validation_ref"] or sealed.get("production_state") != "not_activated":
                raise guard.GuardError("缺独立已封存的候选终态原件")
            guard.check_binding(self.root, material["profile_binding"])
            guard.check_binding(self.root, material["override_binding"])
        credential = guard.read_bound_bytes(self.root, self.probes["credential_binding"]).strip()
        if not credential or b"\n" in credential or b"\r" in credential or len(credential) > 256:
            raise guard.GuardError("候选专用Key原件非法")
        self._credential = credential
        candidate_compose = self._compose_document("candidate")
        rollback_compose = self._compose_document("rollback")
        raw_candidate, raw_rollback = copy.deepcopy(candidate_compose), copy.deepcopy(rollback_compose)
        for doc in (candidate_compose, rollback_compose):
            doc["services"][self.runtime["application_service"]].pop("image", None)
            doc["services"][self.runtime["application_service"]].get("labels", {}).pop("sub2api.delivery.stage", None)
        if candidate_compose != rollback_compose:
            raise guard.GuardError("两份Compose仅可改变唯一应用镜像／阶段标签")
        self._compose_cache = candidate_compose
        report = {"schema_version": DELIVERY_SCHEMA, "configuration_binding": self.context["configuration_ref"], "candidate": self.configuration["candidate"], "rollback": self.configuration["rollback"], "qualification_replays": reports, "candidate_compose": raw_candidate, "rollback_compose": raw_rollback, "normalized_compose": candidate_compose, "allowed_changes": ["application_image", "application_stage_label", "active_runtime_profile"], "result": "pass"}
        if materials is not None:
            report.update(schema_version=MATERIAL_DELIVERY_SCHEMA, qualification_materials_binding=materials)
        validate_delivery_report(report, store, current_execution=True, configuration=self.configuration, trusted_data_root=self.root, source=source)
        self._qualification_report = report
        return report

    def _inspect(self, directory: Path, checkpoint: Any = None, *, collection_phase: str | None = None) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
        guard.private_directory(directory)
        inspections, originals = {}, []
        for service in [self.runtime["application_service"], *self.runtime["dependency_services"]]:
            ids = self._command(self._compose() + ["ps", "--all", "--quiet", service], checkpoint=checkpoint).decode("ascii").split()
            if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", ids[0]):
                raise guard.GuardError("候选服务必须有唯一真实容器")
            raw = self._command(["container", "inspect", ids[0]], checkpoint=checkpoint)
            parsed = parse_json_bytes(raw)
            if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
                raise guard.GuardError("Docker inspect返回非法")
            labels = parsed[0].get("Config", {}).get("Labels", {})
            if labels.get("com.docker.compose.project") != self.runtime["compose_project"] or labels.get("com.docker.compose.service") != service:
                raise guard.GuardError("实际容器不属于冻结候选project／service")
            if service == self.runtime["application_service"] and any(labels.get(key) != item for key, item in self.runtime["expected_labels"].items()):
                raise guard.GuardError("实际应用缺候选隔离身份")
            path = directory / (service + "-inspect.json")
            guard.write_once(path, raw)
            originals.append(guard.binding(self.root, path))
            inspections[service] = parsed[0]
        app = inspections[self.runtime["application_service"]]
        container_id = app["Id"]
        config_image = app.get("Config", {}).get("Image", "")
        image_raw = self._command(["image", "inspect", app["Image"]], checkpoint=checkpoint)
        image = parse_json_bytes(image_raw)
        image_path = directory / "application-image-inspect.json"
        guard.write_once(image_path, image_raw)
        originals.append(guard.binding(self.root, image_path))
        # 平台只能来自本次真实镜像原件；缺字段或架构不符时不得以宿主名倒填。
        if not isinstance(image, list) or len(image) != 1 or not isinstance(image[0], dict) or image[0].get("Os") != "linux" or image[0].get("Architecture") not in {"arm64", "aarch64"}:
            raise guard.GuardError("真实镜像原件缺少linux ARM64平台，不能继续交付")
        architecture = "arm64" if image[0]["Architecture"] == "aarch64" else image[0]["Architecture"]
        image_platform = image[0]["Os"] + "/" + architecture
        digest = config_image.rsplit("@", 1)[-1]
        if not guard.IMAGE.fullmatch(digest) or not isinstance(image, list) or len(image) != 1 or image[0].get("Id") != app["Image"] or config_image not in image[0].get("RepoDigests", []):
            image_actual = {"state": "unknown", "reason": "容器镜像未取得一致的真实RepoDigest原件"}
        else:
            identity_path = directory / "image-identity.json"
            guard.write_json_once(identity_path, {"image_digest": digest, "container_inspect_binding": originals[0], "image_inspect_binding": guard.binding(self.root, image_path)})
            image_actual = {"state": "known", "value": digest, "direct_binding": guard.binding(self.root, identity_path)}
            originals.append(guard.binding(self.root, identity_path))
        tar_bytes = self._command(["container", "cp", container_id + ":" + self.runtime["profile_container_path"], "-"], checkpoint=checkpoint)
        profile_bytes = profile_observations.extract_profile_tar(tar_bytes, self.runtime["profile_container_path"])
        # 原始Profile也严格解析；其实际字节摘要不由目标倒填。
        parse_json_bytes(profile_bytes, "真实运行Profile")
        profile_path = directory / "runtime-profile.json"
        guard.write_once(profile_path, profile_bytes)
        profile_identity_path = directory / "profile-identity.json"
        profile_identity = {"runtime_profile_sha256": hashlib.sha256(profile_bytes).hexdigest(), "profile_binding": guard.binding(self.root, profile_path)}
        if collection_phase is not None:
            tar_path = directory / "runtime-profile.tar"
            guard.write_once(tar_path, tar_bytes)
            originals.append(guard.binding(self.root, tar_path))
            measured = {"image": image_actual, "profile": {"state": "known", "value": hashlib.sha256(profile_bytes).hexdigest()}}
            observation = {"kind": "container", "container_id": container_id,
                           "container_profile_path": self.runtime["profile_container_path"], "application_service": self.runtime["application_service"],
                           "container_inspect_binding": originals[0], "image_inspect_binding": guard.binding(self.root, image_path),
                           "profile_tar_binding": guard.binding(self.root, tar_path),
                           "image_digest": image_actual.get("value"), "profile_sha256": hashlib.sha256(profile_bytes).hexdigest()}
            proof = self._profile_proof(directory, collection_phase, guard.binding(self.root, profile_path), measured, observation)
            proof_binding = None
            if proof is not None:
                proof_path = directory / "runtime-profile-proof.json"
                guard.write_json_once(proof_path, proof)
                proof_binding = guard.binding(self.root, proof_path)
                originals.append(proof_binding)
            profile_identity.update(schema_version=profile_observations.RUNTIME_IDENTITY_SCHEMA, profile_observation_proof_binding=proof_binding)
        guard.write_json_once(profile_identity_path, profile_identity)
        actual = {"image": image_actual, "profile": {"state": "known", "value": hashlib.sha256(profile_bytes).hexdigest(), "direct_binding": guard.binding(self.root, profile_identity_path)}}
        originals.extend([guard.binding(self.root, profile_path), guard.binding(self.root, profile_identity_path)])
        if not self._compose_cache:
            self._compose_cache = self._compose_document("candidate")
            self._compose_cache["services"][self.runtime["application_service"]].pop("image", None)
            self._compose_cache["services"][self.runtime["application_service"]].get("labels", {}).pop("sub2api.delivery.stage", None)
        projection = stable_environment_projection(self._compose_cache, inspections, self.runtime["application_service"])
        projection_path = directory / "stable-projection.json"
        guard.write_json_once(projection_path, projection)
        originals.append(guard.binding(self.root, projection_path))
        environment = {"host_id": "ARM64", "architecture": image_platform, "compose_sha256": self.configuration["paths"]["compose_binding"]["sha256"], "environment_sha256": canonical_sha256(projection), "configuration_sha256": canonical_sha256(self.configuration), "network_sha256": canonical_sha256({name: item.get("NetworkSettings", {}).get("Networks", {}) for name, item in projection["containers"].items()})}
        return actual, environment, originals, container_id

    def before(self, directory: Path, expected_environment: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        actual, environment, originals, _ = self._inspect(directory / "before", collection_phase="before")
        if expected_environment is not None and environment != expected_environment:
            raise guard.GuardError("armed前实测环境与冻结计划漂移")
        allowed = {(self.configuration[name]["image_digest"], self.configuration[name]["runtime_profile_sha256"]) for name in ("candidate", "rollback")}
        if actual["image"].get("state") != "known" or actual["profile"].get("state") != "known" or (actual["image"].get("value"), actual["profile"].get("value")) not in allowed:
            raise guard.GuardError("armed前应用身份不属于两份已验证Candidate")
        active = guard.managed_path(self.root, self.configuration["paths"]["active_profile_relative"])
        path = directory / "before" / "host-active-profile.json"
        guard.write_once(path, guard.read_bytes(active))
        if hashlib.sha256(guard.read_bytes(path)).hexdigest() != actual["profile"]["value"]:
            raise guard.GuardError("armed前宿主Profile与容器实测不符")
        originals.append(guard.binding(self.root, path))
        container_identity_path = directory / "before" / "profile-identity.json"
        container_identity = guard.read_json(container_identity_path)
        proof = self._profile_proof(directory / "before", "before", guard.binding(self.root, path), actual,
                                    {"kind": "host", "active_profile_relative": self.configuration["paths"]["active_profile_relative"],
                                     "container_proof_binding": container_identity["profile_observation_proof_binding"],
                                     "profile_sha256": actual["profile"]["value"]})
        if proof is None or container_identity["profile_observation_proof_binding"] is None:
            raise guard.GuardError("armed前宿主Profile缺同次容器派生证明")
        proof_path = directory / "before" / "host-active-profile-proof.json"
        guard.write_json_once(proof_path, proof)
        host_identity_path = directory / "before" / "host-profile-identity.json"
        guard.write_json_once(host_identity_path, {"schema_version": profile_observations.HOST_IDENTITY_SCHEMA,
                              "active_profile_relative": self.configuration["paths"]["active_profile_relative"],
                              "profile_binding": guard.binding(self.root, path), "profile_observation_proof_binding": guard.binding(self.root, proof_path),
                              "container_profile_identity_binding": guard.binding(self.root, container_identity_path)})
        originals.extend([guard.binding(self.root, proof_path), guard.binding(self.root, host_identity_path)])
        return originals

    def failed_observation(self, fallback: dict[str, Any], directory: Path) -> dict[str, Any]:
        """只读取本次已取得原件，异常时不重新运行或倒填目标实测。"""
        during = directory / "during"
        if not during.exists():
            return fallback
        for name, filename, field in (("image", "image-identity.json", "image_digest"), ("profile", "profile-identity.json", "runtime_profile_sha256")):
            candidates = sorted(during.rglob(filename))
            if candidates:
                path = candidates[-1]
                raw = guard.read_json(path)
                fallback["actual"][name] = {"state": "known", "value": raw[field], "direct_binding": guard.binding(self.root, path)}
        projections = sorted(during.rglob("stable-projection.json"))
        if projections:
            projection = guard.read_json(projections[-1])
            fallback["environment"] = {"host_id": "ARM64", "architecture": "arm64", "compose_sha256": self.configuration["paths"]["compose_binding"]["sha256"], "environment_sha256": canonical_sha256(projection), "configuration_sha256": canonical_sha256(self.configuration), "network_sha256": canonical_sha256({name: item.get("NetworkSettings", {}).get("Networks", {}) for name, item in projection["containers"].items()})}
        request_records = []
        for path in sorted(during.rglob("http-*-request.json"), key=lambda item: int(item.name.split("-")[1])):
            request = guard.read_json(path)
            response_path = path.with_name(path.name.replace("-request.json", "-response.json"))
            request_records.append({"method": request["method"], "path": request["path"], "request_binding": guard.binding(self.root, path), "response_binding": guard.binding(self.root, response_path) if response_path.exists() else None})
        fallback["request_records"] = request_records
        fallback["during_bindings"] = [guard.binding(self.root, path) for path in sorted(during.rglob("*")) if path.is_file()]
        fallback["after_bindings"] = all_bindings_for_records(request_records)
        return fallback

    def _http(self, directory: Path, authenticated: bool, path: str, records: list[dict[str, Any]], checkpoint: Any = None) -> tuple[int, Any]:
        if path not in {"/health", "/v1/models"} or (authenticated and path != "/v1/models"):
            raise guard.GuardError("HTTP探针不在固定method/path闭集")
        if len(records) >= self.probes["max_requests"]:
            raise guard.GuardError("HTTP探针达到冻结次数上限")
        if checkpoint:
            checkpoint()
        url = urlsplit(self.probes["base_url"])
        number = len(records) + 1
        request_path = directory / f"http-{number:06d}-request.json"
        request = {"method": "GET", "path": path, "base_url": self.probes["base_url"], "credential": "candidate_private_ref" if authenticated else "none"}
        guard.write_json_once(request_path, request)
        record = {"method": "GET", "path": path, "request_binding": guard.binding(self.root, request_path), "response_binding": None}
        records.append(record)
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=self.probes["timeout_seconds"])
        try:
            headers = {"Accept": "application/json", "User-Agent": "Claude-Candidate-Delivery/1"}
            if authenticated:
                if self._credential is None:
                    raise guard.GuardError("候选专用凭据未通过准入")
                headers["Authorization"] = "Bearer " + self._credential.decode("ascii")
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024 or (self._credential and self._credential in content):
                raise guard.GuardError("HTTP响应过大或回显私有凭据")
            parsed = parse_json_bytes(content) if content else None
            response_path = directory / f"http-{number:06d}-response.json"
            # 只保存固定安全响应字段，不录Set-Cookie、Authorization或凭据明文。
            document = {"status": response.status, "body": parsed, "redirect_followed": False, "location_present": bool(response.getheader("Location"))}
            guard.write_json_once(response_path, document)
            record["response_binding"] = guard.binding(self.root, response_path)
            if 300 <= response.status < 400:
                raise guard.GuardError("候选入口返回redirect；禁止跟随")
            if checkpoint:
                checkpoint()
            return response.status, parsed
        finally:
            connection.close()

    def _database(self, directory: Path, checkpoint: Any = None) -> tuple[bool, list[dict[str, Any]]]:
        db = self.probes["database"]
        group_id, key_id = self.probes["group_id"], db["api_key_id"]
        # 只允许固定只读事务；账号credentials、extra和原始key不进入响应。
        sql = f"""BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5000ms';
SELECT json_build_object(
 'group', (SELECT json_build_object('id', id, 'status', status, 'model_allowlist', model_allowlist) FROM groups WHERE id={group_id} AND deleted_at IS NULL),
 'accounts', (SELECT coalesce(json_agg(json_build_object('id', a.id, 'platform', a.platform, 'status', a.status) ORDER BY a.id), '[]'::json) FROM accounts a JOIN account_groups ag ON ag.account_id=a.id WHERE ag.group_id={group_id} AND a.deleted_at IS NULL),
 'key', (SELECT json_build_object('id', id, 'group_id', group_id, 'status', status, 'key_sha256', encode(sha256(convert_to(key, 'UTF8')), 'hex')) FROM api_keys WHERE id={key_id} AND deleted_at IS NULL));
ROLLBACK;
""".encode("ascii")
        raw = self._command(self._compose() + ["exec", "--no-TTY", db["service"], "psql", "--username", db["user"], "--dbname", db["name"], "--no-password", "--no-psqlrc", "--tuples-only", "--no-align", "--set", "ON_ERROR_STOP=1", "--quiet"], input_bytes=sql, checkpoint=checkpoint)
        document = parse_json_bytes(raw.strip(), "只读数据库事实")
        if not isinstance(document, dict):
            raise guard.GuardError("只读数据库响应非法")
        sql_path = directory / "database-readonly.sql"
        data_path = directory / "database-facts.json"
        guard.write_once(sql_path, sql)
        guard.write_json_once(data_path, document)
        group = document.get("group") or {}
        accounts = document.get("accounts") or []
        key = document.get("key") or {}
        allowlist = group.get("model_allowlist")
        expected = self.probes["expected_models"]
        models = sorted(allowlist["models"]) if isinstance(allowlist, dict) and allowlist.get("enabled") is True and isinstance(allowlist.get("models"), list) and all(isinstance(model, str) for model in allowlist["models"]) else []
        success = group.get("id") == self.probes["group_id"] and group.get("status") == "active" and models == expected and isinstance(accounts, list) and sorted(item.get("id") for item in accounts) == self.probes["account_ids"] and all(item.get("status") == "active" and item.get("platform") == "anthropic" for item in accounts) and key.get("id") == key_id and key.get("group_id") == self.probes["group_id"] and key.get("status") == "active" and self._credential is not None and key.get("key_sha256") == hashlib.sha256(self._credential).hexdigest()
        return bool(success), [guard.binding(self.root, sql_path), guard.binding(self.root, data_path)]

    def _business(self, directory: Path, records: list[dict[str, Any]], checkpoint: Any = None) -> tuple[dict[str, bool], list[dict[str, Any]]]:
        guard.private_directory(directory)
        health_status, health_body = self._http(directory, False, "/health", records, checkpoint)
        models_status, models_body = self._http(directory, True, "/v1/models", records, checkpoint)
        rejected_status, _ = self._http(directory, False, "/v1/models", records, checkpoint)
        models = models_body.get("data") if isinstance(models_body, dict) else None
        model_pass = models_status == 200 and isinstance(models, list) and all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in models) and sorted(item["id"] for item in models) == self.probes["expected_models"] and models_body.get("object") == "list"
        data_pass, bindings = self._database(directory, checkpoint)
        redis_raw = self._command(self._compose() + ["exec", "--no-TTY", self.probes["redis"]["service"], "redis-cli", "PING"], checkpoint=checkpoint)
        redis_path = directory / "redis-ping.txt"
        guard.write_once(redis_path, redis_raw)
        bindings.append(guard.binding(self.root, redis_path))
        return {"health": health_status == 200 and isinstance(health_body, dict) and health_body.get("status") in {"ok", "healthy"}, "auth": model_pass and rejected_status in {401, 403}, "data": data_pass, "dependencies": data_pass and redis_raw.strip() == b"PONG", "entry-routing": model_pass and data_pass, "smoke": model_pass and data_pass and rejected_status in {401, 403}}, bindings

    def _switch(self, material: str, checkpoint: Any = None) -> None:
        if checkpoint:
            checkpoint()
        source = guard.check_binding(self.root, self.configuration[material]["profile_binding"])
        active = guard.managed_path(self.root, self.configuration["paths"]["active_profile_relative"])
        guard.private_directory(active.parent)
        temporary = active.parent / (".profile-" + hashlib.sha256(guard.read_bytes(source)).hexdigest())
        guard.write_once(temporary, guard.read_bytes(source))
        os.replace(temporary, active)
        guard.sync_directory(active.parent)
        if checkpoint:
            checkpoint()
        self._command(self._compose(material) + ["up", "--no-deps", "--detach", "--no-build", "--force-recreate", self.runtime["application_service"]], checkpoint=checkpoint)

    def _observe(self, stage: str, target: dict[str, Any], expected_environment: dict[str, Any], directory: Path, checkpoint: Any = None, *, collection_phase: str = "execution") -> dict[str, Any]:
        started = time.monotonic()
        records: list[dict[str, Any]] = []
        actual, environment, originals, _ = self._inspect(directory / "identity", checkpoint, collection_phase=collection_phase)
        business, business_bindings = self._business(directory / "business", records, checkpoint)
        originals.extend(business_bindings)
        image_pass = actual["image"].get("state") == "known" and actual["image"].get("value") == target["image_digest"]
        profile_pass = actual["profile"].get("state") == "known" and actual["profile"].get("value") == target["runtime_profile_sha256"]
        stable_pass = environment == expected_environment
        values = {**business, "image-identity": image_pass, "profile": profile_pass, "identity": image_pass and profile_pass and stable_pass, "release-isolation": stable_pass, "safety": stable_pass and all(business.values()), "state": stable_pass and all(business.values())}
        if stage == "stable_observed":
            deadline = started + self.configuration["limits"]["observation_window_seconds"]
            number = 0
            while time.monotonic() < deadline:
                if checkpoint:
                    checkpoint()
                time.sleep(min(self.configuration["limits"]["poll_seconds"], max(0, deadline - time.monotonic())))
                number += 1
                current_actual, current_environment, more, _ = self._inspect(directory / f"window-{number:06d}" / "identity", checkpoint, collection_phase=collection_phase)
                current_business, more_business = self._business(directory / f"window-{number:06d}" / "business", records, checkpoint)
                originals.extend(more + more_business)
                stable_pass = stable_pass and current_environment == expected_environment
                identity_pass = all(current_actual[key].get("state") == "known" and current_actual[key].get("value") == target[target_key] for key, target_key in (("image", "image_digest"), ("profile", "runtime_profile_sha256")))
                values["identity"] = values["identity"] and identity_pass and stable_pass
                values["data"] = values["data"] and current_business["data"]
                values["release-isolation"] = values["release-isolation"] and stable_pass
                values["safety"] = values["safety"] and all(current_business.values()) and stable_pass
                values["state"] = values["state"] and stable_pass and identity_pass
                actual, environment = current_actual, current_environment
        proof_bindings = sorted(guard.all_bindings([originals, records]), key=lambda item: item["path"])
        check_report = {"schema_version": "claude-upgrade-delivery-stage-checks/v1", "stage": stage, "target": target, "actual": actual, "expected_environment": expected_environment, "actual_environment": environment, "probe_results": business, "result_by_check": {name: values[name] for name in guard.STAGE_CHECKS[stage]}, "bindings": proof_bindings}
        check_path = directory / "checks.json"
        guard.write_json_once(check_path, check_report)
        check_binding = guard.binding(self.root, check_path)
        originals.append(check_binding)
        checks = [{"id": name, "result": "pass" if values[name] else "failed", "facts": {"source": "fixed-readonly-probes", "check_report_binding": check_binding, "bindings": proof_bindings}} for name in guard.STAGE_CHECKS[stage]]
        passed = image_pass and profile_pass and stable_pass and all(item["result"] == "pass" for item in checks)
        duration = time.monotonic() - started
        timing_path = directory / "timing.json"
        guard.write_json_once(timing_path, {"stage": stage, "actual_elapsed_seconds": duration, "observation_window_seconds": self.configuration["limits"]["observation_window_seconds"] if stage == "stable_observed" else 0})
        originals.append(guard.binding(self.root, timing_path))
        return {"stage": stage, "target": target, "actual": actual, "environment": environment, "checks": checks, "observed_seconds": int(duration) if stage == "stable_observed" else 0, "live_request_count": 0, "request_records": records, "before_bindings": [], "during_bindings": originals, "after_bindings": all_bindings_for_records(records), "result": "pass" if passed else "failed"}

    def execute(self, stage: str, target: dict[str, Any], environment: dict[str, Any], directory: Path, checkpoint: Any) -> dict[str, Any]:
        qualification = self.check_qualification()
        qualification_path = directory / "qualification.json"
        guard.write_json_once(qualification_path, qualification)
        started = time.monotonic()
        def bounded() -> None:
            checkpoint()
            if time.monotonic() - started > self.configuration["limits"]["stage_timeout_seconds"]:
                raise guard.GuardError("单阶段超过冻结时限")
        if stage != "stable_observed":
            self._switch("rollback" if stage == "rollback_verified" else "candidate", bounded)
        observation = self._observe(stage, target, environment, directory / "during", bounded)
        observation["during_bindings"].append(guard.binding(self.root, qualification_path))
        return observation

    def restore(self, target: dict[str, Any], environment: dict[str, Any], directory: Path) -> dict[str, Any]:
        # 恢复不依赖失效worker租约；仍受每个固定命令与完整阶段时限约束。
        started = time.monotonic()
        def bounded() -> None:
            if time.monotonic() - started > self.configuration["limits"]["stage_timeout_seconds"]:
                raise guard.GuardError("恢复超过冻结时限")
        self._credential = guard.read_bound_bytes(self.root, self.probes["credential_binding"]).strip()
        self._switch("rollback", bounded)
        observation = self._observe("rollback_verified", target, environment, directory / "observed", bounded, collection_phase="recovery")
        return {"target": target, "actual": observation["actual"], "checks": observation["checks"], "bindings": [*observation["during_bindings"], *observation["after_bindings"]], "result": observation["result"]}

    def collect_completed_delivery_admission(self, request: dict[str, Any], directory: Path,
                                            stable_execution: Mapping[str, Any], *, source: Any) -> dict[str, Any]:
        """当前 Docker 实测复用原九门禁；不签旧 producer，不再等待观察窗。"""
        from tools.official_client_control.qualification_material_import import _material_successor_write_phase
        with _material_successor_write_phase():
            guard.private_directory(directory)
        qualification = self._check_completed_qualification(source)
        with _material_successor_write_phase():
            return self._write_completed_delivery_admission_measurement(request, directory,
                stable_execution, qualification)

    def _write_completed_delivery_admission_measurement(self, request: dict[str, Any], directory: Path,
                                            stable_execution: Mapping[str, Any], qualification: Mapping[str, Any]) -> dict[str, Any]:
        """实际九门禁及诊断原件写相位；进入前已完成资格语义段完整尾验。"""
        qualification_path = directory / "qualification.json"
        guard.write_json_once(qualification_path, qualification)
        actual, environment, originals, _ = self._inspect(directory / "identity")
        records: list[dict[str, Any]] = []
        business, business_bindings = self._business(directory / "business", records)
        originals.extend([guard.binding(self.root, qualification_path), *business_bindings, *all_bindings_for_records(records)])
        if guard.read_bound_json(self.root, self.context["configuration_ref"]) != self.configuration:
            raise guard.GuardError("完成后准入的原配置字节漂移")
        inventory_path = directory / "inventory.json"
        guard.write_json_once(inventory_path, {"bindings": sorted(guard.all_bindings(originals), key=lambda item: item["path"]),
                                              "all_bytes_revalidated": all(guard.check_binding(self.root, item) for item in guard.all_bindings(originals))})
        inventory_binding = guard.binding(self.root, inventory_path)
        originals.append(inventory_binding)
        rollback = {key: self.configuration["rollback"][key] for key in ("image_digest", "runtime_profile_sha256")}
        allowed = {(self.configuration[name]["image_digest"], self.configuration[name]["runtime_profile_sha256"]) for name in ("candidate", "rollback")}
        public = canonical_json_bytes({"environment": environment, "rollback": rollback, "actual": actual, "request_records": records})
        key = guard.read_bound_bytes(self.root, self.configuration["guard"]["channel_key_binding"])
        scanned = [public, *(guard.read_bound_bytes(self.root, item) for item in all_bindings_for_records(records))]
        no_leak = all(secret not in raw for secret in (self._credential, key) if secret for raw in scanned)
        scan_path = directory / "public-secret-scan.json"
        guard.write_json_once(scan_path, {"schema_version": "claude-upgrade-public-secret-scan/v1", "scope": "current_completed_admission_and_http_records",
            "public_core_sha256": hashlib.sha256(public).hexdigest(), "http_bindings": all_bindings_for_records(records),
            "private_secret_fingerprints": [hashlib.sha256(secret).hexdigest() for secret in (self._credential, key) if secret],
            "result": "pass" if no_leak else "failed"})
        scan_binding = guard.binding(self.root, scan_path)
        originals.append(scan_binding)
        gate_values = {"compose-diff": qualification["candidate_compose"]["services"][self.runtime["application_service"]]["image"] != qualification["rollback_compose"]["services"][self.runtime["application_service"]]["image"] and qualification["result"] == "pass",
            "dependency-connectivity": business["dependencies"], "entry-routing": business["entry-routing"], "evidence-inventory": bool(originals), "health": business["health"],
            "image-identity": actual["image"].get("state") == "known" and actual["profile"].get("state") == "known" and (actual["image"].get("value"), actual["profile"].get("value")) in allowed,
            "observation-window": request["observation_window_seconds"] == self.configuration["limits"]["observation_window_seconds"] and stable_execution["observed_seconds"] >= request["observation_window_seconds"] and stable_execution["result"] == "pass",
            "rollback-availability": qualification["qualification_replays"][1]["result"] == "pass", "secret-scan": no_leak}
        gates = [{"id": name, "result": "pass" if gate_values[name] else "failed", "facts": {"source": "current-arm64-completed-readonly-admission", "bindings": [*originals]}} for name in DELIVERY_GATES]
        return {"environment": environment, "rollback": rollback, "actual": actual, "gates": gates,
                "bindings": sorted(guard.all_bindings(originals), key=lambda item: item["path"])}

    def freeze(self, request: dict[str, Any], directory: Path) -> dict[str, Any]:
        qualification = self.check_qualification()
        qualification_path = directory / "qualification.json"
        guard.write_json_once(qualification_path, qualification)
        guard.check_binding(self.root, self.context["configuration_ref"])
        if guard.read_bound_json(self.root, self.context["configuration_ref"]) != self.configuration:
            raise guard.GuardError("raw配置binding与有效配置不一致")
        actual, environment, originals, _ = self._inspect(directory / "before", collection_phase="freeze")
        records: list[dict[str, Any]] = []
        business, business_bindings = self._business(directory / "business", records)
        originals.extend(business_bindings + all_bindings_for_records(records))
        qualification_binding = guard.binding(self.root, qualification_path)
        originals.append(qualification_binding)
        snapshot = guard.read_json(directory / "before" / "stable-projection.json")
        common = {"campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"], "result": "pass"}
        inventory_path = directory / "inventory.json"
        guard.write_json_once(inventory_path, {"bindings": sorted(guard.all_bindings(originals), key=lambda item: item["path"]), "all_bytes_revalidated": all(guard.check_binding(self.root, item) for item in guard.all_bindings(originals))})
        inventory_binding = guard.binding(self.root, inventory_path)
        originals.append(inventory_binding)
        rollback = self.configuration["rollback"]
        q = rollback["qualification"]
        from tools.official_client_control.store import ControlStore
        store = ControlStore.open_read_only(guard.managed_path(self.root, q["store_relative"]))
        acceptance = store.load_fact(q["acceptance_ref"])
        probe_recipe = {"schema_version": "claude-upgrade-zero-model-probe-recipe/v1", "allowed_requests": [{"method": "GET", "path": "/health", "authentication": "none"}, {"method": "GET", "path": "/v1/models", "authentication": "candidate_private_ref"}, {"method": "GET", "path": "/v1/models", "authentication": "none"}], "group_id": self.probes["group_id"], "account_ids": self.probes["account_ids"], "expected_models": self.probes["expected_models"], "max_requests": self.probes["max_requests"], "timeout_seconds": self.probes["timeout_seconds"], "redirect": "reject", "database": "fixed_read_only_transaction", "redis": "PING"}
        recipe_path = directory / "probe-recipe.json"
        guard.write_json_once(recipe_path, probe_recipe)
        cores = {
            "configuration": {"configuration": self.configuration, "configuration_sha256": canonical_sha256(self.configuration), "binding": self.context["configuration_ref"]},
            "stable_projection": {"projection": snapshot, "projection_sha256": canonical_sha256(snapshot), "binding": guard.binding(self.root, directory / "before" / "stable-projection.json")},
            "freeze": {"environment": environment, "before_bindings": originals, "gates": [], "guard_identity": {}},
            "rollback_state": {"candidate_ref": q["candidate_ref"], "build_receipt_ref": q["build_receipt_ref"], "acceptance_ref": q["acceptance_ref"], "evidence_package_ref": acceptance["payload"]["candidate_evidence_package_ref"], "image_digest": rollback["image_digest"], "runtime_profile_sha256": rollback["runtime_profile_sha256"], "bindings": [rollback["profile_binding"], rollback["override_binding"], q["sealed_result_binding"]]},
            "zero_model_probes": {"mode": "recipe", "recipe": probe_recipe, "request_records": [], "bindings": [guard.binding(self.root, recipe_path)]},
            "deployment_configuration": {"configuration_binding": self.context["configuration_ref"], "deployment_sha256": self.configuration["guard"]["deployment_receipt_binding"]["sha256"], "bindings": [self.configuration["guard"][name] for name in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding")]},
        }
        for kind, core in cores.items():
            core.update(common)
            core["contract_version"] = f"claude-delivery-{kind.replace('_', '-')}/v1"
        key = guard.read_bound_bytes(self.root, self.configuration["guard"]["channel_key_binding"])
        public_bytes = canonical_json_bytes(cores)
        http_public = [guard.read_bound_bytes(self.root, item) for item in all_bindings_for_records(records)]
        scanned = [public_bytes, *http_public]
        no_leak = all(secret not in data for secret in (self._credential, key) if secret for data in scanned)
        scan_path = directory / "public-secret-scan.json"
        guard.write_json_once(scan_path, {"schema_version": "claude-upgrade-public-secret-scan/v1", "scope": "public_core_and_http_records", "public_core_sha256": hashlib.sha256(public_bytes).hexdigest(), "http_bindings": all_bindings_for_records(records), "private_secret_fingerprints": [hashlib.sha256(secret).hexdigest() for secret in (self._credential, key) if secret], "result": "pass" if no_leak else "failed"})
        scan_binding = guard.binding(self.root, scan_path)
        allowed = {(self.configuration[name]["image_digest"], self.configuration[name]["runtime_profile_sha256"]) for name in ("candidate", "rollback")}
        gate_values = {"compose-diff": qualification["candidate_compose"]["services"][self.runtime["application_service"]]["image"] != qualification["rollback_compose"]["services"][self.runtime["application_service"]]["image"] and qualification["result"] == "pass", "dependency-connectivity": business["dependencies"], "entry-routing": business["entry-routing"], "evidence-inventory": bool(originals), "health": business["health"], "image-identity": actual["image"].get("state") == "known" and actual["profile"].get("state") == "known" and (actual["image"].get("value"), actual["profile"].get("value")) in allowed, "observation-window": request["observation_window_seconds"] == self.configuration["limits"]["observation_window_seconds"], "rollback-availability": qualification["qualification_replays"][1]["result"] == "pass", "secret-scan": no_leak}
        gates = [{"id": name, "result": "pass" if gate_values[name] else "failed", "facts": {"source": "fixed-arm64-readonly-freeze", "bindings": [qualification_binding, inventory_binding, scan_binding, *originals]}} for name in DELIVERY_GATES]
        cores["freeze"]["gates"] = gates
        if any(item["result"] != "pass" for item in gates):
            for core in cores.values():
                core["result"] = "failed"
        return {"environment": environment, "rollback": {"image_digest": rollback["image_digest"], "runtime_profile_sha256": rollback["runtime_profile_sha256"]}, "gates": gates, "cores": cores}


def all_bindings_for_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return guard.all_bindings(records)


class Arm64DeliveryExecutor:
    """受管工厂能力实例。构造不会启动守卫或修改现场。"""

    contract_version = EXECUTOR_VERSION

    def __init__(self, configuration: dict[str, Any], execution_context: dict[str, Any]):
        self.configuration = validate_configuration(configuration)
        self.context = validate_execution_context(execution_context, self.configuration)
        self.data_root = Path(self.context["data_root"])
        guard.private_directory(self.data_root)
        if guard.read_bound_json(self.data_root, self.context["configuration_ref"]) != self.configuration:
            raise guard.GuardError("冻结raw配置不等于factory有效配置")
        for item in guard.all_bindings(configuration):
            guard.check_binding(self.data_root, item)
        key_path = guard.check_binding(self.data_root, configuration["guard"]["channel_key_binding"])
        if key_path.stat().st_mode & 0o077:
            raise guard.GuardError("守卫通道密钥必须为私有600文件")
        self.client = guard.GuardClient(self.data_root, configuration["guard"]["socket_relative"], configuration["guard"]["channel_key_binding"], configuration["limits"]["stage_timeout_seconds"] + 30)
        self.owner = guard.process_identity(os.getpid())
        self._heartbeats: dict[str, tuple[threading.Event, threading.Thread]] = {}

    def _identity(self, identity: Any, *, require_live: bool = True) -> dict[str, Any]:
        item = guard.exact(identity, GUARD_IDENTITY_FIELDS, "guard身份")
        expected = {"contract_version": GUARD_IDENTITY_SCHEMA, "authority": AUTHORITY, "source_scope": self.configuration["source_scope"], "host_id": "ARM64", "instance_id": self.configuration["instance_id"], "service_name": self.configuration["guard"]["service_name"]}
        if any(item[key] != value for key, value in expected.items()) or item["process_group_id"] == self.owner["process_group_id"]:
            raise guard.GuardError("guard来源、实例或独立进程组不符")
        if self.configuration["source_scope"] == "formal" and item["independent_service"] is not True:
            raise guard.GuardError("formal执行器缺实际独立service证明")
        for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"):
            if item[key] != self.configuration["guard"][key]:
                raise guard.GuardError("守卫实际部署字节与冻结配置不符")
            guard.check_binding(self.data_root, item[key])
        if require_live and not guard.owner_alive({key: item[key] for key in ("boot_id", "pid", "process_start", "process_group_id")}):
            raise guard.GuardError("守卫实例已消失或PID被重用")
        return item

    def freeze(self, request: dict[str, Any]) -> dict[str, Any]:
        guard.exact(request, FREEZE_FIELDS, "freeze请求")
        expected = {"campaign_id": self.context["campaign_id"], "candidate_id": self.context["candidate_id"], "delivery_id": self.context["delivery_id"], "candidate_image_digest": self.configuration["candidate"]["image_digest"], "runtime_profile_sha256": self.configuration["candidate"]["runtime_profile_sha256"], "observation_window_seconds": self.configuration["limits"]["observation_window_seconds"]}
        if any(request[key] != value for key, value in expected.items()) or request["architecture"] not in {"arm64", "aarch64", "linux/arm64"} or request["delivery_gates"] != list(DELIVERY_GATES):
            raise guard.GuardError("freeze请求与配置或门禁闭集不符")
        self._status()
        snapshot = self.client.call("inspect", request)
        guard.exact(snapshot, {"environment", "rollback", "gates", "cores"}, "freeze返回")
        guard.exact(snapshot["cores"], EVIDENCE_TYPES, "冻结证据闭集")
        self._identity(snapshot["cores"]["freeze"]["guard_identity"])
        return snapshot

    def _start_heartbeat(self, ticket: dict[str, Any]) -> None:
        operation_id = ticket["operation_id"]
        if operation_id in self._heartbeats:
            return
        stop = threading.Event()
        def heartbeat() -> None:
            while not stop.wait(max(0.1, self.configuration["limits"]["lease_seconds"] / 3)):
                try:
                    self.client.call("heartbeat", {"ticket": ticket, "owner": self.owner})
                except Exception:
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        self._heartbeats[operation_id] = (stop, thread)
        thread.start()

    def prepare_stage(self, request: dict[str, Any]) -> dict[str, Any]:
        self._status()
        ticket = self.client.call("prepare", {"request": request, "owner": self.owner})
        self._identity(ticket["guard_identity"], require_live=False)
        guard.check_binding(self.data_root, ticket["prepared_binding"])
        self._start_heartbeat(ticket)
        return ticket

    def run_stage(self, ticket: dict[str, Any]) -> dict[str, Any]:
        return self.client.call("run", ticket)

    def finish_stage(self, ticket: dict[str, Any], verified_decision: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.client.call("finish", {"ticket": ticket, "decision": verified_decision})
        finally:
            existing = self._heartbeats.pop(ticket["operation_id"], None)
            if existing:
                existing[0].set()
                existing[1].join(timeout=1)

    def recover_pending(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.client.call("recover", request)

    def _status(self) -> dict[str, Any]:
        result = self.client.call("status", {})
        self._identity(result["guard_identity"])
        expected = {key: value for key, value in self.context.items() if key not in {"segment", "run_nonce"}}
        if result["execution_context"] != expected:
            raise guard.GuardError("固定守卫服务属于另一Campaign或delivery")
        return result

    def inventory(self) -> dict[str, Any]:
        self._status()
        return self.client.call("inventory", {})

    def operations_index(self) -> dict[str, Any]:
        inventory = self.inventory()
        return {"operation_index_binding": inventory["operations_index_binding"], "origin": inventory["origin"]}

    def _successor_store(self):
        from tools.official_client_control.store import ControlStore
        from tools.official_client_control.qualification_material_import import _material_successor_provider_store
        qualification = self.configuration["candidate"]["qualification"]
        root = guard.managed_path(self.data_root, qualification["store_relative"])
        selected = _material_successor_provider_store(self, root)
        return selected if selected is not None else ControlStore.open_read_only(root)

    def _completed_delivery_runtime(self):
        """固定正式 runtime；回归仅在已有 ControlledExecutor 边界替换。"""
        return DockerComposeRuntime(self.configuration, self.context)

    def _completed_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = guard.exact(dict(request), FREEZE_FIELDS, "完成后只读准入请求")
        expected = {"campaign_id": self.context["campaign_id"], "candidate_id": self.context["candidate_id"],
            "delivery_id": self.context["delivery_id"], "candidate_image_digest": self.configuration["candidate"]["image_digest"],
            "runtime_profile_sha256": self.configuration["candidate"]["runtime_profile_sha256"],
            "observation_window_seconds": self.configuration["limits"]["observation_window_seconds"]}
        if (any(value[key] != item for key, item in expected.items())
                or value["architecture"] not in {"arm64", "aarch64", "linux/arm64"}
                or value["delivery_gates"] != list(DELIVERY_GATES)):
            raise guard.GuardError("完成后只读请求与原配置或九门禁闭集不一致")
        return value

    def _completed_inputs(self, store: Any, projection: Any, state: Mapping[str, Any],
                          originals: _ProducerOriginals, *, require_unfinalized: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """原有效四 pass 和 stable 原观察文件是唯一历史观察窗依据。"""
        from tools.official_client_control.contracts import CANDIDATE_DELIVERY_STAGES
        from tools.official_client_control.receipts import delivery_chain_state, delivery_receipts
        from tools.official_client_control.delivery_execution import validate_operational_evidence
        successor = getattr(projection, "successor", None)
        if successor is None:
            raise guard.GuardError("完成后准入缺已完整验证的材料后继")
        facts = successor.facts
        plan = store.load_object(facts["plan_ref"])["payload"]
        chain = delivery_chain_state(store, self.context["campaign_id"], self.context["delivery_id"])
        if (not chain.complete() or chain.pending is not None or chain.valid_refs(store) != facts["delivery_fact_refs"]
                or facts["guard_identity"] != state["status"]["guard_identity"]
                or facts["execution_context"] != state["status"]["execution_context"]):
            raise guard.GuardError("完成后准入的原四 pass、原守卫或稳定上下文已变化")
        if require_unfinalized and delivery_receipts(store, self.context["campaign_id"], facts["plan_ref"]):
            raise guard.GuardError("已有完成收据的 Plan 不得重新形成完成后现场报告")
        environment = {key: plan[key] for key in ("host_id", "architecture", "compose_sha256", "environment_sha256", "configuration_sha256", "network_sha256")}
        if facts["environment"] != environment or environment["configuration_sha256"] != canonical_sha256(self.configuration):
            raise guard.GuardError("完成后准入的六环境字段不是原 Plan")
        stable_core, stable_binding = None, None
        for stage, reference in zip(CANDIDATE_DELIVERY_STAGES, facts["delivery_fact_refs"]):
            fact = store.load_fact(reference)
            payload = fact["payload"]
            if (fact["fact_kind"] != "candidate_delivery_recorded" or payload["stage"] != stage
                    or payload["delivery_plan_ref"] != facts["plan_ref"] or payload["result"] != "pass"
                    or any(payload[key] != item for key, item in environment.items())):
                raise guard.GuardError("完成后准入不是原 Plan 的有序四阶段 pass")
            candidates = []
            for evidence_ref in payload["evidence_refs"]:
                manifest = store.load_object(evidence_ref)["payload"]
                if manifest.get("evidence_type") != "stage_execution":
                    continue
                validate_operational_evidence(manifest)
                value = manifest["entries"][0]["facts"]
                origin = value["origin"]
                core = {key: item for key, item in value.items() if key != "origin"}
                if core["stage"] != stage or core["plan_ref"] != facts["plan_ref"] or core["result"] != "pass":
                    raise guard.GuardError("原四 pass 的完整物理 producer 与 Plan 不一致")
                binding, pointer, _ = self._producer_record(state, core, origin, originals)
                if pointer:
                    raise guard.GuardError("原物理 pass 不能由 freeze core 代替")
                envelope = {"schema_version": guard_epochs.CONTINUITY_V2_SCHEMA,
                    "source_service_instance_id": origin["service_instance_id"],
                    "channel_service_instance_id": state["status"]["guard_identity"]["service_instance_id"],
                    "stable_deployment_identity_sha256": state["registered"].stable_deployment_identity_sha256,
                    "epoch_bindings": state["registrations"], "readiness_bindings": state["readiness"],
                    "registry_head_binding": state["head"], "source_record_binding": binding, "source_json_pointer": "",
                    "current_operations_index_binding": state["inventory"]["operations_index_binding"]}
                guard_epochs.validate_guard_continuity(self.configuration, self.context, envelope, originals.add,
                    source_core=core, source_origin=origin, channel_identity=state["status"]["guard_identity"], purpose="current_execution")
                candidates.append((core, binding))
            if len(candidates) != 1:
                raise guard.GuardError("原 pass 没有唯一完整物理执行证据")
            if stage == "stable_observed":
                stable_core, stable_binding = candidates[0]
                path = PurePosixPath(stable_binding["path"]).parent.as_posix() + "/observation.json"
                rows = [row for row in state["index"]["operations"] if row["operation_id"] == stable_core["operation_id"]]
                observation_bindings = [item for row in rows for item in row["bindings"] if item["path"] == path]
                if len(observation_bindings) != 1:
                    raise guard.GuardError("原 stable pass 缺固定观察原件或被索引省略")
                observation = parse_json_bytes(originals.add(observation_bindings[0]))
                for key in ("stage", "actual", "environment", "checks", "observed_seconds", "request_records", "result"):
                    if observation[key] != stable_core[key]:
                        raise guard.GuardError("原 stable producer 与真实观察原件不相同")
                if (stable_core["observed_seconds"] < plan["observation_window_seconds"]
                        or stable_core["observed_seconds"] != payload["observed_seconds"]
                        or any(item["result"] != "pass" for item in stable_core["checks"])):
                    raise guard.GuardError("原 stable pass 没有完整证明冻结观察窗")
        return plan, stable_core, stable_binding

    def _completed_report_path(self, successor: Any) -> Path:
        identity = canonical_sha256({"plan_ref": successor.facts["plan_ref"], "successor_ref": successor.reference,
                                    "configuration_binding": self.context["configuration_ref"]})
        return guard.managed_path(self.data_root, "control/completed-delivery-admissions/" + identity + "/report.json")

    def _collect_completed_measurement(self, request: Mapping[str, Any], directory: Path,
                                       plan: Mapping[str, Any], stable: Mapping[str, Any], *, source: Any) -> dict[str, Any]:
        runtime = self._completed_delivery_runtime()
        if self.configuration["source_scope"] == "formal" and type(runtime) is not DockerComposeRuntime:
            raise guard.GuardError("正式完成后准入必须实际使用固定 Docker runtime")
        if type(runtime) is DockerComposeRuntime:
            value = runtime.collect_completed_delivery_admission(dict(request), directory, stable, source=source)
        else:
            # 回归现场接缝不承接产品内部只读相位能力，整次实测按独立写相位处理。
            from tools.official_client_control.qualification_material_import import _material_successor_write_phase
            with _material_successor_write_phase():
                value = runtime.collect_completed_delivery_admission(dict(request), directory, stable, source=source)
        guard.exact(value, {"environment", "rollback", "actual", "gates", "bindings"}, "当前完成后现场测量")
        _completed_measurement_paths(value, directory.relative_to(self.data_root).as_posix())
        expected = {key: plan[key] for key in ("host_id", "architecture", "compose_sha256", "environment_sha256", "configuration_sha256", "network_sha256")}
        rollback = {"image_digest": plan["rollback_image_digest"], "runtime_profile_sha256": plan["rollback_runtime_profile_sha256"]}
        if (value["environment"] != expected or value["rollback"] != rollback
                or [item["id"] for item in value["gates"]] != list(DELIVERY_GATES)
                or any(item["result"] != "pass" for item in value["gates"])
                or any(value["actual"][key].get("state") != "known" or value["actual"][key].get("value") != self.configuration["candidate"][target]
                       for key, target in (("image", "image_digest"), ("profile", "runtime_profile_sha256")))):
            raise guard.GuardError("完成后真实九门禁、候选状态、六环境或回退身份漂移")
        return value

    def _read_completed_report(self, store: Any, projection: Any, state: Mapping[str, Any], originals: _ProducerOriginals,
                               *, require_existing: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        """只核固定首次报告原件；现场权限仍由下方独立实测入口取得。"""
        successor = projection.successor
        path = self._completed_report_path(successor)
        if not path.exists():
            if require_existing:
                raise guard.GuardError("当前完成后现场报告缺失")
            return None
        plan, stable, stable_binding = self._completed_inputs(store, projection, state, originals, require_unfinalized=False)
        binding = guard.binding(self.data_root, path)
        report = parse_json_bytes(originals.add(binding))
        fields = {"schema_version", "client", "campaign_id", "candidate_id", "delivery_id", "plan_ref", "configuration_binding", "configuration_sha256",
                  "successor_ref", "reader_identity_binding", "control_deployment_binding", "original_guard_identity", "stable_execution_binding", "environment",
                  "rollback", "gates", "observations", "bindings", "identity_sha256"}
        guard.exact(report, fields, "完成后不可变现场报告")
        proof = parse_json_bytes(originals.add(successor.successor_materials_binding))
        expected = {"schema_version": "claude-completed-delivery-admission/v1", "client": "claude",
            **{key: self.context[key] for key in ("campaign_id", "candidate_id", "delivery_id")},
            "plan_ref": successor.facts["plan_ref"], "configuration_binding": self.context["configuration_ref"],
            "configuration_sha256": canonical_sha256(self.configuration), "successor_ref": successor.reference,
            "reader_identity_binding": proof["reader_identity_binding"], "control_deployment_binding": successor.facts["control_deployment_binding"],
            "original_guard_identity": state["status"]["guard_identity"], "stable_execution_binding": stable_binding}
        if (any(report[key] != item for key, item in expected.items())
                or report["identity_sha256"] != canonical_sha256({key: item for key, item in report.items() if key != "identity_sha256"})
                or not isinstance(report["observations"], list) or len(report["observations"]) != 2):
            raise guard.GuardError("完成后报告没有绑定当前 reader、控制部署、原配置和原稳定 pass")
        observation_paths = [_completed_measurement_paths(item, (path.parent / "initial" / str(number)).relative_to(self.data_root).as_posix())
                             for number, item in enumerate(report["observations"], 1)]
        if (observation_paths[0] != observation_paths[1]
                or report["bindings"] != sorted(guard.all_bindings(report["observations"]), key=lambda item: item["path"])):
            raise guard.GuardError("固定首次报告的两测量绑定坐标或归档闭集不一致")
        for item in guard.all_bindings(report):
            originals.add(item)
        signature = _completed_live_signature(report["observations"][0])
        if (signature != _completed_live_signature(report["observations"][1])
                or report["environment"] != report["observations"][0]["environment"]
                or report["rollback"] != report["observations"][0]["rollback"]
                or report["gates"] != report["observations"][0]["gates"]):
            raise guard.GuardError("固定首次报告的首尾测量或准入结论不一致")
        return report, binding, plan, stable

    def _check_completed_report(self, store: Any, projection: Any, state: Mapping[str, Any], originals: _ProducerOriginals,
                                *, source: Any, require_existing: bool) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """当前实测授内存准入；固定首次报告仅作为不可变归档坐标。"""
        checked = self._read_completed_report(store, projection, state, originals, require_existing=require_existing)
        if checked is None:
            return None
        report, binding, plan, stable = checked
        signature = _completed_live_signature(report["observations"][0])
        request = self._completed_request({"campaign_id": self.context["campaign_id"], "candidate_id": self.context["candidate_id"],
            "delivery_id": self.context["delivery_id"], "candidate_image_digest": self.configuration["candidate"]["image_digest"],
            "architecture": plan["architecture"], "runtime_profile_sha256": self.configuration["candidate"]["runtime_profile_sha256"],
            "observation_window_seconds": plan["observation_window_seconds"], "delivery_gates": list(DELIVERY_GATES)})
        diagnostic = self._completed_report_path(projection.successor).parent / "rechecks" / secrets.token_hex(16)
        for number in (1, 2):
            directory = diagnostic / str(number)
            measured = self._collect_completed_measurement(request, directory, plan, stable, source=source)
            expected_paths = _completed_measurement_paths(report["observations"][number - 1],
                (self._completed_report_path(projection.successor).parent / "initial" / str(number)).relative_to(self.data_root).as_posix())
            if (_completed_live_signature(measured) != signature
                    or _completed_measurement_paths(measured, directory.relative_to(self.data_root).as_posix()) != expected_paths):
                raise guard.GuardError("当前完成后现场与固定首次报告漂移")
        originals.revalidate()
        return report, binding

    def read_completed_delivery_admission(self, request: Mapping[str, Any], *, store: Any,
                                         plan_ref: Mapping[str, Any], delivery_fact_refs: list[dict[str, Any]],
                                         delivery_lock_fd: int | None = None) -> VerifiedCompletedDeliveryAdmission:
        """真实持锁只读消费入口；诊断与报告的写相位在内部单独隔离。"""
        from tools.official_client_control.qualification_material_import import _material_successor_consumer_scope
        with _material_successor_consumer_scope(store, self.configuration,
                plan_ref=plan_ref, delivery_lock_fd=delivery_lock_fd):
            return self._read_completed_delivery_admission_in_scope(request, store=store,
                plan_ref=plan_ref, delivery_fact_refs=delivery_fact_refs)

    def _read_completed_delivery_admission_in_scope(self, request: Mapping[str, Any], *, store: Any,
                                         plan_ref: Mapping[str, Any], delivery_fact_refs: list[dict[str, Any]]) -> VerifiedCompletedDeliveryAdmission:
        """原四 pass 后的当前只读准入，不调用旧 guard.inspect/freeze 或物理阶段。"""
        from tools.official_client_control.delivery_execution import VerifiedSource
        from tools.official_client_control.qualification_material_import import describe_material_successor_bindings, resolve_material_successor
        value = self._completed_request(request)
        descriptor = describe_material_successor_bindings(store, self.configuration, plan_ref=plan_ref)
        if descriptor is None or descriptor["facts"]["delivery_fact_refs"] != delivery_fact_refs:
            raise guard.GuardError("完成后准入没有同一原 Plan 与四 pass 的唯一材料关联")
        originals = _ProducerOriginals(self.data_root)
        self._material_originals(store, originals, descriptor)
        state = self._producer_state(originals)
        origin = state["inventory"]["origin"]
        content = {"campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"],
                   "operation_index_binding": origin["operations_index_binding"]}
        rows = self.verify_recorded_origin_with_material_successor(store, [{"content": content, "origin": origin}])
        if rows is None:
            raise guard.GuardError("完成后准入不能回落旧材料来源")
        source = VerifiedSource(**rows[0])
        projection = source.gate_projection
        plan, stable, stable_binding = self._completed_inputs(store, projection, state, originals, require_unfinalized=True)
        successor = projection.successor
        path = self._completed_report_path(successor)
        if not path.exists():
            if path.parent.exists() and any(path.parent.iterdir()):
                raise guard.GuardError("固定首次完成后报告目录不完整，禁止补写已有半成品")
            from tools.official_client_control.qualification_material_import import _material_successor_write_phase
            with _material_successor_write_phase():
                guard.private_directory(path.parent)
            measurements = [self._collect_completed_measurement(value, path.parent / "initial" / str(number), plan, stable, source=source) for number in (1, 2)]
            if _completed_live_signature(measurements[0]) != _completed_live_signature(measurements[1]):
                raise guard.GuardError("完成后首次现场首尾测量漂移")
            proof = parse_json_bytes(originals.add(successor.successor_materials_binding))
            report = {"schema_version": "claude-completed-delivery-admission/v1", "client": "claude",
                **{key: self.context[key] for key in ("campaign_id", "candidate_id", "delivery_id")},
                "plan_ref": dict(plan_ref), "configuration_binding": self.context["configuration_ref"], "configuration_sha256": canonical_sha256(self.configuration),
                "successor_ref": successor.reference, "reader_identity_binding": proof["reader_identity_binding"],
                "control_deployment_binding": successor.facts["control_deployment_binding"], "original_guard_identity": state["status"]["guard_identity"],
                "stable_execution_binding": stable_binding, **{key: measurements[0][key] for key in ("environment", "rollback", "gates")},
                "observations": measurements, "bindings": sorted(guard.all_bindings(measurements), key=lambda item: item["path"])}
            report["identity_sha256"] = canonical_sha256(report)
            # 首尾真实源与有效登记仍相同，才封存首次独立报告；O_EXCL 不覆盖。
            tail = self._producer_state(originals)
            if (tail["status"] != state["status"] or tail["inventory"] != state["inventory"]
                    or describe_material_successor_bindings(store, self.configuration, plan_ref=plan_ref) != descriptor):
                raise guard.GuardError("首次完成后报告形成期间原来源或材料关联漂移")
            resolve_material_successor(store, self.configuration, source, purpose="current_delivery", plan_ref=plan_ref)
            with _material_successor_write_phase():
                guard.write_json_once(path, report)
        # 本入口已由正式来源读取核现场；这里只读固定报告，返回前再独立尾验。
        # 首次报告的两次实际形成测量也不能被历史 JSON 替代。
        checked = self._read_completed_report(store, projection, state, originals, require_existing=True)
        report, report_binding, _plan, _stable = checked
        bindings = tuple(sorted(guard.all_bindings([report, report_binding]), key=lambda item: item["path"]))
        current = _ProducerOriginals(self.data_root)
        for item in bindings:
            current.add(item)
        def tail_check() -> None:
            # 当前现场、完整来源、关联及有效工具身份在每个消费者尾次重新核。
            selected = self.verify_recorded_origin_with_material_successor(store, [{"content": content, "origin": origin}])
            if selected is None or selected[0]["gate_projection"].successor.reference != successor.reference:
                raise guard.GuardError("完成后准入尾验没有同一材料后继")
            current.revalidate()
        result = VerifiedCompletedDeliveryAdmission(MappingProxyType(dict(report["environment"])), MappingProxyType(dict(report["rollback"])),
            tuple(report["gates"]), report_binding, bindings, successor.reference, dict(plan_ref),
            _COMPLETED_ADMISSION_TOKEN, tail_check, current, canonical_sha256(self.configuration), self.data_root)
        result.revalidate()
        return result

    def _material_originals(self, store: Any, originals: _ProducerOriginals, descriptor: Mapping[str, Any]) -> None:
        """候选关联仅是取件描述；通过 selector 前不产生投影权限。"""
        from tools.official_client_control.qualification_material_import import _register_material_successor_provider
        # 先绑定固定 owner 供字节接缝使用；缺真实 state 时尚无语义复用权限。
        _register_material_successor_provider(self, originals, None)
        originals.add(descriptor["object_binding"])
        facts = descriptor["facts"]
        for item in guard.all_bindings([facts, self.configuration, self.context["configuration_ref"]]):
            originals.add(item)
        for name in ("predecessor_materials_binding", "successor_materials_binding"):
            self._import_originals(originals, facts[name])
        self._registration_originals(store, originals, facts["campaign_id"], expected_head=facts["tool_evolution_registration_ref"])
        self._qualification_subject_originals(store, originals)

    def _registration_originals(self, store: Any, originals: _ProducerOriginals, campaign_id: str,
                                *, expected_head: Mapping[str, Any] | None = None) -> None:
        """只沿有效登记单链取固定对象与演进原件，不扫描任意控制目录。"""
        from tools.official_client_control.receipts import tool_evolutions
        chain = tool_evolutions(store, store.load_campaign(campaign_id))
        if not chain or (expected_head is not None and chain[-1][0] != dict(expected_head)):
            raise guard.GuardError("材料来源缺同一有效工具登记头")
        campaign_root = guard.managed_path(self.data_root, "evidence/campaigns/" + campaign_id)
        for reference, entry in chain:
            document = store.load_object(reference)
            binding = originals.binding(store.object_path(reference))
            if parse_json_bytes(originals.add(binding)) != document:
                raise guard.GuardError("工具登记对象与实际 Store 原件不同")
            relative = validate_relative_path(entry["evolution_receipt"]["path"], "登记演进原件")
            path = guard.managed_path(self.data_root, campaign_root.relative_to(self.data_root).as_posix() + "/" + relative)
            binding = originals.binding(path)
            if binding["sha256"] != entry["evolution_receipt"]["sha256"]:
                raise guard.GuardError("有效工具登记的演进原件 SHA 漂移")
            originals.add(binding)
        originals.add(originals.binding(campaign_root / "campaign.json"))

    def _qualification_subject_originals(self, store: Any, originals: _ProducerOriginals,
                                         *, read_bindings: list[dict[str, Any]] | None = None) -> None:
        """沿原 sealed 父调用既有 subject verifier，仅加入其实际返回的精确读集。"""
        from tools.official_client_capture.claude_qualification_material_source import qualification_gate_subject
        from tools.official_client_control.qualification_material_import import _subject_file_requirements, _material_successor_subject_dependencies
        dependency_bindings, dependency_projections = [], []
        for role in ("candidate", "rollback"):
            qualification = self.configuration[role]["qualification"]
            sealed = parse_json_bytes(originals.add(qualification["sealed_result_binding"]))
            if "external_gate" not in sealed and role == "rollback" and self.configuration["source_scope"] == "regression_fixture":
                # 原夹具显式无门禁回退没有 subject 域；不能事后给它补一个域。
                continue
            bindings: list[dict[str, Any]] = []
            projections = []
            qualification_gate_subject(store, qualification, self.data_root, sealed,
                read_bindings=bindings, read_projections=projections)
            artifacts, sources = _subject_file_requirements(store,
                self.data_root, qualification, bindings, projections, originals.add)
            originals._register_subject_artifacts(artifacts)
            originals._register_subject_sources(sources)
            for binding in bindings:
                originals.add(binding)
            dependency_bindings.extend(bindings)
            dependency_projections.extend(projections)
        if read_bindings is not None:
            read_bindings.extend(dependency_bindings)
        from tools.official_client_control.qualification_material_import import _material_successor_byte_nonce
        if _material_successor_byte_nonce(originals) is not None:
            originals._subject_dependency_roots, originals._subject_dependency_probes = _material_successor_subject_dependencies(
                originals, self.configuration, dependency_bindings, dependency_projections)

    @staticmethod
    def _import_originals(originals: _ProducerOriginals, binding: Mapping[str, Any]) -> list[dict[str, Any]]:
        """只取固定 import 合同的完整原件；旧 reader JSON 只作历史锚点。"""
        from tools.official_client_control.qualification_material_import import validate_import_shape
        document = validate_import_shape(parse_json_bytes(originals.add(binding)))
        if binding["path"] != f"control/qualification-imports/{document['import_id']}/material-import.json":
            raise guard.GuardError("材料准备的旧 proof 不在原固定 import 坐标")
        bindings = [dict(binding), *guard.all_bindings(document)]
        for item in bindings[1:]:
            originals.add(item)
        for acquisition in document["source_acquisition_bindings"]:
            value = parse_json_bytes(originals[acquisition["binding"]["path"]])
            acquired = guard.all_bindings(value)
            bindings.extend(acquired)
            for item in acquired:
                originals.add(item)
        return guard.all_bindings(bindings)

    def read_material_successor_preparation_source(self, store: Any, *, plan_ref: Mapping[str, Any]):
        """关联封存前取得真实 producer bootstrap；不给任何门禁投影权限。"""
        from tools.official_client_control.delivery_execution import VerifiedSource, validate_operational_evidence
        from tools.official_client_control.receipts import delivery_chain_state, delivery_receipts
        from tools.official_client_control.contracts import CANDIDATE_DELIVERY_STAGES
        plan = store.load_object(dict(plan_ref))["payload"]
        if (self.configuration["schema_version"] != MATERIAL_CONFIGURATION_SCHEMA
                or plan["campaign_id"] != self.context["campaign_id"] or plan["delivery_id"] != self.context["delivery_id"]
                or plan["candidate_id"] != self.context["candidate_id"] or plan["configuration_sha256"] != canonical_sha256(self.configuration)):
            raise guard.GuardError("材料后继准备来源不是原配置的固定 Plan")
        if store.root != self.data_root / self.configuration["candidate"]["qualification"]["store_relative"]:
            raise guard.GuardError("材料后继准备来源不是原实际 Store")
        chain = delivery_chain_state(store, plan["campaign_id"], plan["delivery_id"])
        if not chain.complete() or chain.pending is not None or delivery_receipts(store, plan["campaign_id"], dict(plan_ref)):
            raise guard.GuardError("材料后继准备必须原四 pass、无 pending、无完成收据")
        originals = _ProducerOriginals(self.data_root)
        for item in guard.all_bindings([self.configuration, self.context["configuration_ref"]]):
            originals.add(item)
        material_bindings = self._import_originals(originals, self.configuration["qualification_materials_binding"])
        self._registration_originals(store, originals, plan["campaign_id"])
        subject_bindings: list[dict[str, Any]] = []
        self._qualification_subject_originals(store, originals, read_bindings=subject_bindings)
        state = self._producer_state(originals)
        producer_bindings = []
        for stage, reference in zip(CANDIDATE_DELIVERY_STAGES, chain.valid_refs(store)):
            fact = store.load_fact(reference)
            payload = fact["payload"]
            environment = {key: plan[key] for key in ("host_id", "architecture", "compose_sha256", "environment_sha256", "configuration_sha256", "network_sha256")}
            if (fact["fact_kind"] != "candidate_delivery_recorded" or payload["stage"] != stage
                    or payload["delivery_plan_ref"] != dict(plan_ref) or payload["result"] != "pass"
                    or any(payload[key] != value for key, value in environment.items())):
                raise guard.GuardError("材料后继准备混入其他 Plan 或阶段成果")
            matches = []
            for evidence_ref in payload["evidence_refs"]:
                manifest = store.load_object(evidence_ref)["payload"]
                if manifest.get("evidence_type") != "stage_execution":
                    continue
                validate_operational_evidence(manifest)
                value = manifest["entries"][0]["facts"]
                core = {key: item for key, item in value.items() if key != "origin"}
                if core["stage"] != stage or core["plan_ref"] != dict(plan_ref) or core["result"] != "pass":
                    raise guard.GuardError("材料后继准备的原 physical pass 与 Plan 不一致")
                origin = value["origin"]
                binding, pointer, _freeze_path = self._producer_record(state, core, origin, originals)
                if pointer:
                    raise guard.GuardError("材料后继准备的物理 pass 不能由 freeze core 代替")
                envelope = {"schema_version": guard_epochs.CONTINUITY_V2_SCHEMA,
                    "source_service_instance_id": origin["service_instance_id"], "channel_service_instance_id": state["status"]["guard_identity"]["service_instance_id"],
                    "stable_deployment_identity_sha256": state["registered"].stable_deployment_identity_sha256,
                    "epoch_bindings": state["registrations"], "readiness_bindings": state["readiness"], "registry_head_binding": state["head"],
                    "source_record_binding": binding, "source_json_pointer": "", "current_operations_index_binding": state["inventory"]["operations_index_binding"]}
                guard_epochs.validate_guard_continuity(self.configuration, self.context, envelope, originals.add,
                    source_core=core, source_origin=origin, channel_identity=state["status"]["guard_identity"], purpose="current_execution")
                matches.append(binding)
            if len(matches) != 1:
                raise guard.GuardError("材料后继准备缺唯一完整原物理 producer")
            producer_bindings.extend(matches)
        origin = state["inventory"]["origin"]
        content = {"campaign_id": plan["campaign_id"], "delivery_id": plan["delivery_id"], "operation_index_binding": origin["operations_index_binding"]}
        envelope = {"schema_version": guard_epochs.CONTINUITY_V2_SCHEMA,
            "source_service_instance_id": origin["service_instance_id"], "channel_service_instance_id": state["status"]["guard_identity"]["service_instance_id"],
            "stable_deployment_identity_sha256": state["registered"].stable_deployment_identity_sha256,
            "epoch_bindings": state["registrations"], "readiness_bindings": state["readiness"], "registry_head_binding": state["head"],
            "source_record_binding": origin["operations_index_binding"], "source_json_pointer": "", "current_operations_index_binding": origin["operations_index_binding"]}
        continuity = guard_epochs.validate_guard_continuity(self.configuration, self.context, envelope, originals.add,
            source_core=content, source_origin=origin, channel_identity=state["status"]["guard_identity"], purpose="current_execution")
        materials = self._producer_profiles(state, origin, None, originals)
        proofs = {reference["proof_binding"]["path"]: reference["proof_binding"] for material in materials for reference in material["proof_references"]}
        profile = profile_observations.validate_profile_observation_projection(self.configuration, self.context,
            [proofs[path] for path in sorted(proofs)], originals.add, purpose="formed_replay", producer_materials=materials)
        # bootstrap 不授旧材料的门禁或历史根转换权限；只在本次精确已核
        # import／subject 原件处保留完整父字节，封存后再由真实后继投影闭合。
        deferred = [*material_bindings, *subject_bindings,
            *[self.configuration[role]["qualification"]["sealed_result_binding"] for role in ("candidate", "rollback")]]
        initial = {"operation_index_binding": origin["operations_index_binding"], "producer_bindings": producer_bindings,
            "profile_producer_materials": materials, "profile_proof_bindings": [proofs[path] for path in sorted(proofs)],
            "guard_continuity": envelope, "continuity_originals": [item.as_value() for item in continuity.read_bindings],
            "producer_state_bindings": [state["saved_binding"], state["pointer_binding"]]}
        self._expand_producer_originals(originals, initial, None, profile, deferred_bindings=deferred)
        tail = self._producer_state(originals)
        if (any(tail[key] != state[key] for key in ("status", "inventory", "head", "registrations", "readiness"))
                or chain.valid_refs(store) != delivery_chain_state(store, plan["campaign_id"], plan["delivery_id"]).valid_refs(store)):
            raise guard.GuardError("材料后继准备读取期间原身份、全集或四 pass 漂移")
        originals.revalidate()
        return VerifiedSource(canonical_sha256(content), self.configuration["guard"]["deployment_receipt_binding"]["sha256"], originals,
            canonical_sha256(self.configuration), origin["service_instance_id"], self.data_root,
            profile_projection=profile, guard_continuity=continuity)

    def _producer_state(self, originals: _ProducerOriginals) -> dict[str, Any]:
        """已鉴权通道与真实目录全集分别核验，状态握手不冒充材料签发。"""
        status = self._status()
        guard.exact(status, {"guard_identity", "configuration_sha256", "execution_context", "recovery"}, "当前来源状态")
        if (status["configuration_sha256"] != canonical_sha256(self.configuration)
                or status["recovery"].get("blocked") or status["recovery"].get("unresolved_operation_ids")):
            raise guard.GuardError("原守卫配置不一致或恢复仍未决")
        inventory = self.client.call("inventory", {})
        guard.exact(inventory, {"executions", "restorations", "operations_index_binding", "origin"}, "当前完整 producer 清单")
        expected_origin = {"authority": AUTHORITY, "source_scope": self.configuration["source_scope"],
                           "configuration_sha256": canonical_sha256(self.configuration),
                           "service_instance_id": status["guard_identity"]["service_instance_id"],
                           "operations_index_binding": inventory["operations_index_binding"]}
        if inventory["origin"] != expected_origin:
            raise guard.GuardError("当前操作索引没有来自原已鉴权守卫通道")
        root = guard.managed_path(self.data_root, self.configuration["paths"]["guard_root_relative"])
        saved_binding = originals.binding(root / "configuration.json")
        saved = parse_json_bytes(originals.add(saved_binding))
        if saved != {"configuration": self.configuration, "context": status["execution_context"]}:
            raise guard.GuardError("原守卫保存配置或稳定上下文漂移")
        registrations, readiness = [], []
        directory = root / "epochs"
        for number, path in enumerate(sorted(directory.iterdir()), 1):
            if path.is_symlink() or not path.is_dir() or path.name != f"{number:06d}":
                raise guard.GuardError("实际 epoch 全集缺失、乱序或含错误目录")
            names = {item.name for item in path.iterdir()}
            if not {"registration.json"} <= names <= {"registration.json", "readiness.json"}:
                raise guard.GuardError("实际 epoch 缺注册或含未知原件")
            registrations.append(originals.binding(path / "registration.json"))
            if "readiness.json" in names:
                readiness.append(originals.binding(path / "readiness.json"))
        registered = guard_epochs.validate_registration_chain(self.configuration, self.context, registrations,
            originals.add, channel_identity=status["guard_identity"])
        pointer_binding = originals.binding(root / "epoch-registry/head.json")
        pointer = parse_json_bytes(originals.add(pointer_binding))
        fields = {"schema_version", "snapshot_binding"}
        if pointer.get("schema_version") == guard.REGISTRY_POINTER_SCHEMA:
            fields.add("append_intent_binding")
        elif pointer.get("schema_version") != guard.REGISTRY_POINTER_V1_SCHEMA:
            raise guard.GuardError("实际注册头游标版本非法")
        guard.exact(pointer, fields, "实际注册头游标")
        for item in guard.all_bindings(pointer):
            originals.add(item)
        guard_epochs.validate_registry_head(self.configuration, self.context, registered, pointer["snapshot_binding"], originals.add)
        for item in readiness:
            guard_epochs.validate_epoch_readiness(self.configuration, self.context, registered, item, originals.add)
        index = parse_json_bytes(originals.add(inventory["operations_index_binding"]))
        guard.exact(index, {"schema_version", "configuration_sha256", "operations"}, "实际完整操作索引")
        if (index["schema_version"] != guard_epochs.INDEX_SCHEMA
                or index["configuration_sha256"] != canonical_sha256(self.configuration)
                or not isinstance(index["operations"], list)):
            raise guard.GuardError("完整索引版本、配置或操作集合非法")
        ids, executions, restorations = [], [], []
        for row in index["operations"]:
            guard.exact(row, guard_epochs.INDEX_OPERATION_FIELDS, "实际操作索引条目")
            operation_id = row["operation_id"]
            if not isinstance(operation_id, str) or not guard_epochs.SAFE_OPERATION_ID.fullmatch(operation_id):
                raise guard.GuardError("实际操作坐标非法")
            ids.append(operation_id)
            operation = root / "operations" / operation_id
            if row["prepared_binding"]["path"] != operation.relative_to(self.data_root).as_posix() + "/prepared.json":
                raise guard.GuardError("实际操作索引 prepared 坐标错误")
            for item in guard.all_bindings(row):
                originals.add(item)
            for pattern, values in (("evidence-*.json", executions), ("restoration-core-*.json", restorations)):
                for path in sorted(operation.glob(pattern)):
                    if path.is_symlink() or not re.fullmatch(r"(?:evidence|restoration-core)-[0-9]{6}\.json", path.name):
                        raise guard.GuardError("实际 producer 文件坐标非法")
                    values.append(parse_json_bytes(originals.add(originals.binding(path))))
        actual_ids = sorted(path.name for path in (root / "operations").iterdir())
        if ids != sorted(set(ids)) or actual_ids != ids or executions != inventory["executions"] or restorations != inventory["restorations"]:
            raise guard.GuardError("已鉴权 inventory 没有闭合真实完整操作及 producer 原件")
        return {"status": status, "inventory": inventory, "registered": registered,
                "registrations": registrations, "readiness": readiness, "head": pointer["snapshot_binding"],
                "saved_binding": saved_binding, "pointer_binding": pointer_binding, "index": index}

    def _producer_record(self, state: Mapping[str, Any], content: Mapping[str, Any], origin: Mapping[str, Any],
                         originals: _ProducerOriginals) -> tuple[dict[str, Any], str, Path | None]:
        """来源先逐字匹配真实完整 producer，再核固定坐标，禁止同 SHA 换址。"""
        root = guard.managed_path(self.data_root, self.configuration["paths"]["guard_root_relative"])
        expected = {**dict(content), "origin": dict(origin)}
        matches = []
        for value in [*state["inventory"]["executions"], *state["inventory"]["restorations"]]:
            if value != expected:
                continue
            operation_id = content.get("operation_id")
            if content.get("contract_version") == "claude-delivery-stage-execution/v1":
                path = root / "operations" / str(operation_id) / "evidence-000001.json"
            elif content.get("contract_version") == "claude-delivery-restore/v1":
                terminal = content.get("terminal_binding", {}).get("path", "")
                match = re.fullmatch(re.escape(root.relative_to(self.data_root).as_posix()) + r"/operations/" + re.escape(str(operation_id)) + r"/terminals/([0-9]{6})\.json", terminal)
                if match is None:
                    raise guard.GuardError("原恢复 producer 的 terminal 坐标非法")
                path = root / "operations" / str(operation_id) / ("restoration-core-" + match[1] + ".json")
            else:
                raise guard.GuardError("已鉴权清单中的完整 producer 类型未登记")
            item = originals.binding(path)
            if parse_json_bytes(originals.add(item)) != expected:
                raise guard.GuardError("实际 producer 与完整已鉴权 inventory 不一致")
            matches.append((item, "", None))
        if not matches and set(content) == {"campaign_id", "delivery_id", "operation_index_binding"}:
            current = state["inventory"]
            derived = {"campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"],
                       "operation_index_binding": current["operations_index_binding"]}
            if dict(content) == derived and dict(origin) == current["origin"]:
                matches.append((current["operations_index_binding"], "", None))
        if not matches and (root / "freezes").exists():
            for path in sorted((root / "freezes").glob("*/*/snapshot.json")):
                relative = path.relative_to(self.data_root).as_posix()
                if not re.fullmatch(re.escape(root.relative_to(self.data_root).as_posix()) + r"/freezes/[0-9a-f]{64}/[0-9]{6}/snapshot\.json", relative):
                    raise guard.GuardError("原 freeze snapshot 固定坐标非法")
                item = originals.binding(path)
                snapshot = parse_json_bytes(originals.add(item))
                guard.exact(snapshot, {"environment", "rollback", "gates", "cores"}, "原 freeze snapshot")
                guard.exact(snapshot["cores"], EVIDENCE_TYPES, "原 freeze core 闭集")
                for name, value in snapshot["cores"].items():
                    if value == expected:
                        matches.append((item, "/cores/" + name, path))
        if len(matches) != 1:
            raise guard.GuardError("请求来源没有唯一完整 producer 或原 freeze snapshot")
        return matches[0]

    def _producer_profiles(self, state: Mapping[str, Any], origin: Mapping[str, Any], freeze_path: Path | None,
                           originals: _ProducerOriginals) -> list[dict[str, Any]]:
        """只消费来源索引明确列出的原 producer 和 Profile 派生证明。"""
        materials = []
        def add(phase: str, producer: Any, observation: Any, document: Any, prefix: str) -> None:
            references = profile_observations.select_profile_proof_references(document, prefix, originals.add, configuration=self.configuration)
            materials.append({"collection_phase": phase, "producer_binding": producer,
                              "observation_binding": observation, "proof_references": references})
        if freeze_path is not None:
            item = originals.binding(freeze_path)
            add("freeze", item, None, parse_json_bytes(originals.add(item)), "/cores/freeze/before_bindings/")
        source_index = parse_json_bytes(originals.add(origin["operations_index_binding"]))
        current = {row["operation_id"]: row["prepared_binding"] for row in state["index"]["operations"]}
        for row in source_index["operations"]:
            guard.exact(row, guard_epochs.INDEX_OPERATION_FIELDS, "原 Profile producer 索引")
            if current.get(row["operation_id"]) != row["prepared_binding"]:
                raise guard.GuardError("原 Profile producer 不属于完整当前索引")
            prepared = parse_json_bytes(originals.add(row["prepared_binding"]))
            add("before", row["prepared_binding"], None, prepared, "/before_bindings/")
            prefix = str(PurePosixPath(row["prepared_binding"]["path"]).parent)
            for item in row["bindings"]:
                if item["path"] == prefix + "/observation.json":
                    add("execution", row["prepared_binding"], item, parse_json_bytes(originals.add(item)), "/during_bindings/")
                elif PurePosixPath(item["path"]).parent.as_posix() == prefix + "/terminals":
                    terminal = parse_json_bytes(originals.add(item))
                    if terminal.get("status") == "restored":
                        observation = terminal["restoration"]["bindings"][0]
                        if observation not in row["bindings"]:
                            raise guard.GuardError("原恢复 Profile 观测未列入完整索引")
                        add("recovery", item, observation, parse_json_bytes(originals.add(observation)), "/bindings/")
        return sorted(materials, key=canonical_sha256)

    @staticmethod
    def _expand_producer_originals(originals: _ProducerOriginals, initial: Any, gate_projection: Any, profile_projection: Any,
                                   *, deferred_bindings: Any = ()) -> None:
        """按同一严格投影规则逐项取得闭集，再交既有校验器独立复核。"""
        from tools.official_client_control.delivery_execution import typed_bindings
        deferred = {item["path"]: item for item in typed_bindings(deferred_bindings)}
        if gate_projection is not None and deferred:
            raise guard.GuardError("真实后继投影不能保留 bootstrap 材料递归边界")
        pending = typed_bindings([initial, list(gate_projection.archive_bindings) if gate_projection is not None else []])
        seen = set()
        while pending:
            item = pending.pop()
            raw = originals.add(item)
            if item["path"] in seen:
                continue
            seen.add(item["path"])
            projected = gate_projection.project(item, raw) if gate_projection is not None else None
            if projected is None and profile_projection is not None:
                projected = profile_projection.project(item, raw)
            if projected is None and item["path"] in deferred:
                if deferred[item["path"]] != item:
                    raise guard.GuardError("bootstrap 材料原件的精确递归边界漂移")
                projected = []
            if projected is None:
                try:
                    projected = typed_bindings(json.loads(raw))
                except (ValueError, UnicodeDecodeError):
                    projected = []
            pending.extend(projected)
            if len(seen) > 20000:
                raise guard.GuardError("producer 闭集超出固定文件数")

    def verify_recorded_origin_with_material_successor(self, store: Any, items: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """单项和批量的固定产品入口；无后继关联时维持原 export 合同。"""
        from tools.official_client_control.delivery_execution import VerifiedSource
        from tools.official_client_control.qualification_material_import import (
            describe_material_successor_bindings, resolve_material_successor, _register_material_successor_provider,
        )
        from tools.official_client_control.sealed_gate_bindings import VerifiedGateProjection, project_delivery_raw_bindings
        if not isinstance(items, list) or not 1 <= len(items) <= guard.MAX_EXPORT_ITEMS:
            raise guard.GuardError("批量来源项数超出固定上限")
        for item in items:
            guard.exact(item, {"content", "origin"}, "材料后继来源项")
        if len({canonical_sha256(item) for item in items}) != len(items):
            raise guard.GuardError("材料后继来源包含重复请求")
        descriptor = describe_material_successor_bindings(store, self.configuration)
        if descriptor is None:
            return None
        originals = _ProducerOriginals(self.data_root)
        self._material_originals(store, originals, descriptor)
        state = self._producer_state(originals)
        _register_material_successor_provider(self, originals, state)
        if state["status"]["guard_identity"] != descriptor["facts"]["guard_identity"]:
            raise guard.GuardError("材料后继关联不能由另一个守卫实例承接")
        results = []
        previous_projection, previous_source_instance = None, None
        for request in items:
            content, origin = request["content"], request["origin"]
            item, pointer, freeze_path = self._producer_record(state, content, origin, originals)
            envelope = {"schema_version": guard_epochs.CONTINUITY_V2_SCHEMA,
                "source_service_instance_id": origin["service_instance_id"],
                "channel_service_instance_id": state["status"]["guard_identity"]["service_instance_id"],
                "stable_deployment_identity_sha256": state["registered"].stable_deployment_identity_sha256,
                "epoch_bindings": state["registrations"], "readiness_bindings": state["readiness"],
                "registry_head_binding": state["head"], "source_record_binding": item, "source_json_pointer": pointer,
                "current_operations_index_binding": state["inventory"]["operations_index_binding"]}
            continuity = guard_epochs.validate_guard_continuity(self.configuration, self.context, envelope, originals.add,
                source_core=content, source_origin=origin, channel_identity=state["status"]["guard_identity"], purpose="current_execution")
            materials = self._producer_profiles(state, origin, freeze_path, originals)
            proofs = {reference["proof_binding"]["path"]: reference["proof_binding"] for material in materials for reference in material["proof_references"]}
            profile = profile_observations.validate_profile_observation_projection(self.configuration, self.context,
                [proofs[path] for path in sorted(proofs)], originals.add, purpose="formed_replay", producer_materials=materials)
            # 只承接本批上一项真实 selector 的 reader 纯结果；来源与完整用途核验
            # 每项照常执行。现有 reader scope 退出或规格不同会自动重开真实 reader。
            prior = previous_projection
            successor = getattr(prior, "successor", None)
            if (not isinstance(prior, VerifiedGateProjection) or successor is None
                    or previous_source_instance != origin["service_instance_id"]
                    or successor.reference != descriptor["reference"]
                    or successor.facts["plan_ref"] != descriptor["facts"]["plan_ref"]
                    or successor.facts["configuration_binding"] != self.context["configuration_ref"]
                    or successor.facts["configuration_sha256"] != canonical_sha256(self.configuration)
                    or successor.facts["guard_identity"] != state["status"]["guard_identity"]
                    or successor.facts["execution_context"] != state["status"]["execution_context"]
                    or successor.purpose != "formed_replay"):
                prior = None
            source = VerifiedSource(canonical_sha256(content), self.configuration["guard"]["deployment_receipt_binding"]["sha256"],
                originals, canonical_sha256(self.configuration), origin["service_instance_id"], self.data_root,
                profile_projection=profile, guard_continuity=continuity, gate_projection=prior)
            projection = resolve_material_successor(store, self.configuration, source, purpose="formed_replay")
            if projection is None or projection.successor.reference != descriptor["reference"]:
                raise guard.GuardError("已取件后材料后继关联消失或换代")
            initial = {"content": content, "origin": origin, "profile_producer_materials": materials,
                "profile_proof_bindings": [proofs[path] for path in sorted(proofs)], "configuration_binding": self.context["configuration_ref"],
                "guard_continuity": envelope, "producer_state_bindings": [state["saved_binding"], state["pointer_binding"]],
                "continuity_originals": [value.as_value() for value in continuity.read_bindings]}
            self._expand_producer_originals(originals, initial, projection, profile)
            project_delivery_raw_bindings(source, initial, gate_projection=projection, profile_projection=profile)
            previous_projection, previous_source_instance = projection, source.service_instance_id
            results.append({"content_sha256": source.content_sha256, "deployment_sha256": source.deployment_sha256,
                "bytes_by_path": originals, "configuration_sha256": source.configuration_sha256,
                "service_instance_id": source.service_instance_id, "trusted_data_root": self.data_root,
                "profile_projection": profile, "guard_continuity": continuity, "gate_projection": projection})
        tail = self._producer_state(originals)
        if canonical_sha256({key: state[key] for key in ("status", "inventory", "registrations", "readiness", "head")}) != canonical_sha256({key: tail[key] for key in ("status", "inventory", "registrations", "readiness", "head")}):
            raise guard.GuardError("当前来源读取期间守卫、操作全集或注册头漂移")
        if describe_material_successor_bindings(store, self.configuration) != descriptor:
            raise guard.GuardError("当前来源读取期间材料关联或有效工具头漂移")
        originals.revalidate()
        for result in results:
            source = VerifiedSource(**result)
            current = resolve_material_successor(store, self.configuration, source, purpose="formed_replay")
            if current is None or current.successor.reference != descriptor["reference"]:
                raise guard.GuardError("来源尾验没有同一完整材料后继")
            result["gate_projection"] = current
        # 固定首次报告存在后，正式单项／批量与后续 Store 消费都接同一当前
        # 实测入口；报告 JSON 本身不能把陌生字节放入受管来源 Mapping。
        self._check_completed_report(store, results[0]["gate_projection"], tail, originals,
            source=VerifiedSource(**results[0]), require_existing=False)
        final_state = self._producer_state(originals)
        if (final_state["status"] != state["status"] or final_state["inventory"] != state["inventory"]
                or final_state["head"] != state["head"]
                or describe_material_successor_bindings(store, self.configuration) != descriptor):
            raise guard.GuardError("报告消费尾验发现守卫、完整索引或材料关联漂移")
        for request, result in zip(items, results):
            binding, pointer, _ = self._producer_record(final_state, request["content"], request["origin"], originals)
            continuity = result["guard_continuity"]
            if binding != continuity.source_record_binding.as_value() or pointer != json.loads(continuity.envelope_bytes)["source_json_pointer"]:
                raise guard.GuardError("来源尾验的唯一 producer 坐标或 snapshot pointer 漂移")
        originals.revalidate()
        # 报告诊断写相位已使先前能力过期；用实际尾状态为后续只读消费重新登记。
        _register_material_successor_provider(self, originals, final_state)
        return results

    def verify_origin_evidence(self, content: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
        selected = (self.verify_recorded_origin_with_material_successor(self._successor_store(), [{"content": dict(content), "origin": dict(origin)}])
                    if self.configuration["schema_version"] == MATERIAL_CONFIGURATION_SCHEMA else None)
        if selected is not None:
            return selected[0]
        raw, local = self._call_source_export("export", {"content": dict(content), "origin": dict(origin)})
        originals = (self._decode_local_source_originals(raw, content, origin)
                     if local else self._decode_source_originals(raw["bytes_by_path"]))
        paths: set[str] = set()
        result = self._verify_origin_export(content, origin, raw, originals, [], local_paths=paths if local else None)
        if local:
            originals.require_closure(paths)
        return result

    def _call_source_export(self, action: str, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """仅精确的已鉴权旧服务未知操作响应允许回退；坏清单不能降级。"""
        if self.configuration["schema_version"] == MATERIAL_CONFIGURATION_SCHEMA:
            try:
                return self.client.call(action + "_local", request), True
            except guard.GuardError as error:
                # GuardClient 先核响应 HMAC，再抛出服务端的精确错误。
                if str(error) != "未定义的守卫操作":
                    raise
        return self.client.call(action, request), False

    @staticmethod
    def _decode_source_originals(encoded_originals: Any) -> dict[str, bytes]:
        if not isinstance(encoded_originals, dict):
            raise guard.GuardError("来源原件必须为路径与原字节映射")
        originals = {}
        total = 0
        for path, encoded in encoded_originals.items():
            validate_relative_path(path, "delivery.source.path")
            raw = base64.b64decode(encoded, validate=True)
            total += len(raw)
            if total > guard.MAX_RPC_BYTES // 2:
                raise guard.GuardError("来源原件并集超出固定通道上限")
            originals[path] = raw
        return originals

    def _check_local_export_headers(self, raw: Mapping[str, Any], content: Mapping[str, Any],
                                    origin: Mapping[str, Any]) -> None:
        """实读清单前先核本机通道、原冻结根和完整来源坐标，拒绝 JSON 换根。"""
        self._identity(raw.get("guard_identity"))
        if (raw.get("originals_schema") != guard.LOCAL_ORIGINALS_SCHEMA
                or raw.get("trusted_data_root") != str(self.data_root)
                or raw.get("configuration_sha256") != canonical_sha256(self.configuration)
                or raw.get("profile_configuration_binding") != self.context["configuration_ref"]
                or raw.get("deployment_sha256") != self.configuration["guard"]["deployment_receipt_binding"]["sha256"]
                or raw.get("content_sha256") != canonical_sha256(content)
                or raw.get("service_instance_id") != origin.get("service_instance_id")):
            raise guard.GuardError("本地原件清单不属于同一已鉴权来源、冻结根或守卫实例")

    def _decode_local_source_originals(self, raw: Mapping[str, Any], content: Mapping[str, Any],
                                      origin: Mapping[str, Any]) -> _LocalSourceOriginals:
        guard.exact(raw, LOCAL_SOURCE_METADATA_FIELDS | {"local_original_bindings", "local_original_bindings_sha256"}, "本地来源响应")
        self._check_local_export_headers(raw, content, origin)
        return _LocalSourceOriginals(self.data_root, raw.get("local_original_bindings"),
                                    raw.get("local_original_bindings_sha256"))

    def verify_origin_evidence_batch(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """独立验证同次批量原件；不消费守卫自报通过或共享其投影对象。"""
        selected = (self.verify_recorded_origin_with_material_successor(self._successor_store(), items)
                    if self.configuration["schema_version"] == MATERIAL_CONFIGURATION_SCHEMA else None)
        if selected is not None:
            return selected
        if not isinstance(items, list) or not 1 <= len(items) <= guard.MAX_EXPORT_ITEMS:
            raise guard.GuardError("批量来源项数超出固定上限")
        for item in items:
            guard.exact(item, {"content", "origin"}, "批量来源项")
        digests = [canonical_sha256(item) for item in items]
        if len(set(digests)) != len(digests):
            raise guard.GuardError("批量来源包含重复项")
        raw, local = self._call_source_export("export_batch", {"items": items})
        transport_fields = ({"originals_schema", "local_original_bindings", "local_original_bindings_sha256"}
                            if local else {"bytes_by_path"})
        guard.exact(raw, {"schema_version", "requests_sha256", "items"} | transport_fields, "批量来源响应")
        if (raw["schema_version"] != (guard.LOCAL_EXPORT_BATCH_SCHEMA if local else guard.EXPORT_BATCH_SCHEMA)
                or raw["requests_sha256"] != canonical_sha256(items)
                or not isinstance(raw["items"], list) or len(raw["items"]) != len(items)):
            raise guard.GuardError("批量来源响应未闭合本次精确请求全集")
        if local:
            if raw["originals_schema"] != guard.LOCAL_ORIGINALS_SCHEMA:
                raise guard.GuardError("本地批量原件版本不属于已鉴权清单合同")
            channel = None
            for item, response, digest in zip(items, raw["items"], digests):
                guard.exact(response, LOCAL_SOURCE_METADATA_FIELDS | {"request_sha256"}, "本地批量逐项来源响应")
                self._check_local_export_headers(response, item["content"], item["origin"])
                if (response["request_sha256"] != digest
                        or channel is not None and channel != response["guard_identity"]):
                    raise guard.GuardError("本地批量来源未闭合精确请求或同一当前守卫通道")
                channel = response["guard_identity"]
            originals = _LocalSourceOriginals(self.data_root, raw["local_original_bindings"], raw["local_original_bindings_sha256"])
        else:
            originals = self._decode_source_originals(raw["bytes_by_path"])
        paths: set[str] = set()
        projections: list[Any] = []
        results = []
        channel = None
        for item, response, digest in zip(items, raw["items"], digests):
            if response.get("request_sha256") != digest:
                raise guard.GuardError("批量来源响应与完整内容及来源不对应")
            if channel is not None and channel != response.get("guard_identity"):
                raise guard.GuardError("批量来源不是同一当前守卫通道")
            channel = response.get("guard_identity")
            results.append(self._verify_origin_export(item["content"], item["origin"], response, originals, projections,
                                                      local_paths=paths if local else None))
        if local:
            originals.require_closure(paths)
        return results

    def _verify_origin_export(self, content: Mapping[str, Any], origin: Mapping[str, Any],
                              raw: Mapping[str, Any], originals: Mapping[str, bytes],
                              projections: list[Any], *, local_paths: set[str] | None = None) -> dict[str, Any]:
        self._identity(raw["guard_identity"])
        if raw["content_sha256"] != canonical_sha256(content) or raw["deployment_sha256"] != self.configuration["guard"]["deployment_receipt_binding"]["sha256"]:
            raise guard.GuardError("受管来源返回的内容或部署摘要不匹配")
        for item in guard.all_bindings({"content": content, "origin": origin}):
            data = originals.get(item["path"])
            if data is None or len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise guard.GuardError("受管来源原件不闭合")
        if raw["configuration_sha256"] != canonical_sha256(self.configuration) or raw["trusted_data_root"] != str(self.data_root) or raw["service_instance_id"] != origin["service_instance_id"]:
            raise guard.GuardError("认证通道的来源配置、实例或受管根不一致")
        if raw.get("profile_configuration_binding") != self.context["configuration_ref"]:
            raise guard.GuardError("认证通道缺同一冻结配置的 Profile 来源合同")

        def read_profile_original(item: Mapping[str, Any]) -> bytes:
            data = originals.get(item["path"])
            if data is None:
                raise guard.GuardError("Profile 来源合同缺 guard 已导出的精确原件")
            return data

        profile_projection = profile_observations.validate_profile_observation_projection(
            self.configuration, self.context, raw.get("profile_proof_bindings"), read_profile_original,
            purpose="formed_replay", producer_materials=raw.get("profile_producer_materials"))
        if raw.get("guard_continuity", {}).get("schema_version") != guard_epochs.CONTINUITY_V2_SCHEMA:
            raise guard.GuardError("当前守卫通道缺注册全集头锚点，不能降级消费旧连续性")
        continuity = guard_epochs.validate_guard_continuity(
            self.configuration, self.context, raw.get("guard_continuity"), read_profile_original,
            source_core=content, source_origin=origin, channel_identity=raw["guard_identity"],
            purpose="current_execution")
        gate_projection = None
        if self.configuration.get("schema_version") == MATERIAL_CONFIGURATION_SCHEMA:
            from tools.official_client_control.delivery_execution import VerifiedSource
            from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
            from tools.official_client_control.qualification_material_import import projection_for_configuration
            from tools.official_client_control.store import ControlStore
            source = VerifiedSource(raw["content_sha256"], raw["deployment_sha256"], originals,
                raw["configuration_sha256"], raw["service_instance_id"], self.data_root,
                profile_projection=profile_projection, guard_continuity=continuity)
            q = self.configuration["candidate"]["qualification"]
            store = ControlStore.open_read_only(guard.managed_path(self.data_root, q["store_relative"]))
            if not projections:
                projections.append(projection_for_configuration(store, self.configuration, source, purpose="formed_replay"))
            gate_projection = projections[0]
            initial = {"content": content, "origin": origin,
                       "profile_proof_bindings": list(profile_projection.proof_bindings),
                       "profile_producer_materials": list(profile_projection.producer_materials),
                       "configuration_binding": self.context["configuration_ref"],
                       "guard_continuity": raw["guard_continuity"],
                       "continuity_originals": [item.as_value() for item in continuity.read_bindings]}
            bindings = project_delivery_raw_bindings(source, initial,
                gate_projection=gate_projection, profile_projection=profile_projection)
            for item in bindings:
                if local_paths is not None:
                    local_paths.add(item["path"])
                data = originals.get(item["path"])
                if data is None or len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise guard.GuardError("受管通道遗漏门禁第二根投影的真实原件")
        return {"content_sha256": raw["content_sha256"], "deployment_sha256": raw["deployment_sha256"], "bytes_by_path": originals, "configuration_sha256": raw["configuration_sha256"], "service_instance_id": raw["service_instance_id"], "trusted_data_root": self.data_root,
                "profile_projection": profile_projection, "guard_continuity": continuity,
                "gate_projection": gate_projection}


def create_executor(name: str, configuration: dict[str, Any], execution_context: dict[str, Any]) -> Arm64DeliveryExecutor:
    if name != "arm64-isolated":
        raise guard.GuardError("只提供固定arm64-isolated执行器能力；不动态import")
    return Arm64DeliveryExecutor(configuration, execution_context)


def verify_origin_evidence(executor: Arm64DeliveryExecutor, content: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """固定来源通道能力；调用者须由可信登记选中实例，不从证据创建配置。"""
    if not isinstance(executor, Arm64DeliveryExecutor):
        raise guard.GuardError("来源读取须使用固定执行器实例")
    return executor.verify_origin_evidence(content, origin)


def verify_origin_evidence_batch(executor: Arm64DeliveryExecutor, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量只读来源同样只接受可信登记选中的固定执行器实例。"""
    if not isinstance(executor, Arm64DeliveryExecutor):
        raise guard.GuardError("批量来源读取须使用固定执行器实例")
    return executor.verify_origin_evidence_batch(items)
