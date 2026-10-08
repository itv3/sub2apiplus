"""受管原生 helper 的构建重验与离线接线，不提供当前产品或 ES 启动入口。

本版不能确认唯一 boot、mute、实例边界与尾部排空；诊断与采集入口在任何
目录创建、子进程或 ES 调用前拒绝。外部原件即使摘要正确，也只证明结构。
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
