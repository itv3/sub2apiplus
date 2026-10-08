"""固定本机原生构建；只在私有临时根写两角色，不安装、不签名、不执行输出。

依赖清单来自真实 clang -MD，链接原件来自真实 ld -t；完整命令与版本原件保留。
这些材料只说明本次有限构建，不宣称运行兼容、ES 资格或完整系统调用读集。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shlex
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.official_client_capture.claude_mac_process_events_contract import (
    BUILD_SCHEMA, PROJECTION_DESCRIPTOR, MaterialBinding, canonical_json, fingerprint, require,
)


CLT_ROOT = Path("/Library/Developer/CommandLineTools")
SDK_ALIAS = CLT_ROOT / "SDKs/MacOSX.sdk"
MINIMUM_TARGET = "15.0"
SOURCE_NAMES = (
    "claude_mac_process_events_native.m", "claude_mac_process_events_contract.py",
    "claude_mac_process_events.py", "claude_mac_process_events_build.py",
)
TOOLS = {"clang": CLT_ROOT / "usr/bin/clang", "linker": CLT_ROOT / "usr/bin/ld",
         "lipo": Path("/usr/bin/lipo"), "otool": Path("/usr/bin/otool")}


def _material(path: Path) -> dict[str, Any]:
    """实读规范目标，构建前后逐项复核；只绑定文件，不枚举凭据或用户目录。"""
    path = path.resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode), "构建物料须为普通文件")
        digest, size = hashlib.sha256(), 0
        while True:
            raw = os.read(fd, 1_048_576)
            if not raw:
                break
            digest.update(raw)
            size += len(raw)
        after = os.fstat(fd)
        current = path.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
        require(all(getattr(before, name) == getattr(after, name) == getattr(current, name) for name in fields), "构建物料读取期间变化")
    finally:
        os.close(fd)
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": size}


def _write_once(path: Path, raw: bytes) -> dict[str, Any]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # 失败原件保留；不覆盖旧收据或回收其他进程。
        raise
    return _material(path)


def _run(argv: list[str], root: Path, name: str, traces: list[dict[str, Any]], *, permit_failure: bool = False) -> subprocess.CompletedProcess:
    """构建命令固定且不经 shell；输出不执行、不用 timeout 发送终止信号。"""
    environment = {"PATH": str(CLT_ROOT / "usr/bin") + ":/usr/bin:/bin", "HOME": str(root),
                   "TMPDIR": str(root), "LC_ALL": "C", "LANG": "C"}
    result = subprocess.run(argv, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    require(len(result.stdout) + len(result.stderr) <= 8_388_608, "构建命令输出超过固定上界")
    transcript = canonical_json({"argv": argv, "returncode": result.returncode,
        "stdout": result.stdout.decode("utf-8", errors="strict"),
        "stderr": result.stderr.decode("utf-8", errors="strict")}) + b"\n"
    traces.append(_write_once(root / (name + ".json"), transcript))
    require(permit_failure or result.returncode == 0, f"固定构建命令失败：{name}；原件已保留")
    return result


def _dependencies(path: Path) -> list[Path]:
    value = path.read_text().replace("\\\n", " ")
    require(":" in value, "编译器依赖原件格式非法")
    values = shlex.split(value.split(":", 1)[1], posix=True)
    require(values and len(values) <= 8192, "实际编译依赖为空或越界")
    return [Path(item).resolve(strict=True) for item in values]


def _link_command(result: subprocess.CompletedProcess, expected_linker: Path) -> list[str]:
    commands = []
    for line in result.stderr.decode("utf-8").splitlines():
        if not line.startswith(' "'):
            continue
        values = shlex.split(line)
        if values and Path(values[0]).name == "ld":
            commands.append(values)
    require(len(commands) == 1 and Path(commands[0][0]).resolve() == expected_linker
            and "-no_adhoc_codesign" in commands[0], "驱动没有选出唯一固定且不签名的 linker")
    return commands[0]


def _link_inputs(result: subprocess.CompletedProcess) -> list[Path]:
    """保留 ld 的加载原件，再绑定其中实际列出的输入；不补推测文件名。"""
    paths = []
    for line in (result.stdout + result.stderr).decode("utf-8").splitlines():
        text = line.strip()
        if text.startswith("ld: loaded: "):
            text = text.removeprefix("ld: loaded: ")
        if text.startswith("/"):
            candidate = Path(text)
            if candidate.is_file():
                paths.append(candidate.resolve(strict=True))
    require(paths, "真实链接 trace 没有可绑定输入；保留原件后拒绝出具构建收据")
    return paths


def build_native(staging_root: Path) -> MaterialBinding:
    """冻结 SDK 15.5／arm64／最低构建目标 15.0，仅产生两个未资格化私有构建物。"""
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "本版只在 Darwin arm64 私有构建")
    require(staging_root.is_absolute() and ".." not in staging_root.parts
            and str(staging_root) == str(staging_root.resolve())
            and staging_root.is_relative_to(Path("/private/tmp")), "构建根只能是规范 /private/tmp 私有目录")
    for item in (staging_root, *staging_root.parents):
        require(not item.is_symlink(), "构建根含 symlink")
    if not staging_root.exists():
        staging_root.mkdir(mode=0o700)
    info = staging_root.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
            and not list(staging_root.iterdir()), "构建根须为当前用户私有空目录，不能覆写旧构建")
    source_root = Path(__file__).resolve().parent
    source_files = [_material(source_root / name) for name in SOURCE_NAMES]
    sdk = SDK_ALIAS.resolve(strict=True)
    settings = sdk / "SDKSettings.json"
    import json
    sdk_version = str(json.loads(settings.read_text())["Version"])
    settings_before = _material(settings)
    require(sdk_version == "15.5", "SDK 版本不是本批冻结的 15.5")
    tools = {name: path.resolve(strict=True) for name, path in TOOLS.items()}
    compiler_materials = [_material(path) for path in tools.values()]
    traces, versions = [], {}
    for name, option in (("clang", "--version"), ("linker", "-v"), ("lipo", "-version"), ("otool", "--version")):
        result = _run([str(tools[name]), option], staging_root, name + "-version", traces, permit_failure=True)
        versions[name] = (result.stdout + result.stderr).decode("utf-8").strip()
        require(versions[name], "工具链缺少实际版本输出")
    compile_commands, link_commands, loaded_inputs, dependency_paths, outputs, signature_inspection = [], [], [], [], [], []
    for role, fixture in (("collector", "0"), ("probe_fixture", "1")):
        object_path, dependency_path = staging_root / (role + ".o"), staging_root / (role + ".d")
        output_path = staging_root / role
        compile_command = [str(tools["clang"]), "-std=gnu11", "-fobjc-arc", "-fblocks", "-Wall", "-Wextra", "-Werror",
            "-fno-modules", "-fno-implicit-modules", "-arch", "arm64", "-mmacosx-version-min=" + MINIMUM_TARGET,
            "-isysroot", str(sdk), "-DCLAUDE_PROBE_FIXTURE=" + fixture,
            "-MD", "-MF", str(dependency_path), "-c", str(source_root / SOURCE_NAMES[0]), "-o", str(object_path)]
        before_path = staging_root / (role + "-before.d")
        scan_command = compile_command[:compile_command.index("-MD")] + ["-M", "-MF", str(before_path), str(source_root / SOURCE_NAMES[0])]
        _run(scan_command, staging_root, role + "-dependency-scan", traces)
        before_path.chmod(0o600)
        before_dependencies = {path: _material(path) for path in _dependencies(before_path)}
        _run(compile_command, staging_root, role + "-compile", traces)
        compile_commands.append(compile_command)
        for path in (object_path, dependency_path):
            path.chmod(0o600)
        actual_dependencies = _dependencies(dependency_path)
        require(set(actual_dependencies) == set(before_dependencies)
                and all(_material(path) == before_dependencies[path] for path in actual_dependencies), "实际编译读集与构建前扫描不同或字节变化")
        dependency_paths.extend(actual_dependencies)
        dependency_paths.extend([dependency_path, before_path])
        link_driver = [str(tools["clang"]), "-arch", "arm64", "-mmacosx-version-min=" + MINIMUM_TARGET,
            "-isysroot", str(sdk), str(object_path), "-framework", "Foundation"]
        if role == "collector":
            link_driver += ["-framework", "CoreFoundation", "-lEndpointSecurity", "-lbsm", "-lproc"]
        link_driver += ["-Wl,-t", "-Wl,-no_adhoc_codesign", "-o", str(output_path)]
        dry = _run(link_driver + ["-###"], staging_root, role + "-link-driver", traces)
        command = _link_command(dry, tools["linker"])
        preview_path = staging_root / (role + ".link-scan")
        preview_command = list(command)
        preview_command[preview_command.index("-o") + 1] = str(preview_path)
        preview = _run(preview_command, staging_root, role + "-link-scan", traces)
        preview_path.chmod(0o600)
        explicit_inputs = [Path(value).resolve(strict=True) for value in command[1:]
                           if value.startswith("/") and Path(value).is_file() and Path(value) != output_path]
        before_link = {path: _material(path) for path in _link_inputs(preview) + explicit_inputs}
        result = _run(command, staging_root, role + "-link", traces)
        link_commands.append(command)
        actual_link_inputs = set(_link_inputs(result) + explicit_inputs)
        require(actual_link_inputs == set(before_link)
                and all(_material(path) == before_link[path] for path in actual_link_inputs), "实际链接输入与构建前trace不同或字节变化")
        loaded_inputs.extend(actual_link_inputs)
        output_path.chmod(0o755)
        arch_result = _run([str(tools["lipo"]), "-archs", str(output_path)], staging_root, role + "-architecture", traces)
        require(arch_result.stdout.decode("utf-8").strip() == "arm64", "实际输出不是单一 arm64")
        signature_result = _run([str(tools["otool"]), "-l", str(output_path)], staging_root, role + "-load-commands", traces)
        signed = "LC_CODE_SIGNATURE" in signature_result.stdout.decode("utf-8")
        signature_inspection.append({"role": role, "code_signature_present": signed, "inspection": traces[-1]})
        outputs.append({"role": role, "material": _material(output_path), "mode": 0o755,
                        "architecture": "arm64", "code_signature_present": signed})
    require(SDK_ALIAS.resolve(strict=True) == sdk and _material(settings) == settings_before, "SDK 别名或设置在构建期间变化")
    sdk_materials = [_material(path) for path in sorted(set(dependency_paths + [settings]))]
    framework_materials = [_material(path) for path in sorted(set(loaded_inputs))]
    # 留住实际编译／链接读集；没有系统文件打开追踪，不把它包装成全部工具进程读集。
    for material in source_files + compiler_materials + sdk_materials + framework_materials + traces:
        require(_material(Path(material["path"])) == material, "构建物料在封存前变化")
    status = "ad_hoc_unqualified" if any(output["code_signature_present"] for output in outputs) else "unsigned"
    receipt = {
        "schema_version": BUILD_SCHEMA, "source_files": source_files, "source_files_sha256": fingerprint(source_files),
        "projection_contract_sha256": fingerprint(PROJECTION_DESCRIPTOR),
        "compiler": {"materials": compiler_materials, "versions": versions, "driver_traces": traces,
                     "linker_trace_status": "retained_actual_trace_unqualified"},
        "sdk": {"requested_path": str(SDK_ALIAS), "resolved_path": str(sdk), "version": sdk_version,
                "materials": sdk_materials, "alias_verified_before_after": True,
                "read_set_status": "actual_dependencies_and_link_inputs_unqualified"},
        "architecture": "arm64", "deployment_target": MINIMUM_TARGET,
        "compile_argv": compile_commands, "link_argv": link_commands, "framework_bindings": framework_materials,
        "unsigned_outputs": outputs,
        "signing": {"status": status, "performed": False, "entitlements_qualified": False,
                    "qualification": "unqualified_build_only", "signature_inspection": signature_inspection},
        "final_outputs": outputs, "observed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    binding = _write_once(staging_root / "build-receipt.json", canonical_json(receipt) + b"\n")
    return MaterialBinding(**binding)


def main() -> int:
    """只公开私有构建入口，不接受编译器、宏、框架、命令或安装路径覆盖。"""
    parser = argparse.ArgumentParser(description="Claude 原生进程观测有限私有构建，不执行输出")
    parser.add_argument("--staging-root", type=Path, required=True)
    arguments = parser.parse_args()
    receipt = build_native(arguments.staging_root)
    print(canonical_json({"build_receipt": {"path": receipt.path, "sha256": receipt.sha256, "bytes": receipt.bytes},
        "formal_ready": False, "launch_allowed": False, "process_events_complete": False,
        "registration_verified": False, "executed_image_verified": False}).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
