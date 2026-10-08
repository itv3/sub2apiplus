"""受管原生 helper 的构建重验、旧 v1 重放与有限 owned-v2 生产接线。

本版不能确认唯一 boot、mute、实例边界与尾部排空；诊断与采集入口在任何
目录创建、子进程或 ES 调用前拒绝。新增 v2 的运行方法显式接固定 helper，
纯 plan/重放不执行；外部原件即使摘要正确，也只证明结构，不授正式来源。
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture.claude_mac_process_events_contract import (
    BUILD_SCHEMA, CAPTURE_SCHEMA, PROJECTION_DESCRIPTOR, MaterialBinding, NativeBarrier,
    NativeBuildIdentity, NativeCaptureResult, NativeContext, NativeProcessSourceError,
    NativeProcessSourceUnavailable, NativeReadiness, canonical_json, context_digest,
    exact, fingerprint, require, strict_json, utc, validate_native_projection,
)


BUILD_FIELDS = frozenset({
    "schema_version", "source_files", "source_files_sha256", "projection_contract_sha256",
    "compiler", "sdk", "architecture", "deployment_target", "compile_argv", "link_argv",
    "framework_bindings", "unsigned_outputs", "signing", "final_outputs", "observed_at_utc",
})
CAPTURE_FIELDS = frozenset({
    "schema_version", "context", "context_sha256", "build_identity_sha256", "origin", "files",
    "process_events_complete", "executed_image_verified", "formal_ready", "launch_allowed", "registration_verified",
})


def _canonical_path(path: Path) -> Path:
    require(path.is_absolute() and ".." not in path.parts and str(path) == str(path.resolve()), "原件路径须为规范绝对路径")
    for item in (path, *path.parents):
        require(not item.is_symlink(), "原件路径包含 symlink")
    return path


def _root(path: Path) -> Path:
    _canonical_path(path)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "原件根须为当前用户私有 0700 目录")
    return path


def _binding(value: Any) -> MaterialBinding:
    exact(value, {"path", "sha256", "bytes"})
    return MaterialBinding(**value)


def read_bound(binding: MaterialBinding, *, mode: int | None = None, private: bool = False,
               maximum: int = 67_108_864) -> bytes:
    """实际读字节并核 inode、mtime、ctime、权限；不接受 FIFO／目录／路径替换。

    SDK／工具链原始系统文件允许现有硬链接；本任务封存原件要求单链接与所有者。
    """
    path = _canonical_path(Path(binding.path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, "原件不是有界普通文件")
        if mode is not None:
            require(stat.S_IMODE(before.st_mode) == mode, "原件权限变化")
        if private:
            require(before.st_uid == os.geteuid() and before.st_nlink == 1, "私有原件所有者或硬链接数非法")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(1_048_576, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            require(size <= maximum, "原件读取期间超过字节上界")
        after = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid", "st_nlink")
        require(all(getattr(before, name) == getattr(after, name) == getattr(current, name) for name in fields), "原件读取期间变化")
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    require(len(raw) == binding.bytes and hashlib.sha256(raw).hexdigest() == binding.sha256, "原件字节或摘要不符")
    return raw


def _materials(values: Any) -> list[MaterialBinding]:
    require(type(values) is list and 0 < len(values) <= 8192, "构建读集须为有界非空清单")
    bindings = [_binding(item) for item in values]
    require(len({item.path for item in bindings}) == len(bindings), "构建读集重复路径")
    for item in bindings:
        read_bound(item, maximum=536_870_912)
    return bindings


def _macho_signature(raw: bytes) -> bool:
    """实读 Mach-O 头、构建目标及命令边界；不能只信输出清单的架构字段。"""
    require(len(raw) >= 32, "原生构建物缺少 Mach-O 头")
    magic, cpu, _subtype, filetype, count, command_bytes, _flags, _reserved = struct.unpack_from("<8I", raw)
    require(magic == 0xFEEDFACF and cpu == 0x0100000C and filetype == 2
            and 0 < count <= 4096 and 32 + command_bytes <= len(raw), "原生构建物不是有界 arm64 可执行 Mach-O")
    offset, signed, build_seen = 32, False, False
    for _ in range(count):
        require(offset + 8 <= 32 + command_bytes, "Mach-O 命令头截短")
        command, size = struct.unpack_from("<2I", raw, offset)
        require(size >= 8 and size % 8 == 0 and offset + size <= 32 + command_bytes, "Mach-O 命令越界")
        if command == 0x32:
            require(not build_seen and size >= 24, "Mach-O 构建版本命令缺失或重复")
            target, minimum, sdk, _tools = struct.unpack_from("<4I", raw, offset + 8)
            require(target == 1 and minimum == 0x000F0000 and sdk == 0x000F0500, "实际 Mach-O 最低目标或 SDK 与冻结值不同")
            build_seen = True
        elif command == 0x1D:
            require(size == 16 and not signed, "代码签名命令非法或重复")
            start, length = struct.unpack_from("<2I", raw, offset + 8)
            require(length > 0 and start + length <= len(raw), "代码签名数据越界")
            signed = True
        offset += size
    require(offset == 32 + command_bytes and build_seen, "Mach-O 命令总长不符或缺冻结构建版本")
    return signed


def verify_native_build(receipt_path: Path, *, native_root: Path,
                        expected_receipt: MaterialBinding) -> NativeBuildIdentity:
    """重读实际源码／header／工具链／链接输入及两输出；通过仍只是一份未资格化构建。"""
    root = _root(native_root)
    require(receipt_path.parent == root and expected_receipt.path == str(receipt_path), "构建收据未绑定当前私有根")
    raw = read_bound(expected_receipt, mode=0o600, private=True)
    receipt = strict_json(raw)
    exact(receipt, BUILD_FIELDS)
    require(receipt["schema_version"] == BUILD_SCHEMA and receipt["architecture"] == "arm64"
            and receipt["deployment_target"] == "15.0", "构建 schema／架构／最低目标版本非法")
    utc(receipt["observed_at_utc"])
    sources = _materials(receipt["source_files"])
    expected_names = {"claude_mac_process_events_native.m", "claude_mac_process_events_contract.py",
                      "claude_mac_process_events.py", "claude_mac_process_events_build.py"}
    require({Path(item.path).name for item in sources} == expected_names and len(sources) == 4, "构建源文件不属于本批闭集")
    require(receipt["source_files_sha256"] == fingerprint(receipt["source_files"])
            and receipt["projection_contract_sha256"] == fingerprint(PROJECTION_DESCRIPTOR), "源码或投影合同摘要不符")
    compiler, sdk, signing = receipt["compiler"], receipt["sdk"], receipt["signing"]
    exact(compiler, {"materials", "versions", "driver_traces", "linker_trace_status"})
    _materials(compiler["materials"])
    _materials(compiler["driver_traces"])
    exact(compiler["versions"], {"clang", "linker", "lipo", "otool"})
    require(all(isinstance(v, str) and 0 < len(v) <= 65536 for v in compiler["versions"].values()), "工具链版本输出非法")
    require(compiler["linker_trace_status"] == "retained_actual_trace_unqualified", "链接读集不能自报完整资格")
    exact(sdk, {"requested_path", "resolved_path", "version", "materials", "alias_verified_before_after", "read_set_status"})
    require(sdk["version"] == "15.5" and sdk["alias_verified_before_after"] is True
            and Path(sdk["requested_path"]).resolve() == Path(sdk["resolved_path"]), "SDK 别名、目标或版本变化")
    require(sdk["read_set_status"] == "actual_dependencies_and_link_inputs_unqualified", "构建读集不能授原生运行资格")
    _materials(sdk["materials"])
    _materials(receipt["framework_bindings"])
    for name in ("compile_argv", "link_argv"):
        values = receipt[name]
        require(type(values) is list and len(values) == 2 and all(type(v) is list and v
                and all(isinstance(x, str) and "\x00" not in x for x in v) for v in values), "实际构建参数清单非法")
    exact(signing, {"status", "performed", "entitlements_qualified", "qualification", "signature_inspection"})
    require(signing["performed"] is False and signing["entitlements_qualified"] is False
            and signing["qualification"] == "unqualified_build_only" and signing["status"] in {"unsigned", "ad_hoc_unqualified"}, "本版不能声明合格签名")
    require(type(signing["signature_inspection"]) is list and len(signing["signature_inspection"]) == 2, "签名检查清单非法")
    outputs = {}
    for phase in ("unsigned_outputs", "final_outputs"):
        require(type(receipt[phase]) is list and len(receipt[phase]) == 2, "构建必须有两个固定角色")
        current = {}
        for output in receipt[phase]:
            exact(output, {"role", "material", "mode", "architecture", "code_signature_present"})
            role, material = output["role"], _binding(output["material"])
            require(role in {"collector", "probe_fixture"} and role not in current
                    and type(output["mode"]) is int and output["mode"] == 0o755
                    and output["architecture"] == "arm64" and type(output["code_signature_present"]) is bool
                    and Path(material.path).parent == root, "输出角色、路径、架构或权限非法")
            output_raw = read_bound(material, mode=0o755, private=True)
            require(_macho_signature(output_raw) == output["code_signature_present"], "实际代码签名布局与记录不同")
            current[role] = material
        if outputs:
            require(current == outputs, "未执行签名时两阶段输出不应改变")
        outputs = current
    return NativeBuildIdentity(
        expected_receipt, receipt["source_files_sha256"], receipt["projection_contract_sha256"],
        outputs["collector"], 0o755, "arm64", outputs["probe_fixture"], 0o755,
        fingerprint(sdk), fingerprint(compiler), fingerprint(signing), signing["status"],
    )


def inspect_native_readiness(identity: NativeBuildIdentity, *, diagnostic_root: Path) -> NativeReadiness:
    """本批禁止执行 ES 创建诊断；不写诊断文件、不拉起 helper、不申请权限。"""
    require(isinstance(identity, NativeBuildIdentity) and isinstance(diagnostic_root, Path), "原生诊断参数类型非法")
    raise NativeProcessSourceUnavailable("native_entry_disabled：本版不执行 ES 创建诊断；唯一 boot、mute 与窗口收尾待资格化")


def replay_native_capture(context: NativeContext, bundle_root: Path,
                          expected_manifest: MaterialBinding) -> NativeCaptureResult:
    """只读重放私有原件；拒绝来源自报、额外文件、伪造屏障与身份迁移。"""
    root = _root(bundle_root)
    require(Path(expected_manifest.path).parent == root, "重放清单未绑定当前根")
    manifest_raw = read_bound(expected_manifest, mode=0o600, private=True)
    manifest = strict_json(manifest_raw)
    exact(manifest, CAPTURE_FIELDS)
    require(manifest["schema_version"] == CAPTURE_SCHEMA and manifest["context"] == asdict(context)
            and manifest["context_sha256"] == context_digest(context)
            and manifest["build_identity_sha256"] == context.build_identity_sha256, "重放上下文或构建身份变化")
    require(manifest["origin"] in {"regression_fixture", "supplied_unverified"}, "外部原件不能登记为实际原生来源")
    for name in ("process_events_complete", "executed_image_verified", "formal_ready", "launch_allowed", "registration_verified"):
        require(manifest[name] is False, "离线清单不得自报完整事件或启动权")
    exact(manifest["files"], {"projection", "collector_instance", "ready", "closed"})
    files = {}
    for role, value in manifest["files"].items():
        if value is None:
            require(role in {"ready", "closed"}, "必要原件缺失")
            files[role] = None
            continue
        binding = _binding(value)
        require(Path(binding.path).parent == root and binding.path != expected_manifest.path, "原件必须是本根的独立文件")
        files[role] = binding
    paths = [item.path for item in files.values() if item is not None] + [expected_manifest.path]
    require(len(paths) == len(set(paths)) and {str(p) for p in root.iterdir()} == set(paths), "原件角色复用文件或根内有额外物料")
    raw = read_bound(files["projection"], mode=0o600, private=True, maximum=context.limits.max_total_bytes)
    frames = validate_native_projection(context, raw)
    instance = strict_json(read_bound(files["collector_instance"], mode=0o600, private=True))
    exact(instance, {"schema_version", "collector_run_id", "context_sha256", "origin", "process_events_complete",
                     "executed_image_verified", "formal_ready", "launch_allowed", "registration_verified"})
    require(instance["schema_version"] == "claude-mac-native-collector-instance/internal-v1"
            and instance["collector_run_id"] == context.collector_run_id
            and instance["context_sha256"] == context_digest(context) and instance["origin"] == manifest["origin"], "采集实例原件与上下文不同")
    for name in ("process_events_complete", "executed_image_verified", "formal_ready", "launch_allowed", "registration_verified"):
        require(instance[name] is False, "离线实例不得授可信来源或启动权")
    ready_frames = [value for value in frames if value["record_type"] == "barrier" and value["payload"]["phase"] == "ready"]
    closed_frames = [value for value in frames if value["record_type"] == "closed"]
    for role, matches in (("ready", ready_frames), ("closed", closed_frames)):
        require(len(matches) <= 1 and (files[role] is None) == (not matches), "屏障原件与投影记录缺失情况不符")
        if matches:
            require(read_bound(files[role], mode=0o600, private=True) == canonical_json(matches[0]) + b"\n", "屏障快照不是投影中的原记录")
    for value in frames:
        if value["record_type"] == "barrier":
            require(value["payload"]["collector_instance"] == asdict(files["collector_instance"]), "屏障引用了另一个采集实例")
    reasons = [value["payload"]["reason"] for value in frames if value["record_type"] == "incomplete"]
    if not closed_frames:
        reasons.append("closing_barrier_missing")
    elif closed_frames[0]["payload"]["drain_status"] != "verified":
        reasons.append("drain_unverified")
    status = "incomplete" if reasons else "structurally_valid_fixture"
    if all(value["record_type"] == "diagnostic" for value in frames):
        status = "unavailable"
    return NativeCaptureResult(status, manifest["origin"], context_digest(context), context.build_identity_sha256,
        files["collector_instance"], files["ready"], files["closed"], files["projection"], expected_manifest,
        tuple(sorted(set(reasons))), False, False, False, False, False)


class NativeProcessEventCollector:
    """保留设计服务面；当前没有资格化的 live 分支或调用方解锁开关。"""

    def __init__(self) -> None:
        raise NativeProcessSourceUnavailable("原生窗口尚未资格化，不能直接构造采集器")

    @classmethod
    def open_owned_probe(cls, context: NativeContext, *, identity: NativeBuildIdentity,
                         bundle_root: Path) -> NativeProcessEventCollector:
        require(isinstance(context, NativeContext) and isinstance(identity, NativeBuildIdentity)
                and isinstance(bundle_root, Path), "受控探针参数类型非法")
        raise NativeProcessSourceUnavailable("native_entry_disabled：本版未执行原生窗口验收，拒绝在目录、锁或子进程前启动")

    def await_ready(self) -> NativeBarrier:
        raise NativeProcessSourceUnavailable("本版没有实际原生 ready 屏障")

    def run_owned_probe(self) -> None:
        raise NativeProcessSourceUnavailable("本版没有实际原生 ready，不能启动探针")

    def close(self) -> NativeCaptureResult:
        raise NativeProcessSourceUnavailable("本版没有实际原生窗口，不能补造 closed")

    def __enter__(self) -> NativeProcessEventCollector:
        raise NativeProcessSourceUnavailable("本版没有已创建的原生窗口")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """不可用对象没有句柄或进程，不调用信号或清理其他来源。"""


# owned-v2 使用独立入口；旧 v1 的函数、构建摘要与拒绝行为保持不变。
from tools.official_client_capture.claude_mac_process_events_contract import (
    INCOMPLETE_REASONS, OWNED_CAPTURE_SCHEMA, OWNED_CONTROL_SCHEMA, OWNED_FILE_ROLES,
    OWNED_FLAGS, OWNED_MANIFEST_FIELDS, OWNED_ORIGINS, OWNED_PLAN_SCHEMA, OWNED_REQUEST_SCHEMA,
    OWNED_RESULT_SCHEMA, OWNED_STATUSES, OwnedProbePlan, OwnedProbeReadResult,
    owned_context, owned_plan_shape, owned_relative_binding, validate_owned_probe_projection,
)


def _owned_directory_instance(root: Path) -> dict[str, int]:
    info = _root(root).stat()
    return {"dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}


def create_owned_probe_plan(context: NativeContext, *, identity: NativeBuildIdentity, bundle_root: Path) -> OwnedProbePlan:
    """仅只读核实际构建物料和私有目录，不建账、不 spawn、不读账号。"""
    require(isinstance(context, NativeContext) and isinstance(identity, NativeBuildIdentity), "plan 参数类型非法")
    actual = verify_native_build(Path(identity.build_receipt.path), native_root=Path(identity.helper.path).parent,
                                 expected_receipt=identity.build_receipt)
    require(actual == identity and context.build_identity_sha256 == fingerprint(asdict(identity)), "实际构建身份变化")
    # 新运行计划只接本次受管四源码，不能把同名的外部源码包装为当前 helper。
    receipt_document = strict_json(read_bound(identity.build_receipt, mode=0o600, private=True))
    names = {"claude_mac_process_events_native.m", "claude_mac_process_events_contract.py",
             "claude_mac_process_events.py", "claude_mac_process_events_build.py"}
    expected_paths = {str(Path(__file__).resolve().with_name(name)) for name in names}
    require(type(receipt_document["source_files"]) is list and len(receipt_document["source_files"]) == 4
            and {entry["path"] for entry in receipt_document["source_files"]} == expected_paths
            and fingerprint(receipt_document["source_files"]) == identity.source_files_sha256,
            "owned-v2 构建来源不是当前受管四源码")
    roles = {}
    for name, binding in (("helper", identity.helper), ("probe_fixture", identity.probe_fixture)):
        raw = read_bound(binding, mode=0o755, private=True)
        roles[name] = {"role": "collector" if name == "helper" else name, "material": asdict(binding),
            "mode": 0o755, "architecture": "arm64", "code_signature_present": _macho_signature(raw)}
    value = {"schema_version": OWNED_PLAN_SCHEMA, "context": asdict(context), "context_sha256": context_digest(context),
        "build_receipt": asdict(identity.build_receipt), "build_identity_sha256": context.build_identity_sha256,
        **roles, "bundle_root": str(bundle_root), "bundle_instance": _owned_directory_instance(bundle_root)}
    value["plan_sha256"] = fingerprint(value)
    return OwnedProbePlan(value)


def validate_owned_probe_plan(document: Mapping[str, Any], *, expected_context: NativeContext,
                              identity: NativeBuildIdentity, bundle_root: Path) -> OwnedProbePlan:
    """重新核当前文件与原目录实例；调用方构造 dataclass 不能绕过此步骤。"""
    supplied = OwnedProbePlan(document)
    actual = create_owned_probe_plan(expected_context, identity=identity, bundle_root=bundle_root)
    require(canonical_json(actual.document) == canonical_json(supplied.document), "plan 实际物料或目录实例变化")
    return actual


def _owned_revalidate(plan: OwnedProbePlan) -> None:
    context = owned_plan_shape(plan.document)
    receipt = _binding(plan.document["build_receipt"])
    identity = verify_native_build(Path(receipt.path), native_root=Path(receipt.path).parent, expected_receipt=receipt)
    validate_owned_probe_plan(plan.document, expected_context=context, identity=identity,
                              bundle_root=Path(plan.document["bundle_root"]))


def _owned_write_once(root: Path, name: str, raw: bytes) -> dict[str, Any]:
    """只写一次并 fsync 文件与目录；失败原件留在原位，不覆盖或撤销。"""
    owned_relative_binding({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _owned_read_file(root: Path, relative: Mapping[str, Any], *, maximum: int = 67108864) -> bytes:
    owned_relative_binding(relative)
    return read_bound(MaterialBinding(str(root / relative["path"]), relative["sha256"], relative["bytes"]),
                      mode=0o600, private=True, maximum=maximum)


def _owned_snapshots(frames: tuple[Mapping[str, Any], ...]) -> dict[str, bytes]:
    """快照只摘准确 native 帧/子值，不用当前系统值补齐缺失原件。"""
    values = {}
    for frame in frames:
        kind, payload = frame["record_type"], frame["payload"]
        if kind in {"collector_instance", "closed", "incomplete"}:
            role = "first_error" if kind == "incomplete" else kind
            require(role not in values, "原件角色重复")
            values[role] = canonical_json(frame) + b"\n"
        elif kind == "barrier":
            role = payload["phase"]
            require(role not in values, "屏障角色重复")
            values[role] = canonical_json(frame) + b"\n"
        elif kind == "state_observation":
            for name in ("boot", "mute"):
                role = name + "_" + payload["phase"]
                require(role not in values, "状态原件角色重复")
                values[role] = canonical_json(payload[name + "_observation"]) + b"\n"
        elif kind == "diagnostic":
            values["boot_before"] = canonical_json(payload["boot_observation"]) + b"\n"
    return values


def _owned_summary(plan: OwnedProbePlan, frames: tuple[Mapping[str, Any], ...], files: Mapping[str, Any],
                   origin: str) -> OwnedProbeReadResult:
    reasons = {frame["payload"]["reason"] for frame in frames if frame["record_type"] == "incomplete"}
    diagnostic = bool(frames) and frames[0]["record_type"] == "diagnostic"
    if not diagnostic:
        reasons.add("drain_unverified")
        if files["closing"] is None or files["closed"] is None:
            reasons.add("closing_barrier_missing")
        for frame in frames:
            if frame["record_type"] == "closed":
                if frame["payload"]["unclosed_instances"] > 0:
                    reasons.add("relation_unknown")
                if frame["payload"]["unsubscribe_result"] != 0 or frame["payload"]["delete_result"] != 0:
                    reasons.add("observer_terminated")
    if not frames:
        reasons.add("observer_terminated")
    status = "unavailable" if diagnostic or not frames else "incomplete" if reasons - {"drain_unverified"} else (
        "structurally_valid_fixture" if origin == "regression_fixture" else "observed_unqualified")
    counts = {name: sum(frame["record_type"] == "process_event" and frame["event_type"] == "notify_" + name
                       for frame in frames) for name in ("fork", "exec", "exit")}
    value = {"schema_version": OWNED_RESULT_SCHEMA, "status": status, "origin": origin,
        **{name: plan.document[name] for name in ("plan_sha256", "context_sha256", "build_identity_sha256")},
        "collector_run_id": plan.document["context"]["collector_run_id"],
        **{name: files[name] for name in ("projection", "ready", "closing", "closed", "first_error")},
        "unknown_reasons": sorted(reasons), "record_count": len(frames), "observed_event_counts": counts,
        **{name: False for name in OWNED_FLAGS}}
    return OwnedProbeReadResult(value)


def replay_owned_probe_capture(plan: OwnedProbePlan, bundle_root: Path,
                               expected_manifest: MaterialBinding) -> OwnedProbeReadResult:
    """纯只读核冻结副本和精确投影，不读当前 SDK、不 spawn、不签来源。"""
    require(isinstance(plan, OwnedProbePlan), "replay plan 类型非法")
    owned_plan_shape(plan.document)
    root = _root(bundle_root)
    require(str(root) == plan.document["bundle_root"] and _owned_directory_instance(root) == plan.document["bundle_instance"]
            and Path(expected_manifest.path).parent == root, "replay 根或清单实例变化")
    manifest = strict_json(read_bound(expected_manifest, mode=0o600, private=True))
    exact(manifest, OWNED_MANIFEST_FIELDS)
    require(manifest["schema_version"] == OWNED_CAPTURE_SCHEMA and manifest["origin"] in OWNED_ORIGINS
            and manifest["status"] in OWNED_STATUSES, "清单状态或来源非法")
    for name in ("plan_sha256", "context_sha256", "build_identity_sha256"):
        require(manifest[name] == plan.document[name], "清单绑定变化")
    require(manifest["collector_run_id"] == plan.document["context"]["collector_run_id"], "清单实例变化")
    exact(manifest["files"], OWNED_FILE_ROLES)
    files, raw_files, paths = manifest["files"], {}, {str(expected_manifest.path)}
    for role, binding in files.items():
        if binding is None:
            require(role not in {"plan", "context", "build_receipt", "projection"}, "必要冻结副本缺失")
            continue
        owned_relative_binding(binding)
        path = str(root / binding["path"])
        require(path not in paths, "原件角色复用路径")
        paths.add(path)
        raw_files[role] = _owned_read_file(root, binding)
    require({str(path) for path in root.iterdir()} == paths, "bundle 存在额外原件")
    require(raw_files["plan"] == canonical_json(plan.document) + b"\n"
            and raw_files["context"] == canonical_json(plan.document["context"]) + b"\n", "冻结计划或 context 变化")
    declared_receipt = plan.document["build_receipt"]
    require(len(raw_files["build_receipt"]) == declared_receipt["bytes"]
            and hashlib.sha256(raw_files["build_receipt"]).hexdigest() == declared_receipt["sha256"], "冻结构建收据副本变化")
    receipt = strict_json(raw_files["build_receipt"])
    exact(receipt, BUILD_FIELDS)
    require(receipt["schema_version"] == BUILD_SCHEMA and receipt["signing"]["performed"] is False
            and receipt["signing"]["qualification"] == "unqualified_build_only", "冻结构建副本不是未资格化 v1")
    frames = validate_owned_probe_projection(plan, raw_files["projection"])
    snapshots = _owned_snapshots(frames)
    for role in OWNED_FILE_ROLES - {"plan", "context", "build_receipt", "projection"}:
        require(raw_files.get(role) == snapshots.get(role), "派生原件与准确 native 帧不同")
    result = _owned_summary(plan, frames, files, manifest["origin"])
    require(manifest["status"] == result.document["status"]
            and canonical_json(manifest["unknown_reasons"]) == canonical_json(result.document["unknown_reasons"]), "清单不能清除首错")
    return result


class NativeOwnedProbeCollector:
    """真实 FD/固定 helper 生产接线；本版不授产品或正式来源资格。

    open/诊断/run/close 只能由明确运行调用触发；纯 plan、重放和导入模块不会启动。
    未知关闭不重发命令，原件失败保留，禁止 kill、重建 nonce 或覆盖同根。
    """

    def __init__(self) -> None:
        raise NativeProcessSourceUnavailable("请从固定 owned-v2 plan 打开原生实例")

    @classmethod
    def open(cls, plan: OwnedProbePlan) -> NativeOwnedProbeCollector:
        return cls._start(plan, "collect_owned_probe")

    @classmethod
    def _start(cls, plan: OwnedProbePlan, operation: str) -> NativeOwnedProbeCollector:
        import fcntl
        import subprocess
        import time
        from datetime import datetime, timezone
        require(isinstance(plan, OwnedProbePlan) and operation in {"diagnose", "collect_owned_probe"}, "启动参数非法")
        _owned_revalidate(plan)
        context = owned_context(plan.document["context"])
        require(datetime.now(timezone.utc) < utc(context.limits.deadline_utc), "deadline_exceeded：冻结期限已过")
        root = _root(Path(plan.document["bundle_root"]))
        require(not list(root.iterdir()), "同根已有原件，禁止重新启动；请只读重放已封存结果")
        instance = object.__new__(cls)
        instance.plan, instance.root, instance.context = plan, root, context
        instance._directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        instance._fds, instance._process, instance._projection_fd = set(), None, None
        instance._ready, instance._ran, instance._close_requested, instance._eof = False, False, False, False
        instance._result, instance._bytes, instance._buffer, instance._sequence = None, 0, bytearray(), 1
        instance._records = 0
        instance._deadline_monotonic = time.monotonic() + (utc(context.limits.deadline_utc) - datetime.now(timezone.utc)).total_seconds()
        instance._operation, instance._manifest = operation, None
        try:
            fcntl.flock(instance._directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            require(_owned_directory_instance(root) == plan.document["bundle_instance"] and not list(root.iterdir()), "取锁后目录实例变化")
            _owned_revalidate(plan)
            _owned_write_once(root, "plan.json", canonical_json(plan.document) + b"\n")
            _owned_write_once(root, "context.json", canonical_json(plan.document["context"]) + b"\n")
            receipt = read_bound(_binding(plan.document["build_receipt"]), mode=0o600, private=True)
            _owned_write_once(root, "build-receipt.json", receipt)
            instance._projection_fd = os.open(root / "projection.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            require(_owned_directory_instance(root) == plan.document["bundle_instance"], "派发前目录实例变化")
            context_read, context_write = os.pipe()
            events_read, events_write = os.pipe()
            instance._fds.update((context_read, context_write, events_read, events_write))
            controls = os.pipe() if operation == "collect_owned_probe" else ()
            instance._fds.update(controls)
            argv = [plan.document["helper"]["material"]["path"], "--probe-v2" if operation == "diagnose" else "--collect-owned-probe-v2",
                    "--context-fd", str(context_read), "--events-fd", str(events_write)]
            inherited = [context_read, events_write]
            instance._control_fd = controls[1] if controls else None
            if controls:
                argv += ["--control-fd", str(controls[0])]
                inherited.append(controls[0])
            instance._events_fd = events_read
            instance._process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, cwd=root, env={}, close_fds=True, pass_fds=tuple(inherited))
            for fd in inherited:
                os.close(fd)
                instance._fds.remove(fd)
            request = {"schema_version": OWNED_REQUEST_SCHEMA, "operation": operation,
                **{name: plan.document[name] for name in ("context", "context_sha256", "plan_sha256", "build_identity_sha256", "helper", "probe_fixture")}}
            instance._write_pipe(context_write, canonical_json(request) + b"\n")
            os.close(context_write)
            instance._fds.remove(context_write)
            os.set_blocking(events_read, False)
            return instance
        except BaseException:
            instance._release()
            raise

    def _write_pipe(self, fd: int, raw: bytes) -> None:
        import select
        import time
        from datetime import datetime, timezone
        os.set_blocking(fd, False)
        at = 0
        while at < len(raw):
            remaining = min(self._deadline_monotonic - time.monotonic(),
                            (utc(self.context.limits.deadline_utc) - datetime.now(timezone.utc)).total_seconds())
            require(remaining > 0, "deadline_exceeded：固定控制期限已过")
            _, writable, _ = select.select([], [fd], [], min(remaining, 0.1))
            if not writable:
                continue
            try:
                count = os.write(fd, raw[at:])
            except BlockingIOError:
                continue
            require(count > 0, "control_channel_failed：控制短写")
            at += count

    def _pump(self, *, ready_only: bool = False) -> None:
        import select
        import time
        from datetime import datetime, timezone
        while not self._eof:
            require(_owned_directory_instance(self.root) == self.plan.document["bundle_instance"], "采集时目录实例变化")
            remaining = min(self._deadline_monotonic - time.monotonic(),
                            (utc(self.context.limits.deadline_utc) - datetime.now(timezone.utc)).total_seconds())
            require(remaining > 0, "deadline_exceeded：不续算、不 kill、不重启")
            readable, _, _ = select.select([self._events_fd], [], [], min(remaining, 0.1))
            if not readable:
                continue
            try:
                chunk = os.read(self._events_fd, min(65536, self.context.limits.max_total_bytes + 1 - self._bytes))
            except BlockingIOError:
                continue
            if not chunk:
                self._eof = True
                os.fsync(self._projection_fd)
                return
            require(self._bytes + len(chunk) <= self.context.limits.max_total_bytes, "byte_limit：原件超过上限")
            view = memoryview(chunk)
            while view:
                count = os.write(self._projection_fd, view)
                require(count > 0, "wal_truncated：WAL 短写")
                view = view[count:]
            self._bytes += len(chunk)
            self._buffer.extend(chunk)
            candidate_ready = False
            while b"\n" in self._buffer:
                line, _, tail = self._buffer.partition(b"\n")
                self._buffer = bytearray(tail)
                require(len(line) + 1 <= self.context.limits.max_record_bytes, "byte_limit：原生单行超过上限")
                self._records += 1
                require(self._records <= self.context.limits.max_records, "record_limit：原生行数超过上限")
                frame = strict_json(bytes(line))
                require(isinstance(frame, Mapping), "原生帧不是对象")
                if frame.get("record_type") == "barrier" and frame.get("payload", {}).get("phase") == "ready":
                    candidate_ready = True
            require(len(self._buffer) < self.context.limits.max_record_bytes, "byte_limit：半行超过上限")
            if candidate_ready:
                raw = (self.root / "projection.jsonl").read_bytes()
                validate_owned_probe_projection(self.plan, raw[:raw.rfind(b"\n") + 1])
                self._ready = True
            if ready_only and self._ready:
                os.fsync(self._projection_fd)
                return

    def _command(self, operation: str) -> None:
        require(self._control_fd is not None and self._ready and not self._eof, "缺真实 ready 或控制通路")
        require(_owned_directory_instance(self.root) == self.plan.document["bundle_instance"], "控制前目录实例变化")
        value = {"schema_version": OWNED_CONTROL_SCHEMA, "collector_run_id": self.context.collector_run_id,
            "context_sha256": self.plan.document["context_sha256"], "plan_sha256": self.plan.document["plan_sha256"],
            "sequence": self._sequence, "operation": operation}
        self._sequence += 1
        self._write_pipe(self._control_fd, canonical_json(value) + b"\n")

    def await_ready(self) -> MaterialBinding:
        require(self._operation == "collect_owned_probe" and not self._close_requested, "实例不能再等待 ready")
        self._pump(ready_only=True)
        if not self._ready:
            self.close()
            raise NativeProcessSourceUnavailable("实际原生实例未产生 ready；诊断/失败原件已封存")
        raw = (self.root / "projection.jsonl").read_bytes()
        # ready 时只重验完整前缀；尚未接收到的半行不能当失败或补写换行。
        prefix = raw[:raw.rfind(b"\n") + 1]
        frames = validate_owned_probe_projection(self.plan, prefix)
        ready = _owned_snapshots(frames)["ready"]
        path = self.root / "ready.json"
        if not path.exists():
            _owned_write_once(self.root, path.name, ready)
        binding = MaterialBinding(str(path), hashlib.sha256(ready).hexdigest(), len(ready))
        read_bound(binding, mode=0o600, private=True)
        return binding

    def run_owned_probe(self) -> None:
        require(self._ready and not self._ran and not self._close_requested, "探针必须真实 ready 且只能派发一次")
        self._ran = True
        self._command("run_owned_probe")

    def close(self) -> OwnedProbeReadResult:
        if self._manifest is not None:
            return replay_owned_probe_capture(self.plan, self.root, self._manifest)
        require(not self._close_requested, "未知关闭不能重发；原件保留，禁止重启本根")
        self._close_requested = True
        try:
            if self._operation == "collect_owned_probe" and not self._eof:
                if not self._ready:
                    self._pump(ready_only=True)
                if self._ready and not self._eof:
                    self._command("close")
            self._pump()
            os.close(self._projection_fd)
            self._projection_fd = None
            raw = (self.root / "projection.jsonl").read_bytes()
            frames = validate_owned_probe_projection(self.plan, raw)
            snapshots = _owned_snapshots(frames)
            files = {role: None for role in OWNED_FILE_ROLES}
            for role in ("plan", "context", "build_receipt", "projection"):
                name = {"plan": "plan.json", "context": "context.json", "build_receipt": "build-receipt.json", "projection": "projection.jsonl"}[role]
                data = (self.root / name).read_bytes()
                files[role] = {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for role, data in snapshots.items():
                path = self.root / (role + ".json")
                if not path.exists():
                    _owned_write_once(self.root, path.name, data)
                files[role] = {"path": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                _owned_read_file(self.root, files[role])
            result = _owned_summary(self.plan, frames, files, "native_unqualified")
            manifest = {"schema_version": OWNED_CAPTURE_SCHEMA,
                **{name: self.plan.document[name] for name in ("plan_sha256", "context_sha256", "build_identity_sha256")},
                "collector_run_id": self.context.collector_run_id, "origin": "native_unqualified", "status": result.document["status"],
                "files": files, "unknown_reasons": result.document["unknown_reasons"]}
            relative = _owned_write_once(self.root, "capture-manifest.json", canonical_json(manifest) + b"\n")
            self._manifest = MaterialBinding(str(self.root / relative["path"]), relative["sha256"], relative["bytes"])
            self._result = replay_owned_probe_capture(self.plan, self.root, self._manifest)
            return self._result
        finally:
            self._release()

    def _release(self) -> None:
        # 只关闭本实例持有的 FD；不向 helper/probe 或其它进程发送信号。
        for fd in tuple(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
            self._fds.discard(fd)
        if self._projection_fd is not None:
            os.fsync(self._projection_fd)
            os.close(self._projection_fd)
            self._projection_fd = None
        if self._process is not None:
            self._process.poll()
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None

    def __enter__(self) -> NativeOwnedProbeCollector:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.close()
        except BaseException:
            if exc_type is None:
                raise


def diagnose_owned_probe(plan: OwnedProbePlan) -> OwnedProbeReadResult:
    """实际创建/删除诊断接线；无订阅、无探针，不授以后实例的启动权。"""
    return NativeOwnedProbeCollector._start(plan, "diagnose").close()
