#!/usr/bin/env python3
"""生成并重放 VC-4 Candidate 的严格构建实物收据。

该模块只处理离线构建输入、Docker 镜像实物和零网络 capability probe。
它不读取抓包正文，也不发送任何 HTTP 请求。所有目录都逐项扫描；符号链接和
特殊文件一律失败关闭，避免把 build-parameters.json 中的自述字段当成事实。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


BUILD_PARAMETERS_SCHEMA = "sub2apiplus-candidate-build-parameters/v2"
INVENTORY_RECEIPT_SCHEMA = "codex-upgrade-candidate-build-inventory/v1"
FRONTEND_PROVENANCE_SCHEMA = "codex-upgrade-candidate-frontend-provenance/v1"
IMAGE_INSPECTION_SCHEMA = "codex-upgrade-candidate-image-inspection/v1"
CAPABILITY_PROBE_SCHEMA = "codex-upgrade-candidate-capability-probe/v1"
FRONTEND_BUILDER_SCHEMA = "candidate-frontend-builder-receipt/v1"
TOOLCHAIN_APPROVAL_SCHEMA = "candidate-build-toolchain-deviation-approval/v1"
CAPABILITY_OUTPUT_SCHEMA = "sub2api-candidate-capture-capability/v1"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$"
)
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_OUTPUT_RE = re.compile(r"^v?(?P<major>[0-9]+)(?:\.[0-9]+){1,2}(?:[-+].*)?$")
EXPECTED_ENTRYPOINT = ["/app/docker-entrypoint.sh"]
REQUIRED_IMAGE_FILES = (
    "/app/docker-entrypoint.sh",
    "/app/container-healthcheck.sh",
    "/app/sub2api",
)
REQUIRED_TAGS = frozenset({"embed", "candidatecapture"})


class CandidateBuildError(ValueError):
    """Candidate 构建参数、实物或收据不可复核。"""


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _self_bound(payload: Mapping[str, Any], field: str = "receipt_digest") -> dict[str, Any]:
    result = dict(payload)
    result[field] = digest(result)
    return result


def _verify_self_digest(payload: Mapping[str, Any], label: str) -> None:
    recorded = payload.get("receipt_digest")
    if not isinstance(recorded, str) or not SHA256_RE.fullmatch(recorded):
        raise CandidateBuildError(f"{label}缺少合法自摘要")
    unsigned = dict(payload)
    unsigned.pop("receipt_digest")
    if digest(unsigned) != recorded:
        raise CandidateBuildError(f"{label}自摘要不一致")


def _require_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CandidateBuildError(f"{label}字段不闭合")
    return dict(value)


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise CandidateBuildError(f"{label}不是小写 SHA-256")
    return value


def _require_image_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IMAGE_ID_RE.fullmatch(value):
        raise CandidateBuildError(f"{label}不是 Docker image ID")
    return value


def _require_absolute(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise CandidateBuildError(f"{label}必须是绝对路径")
    return Path(value)


def _require_relative(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CandidateBuildError(f"{label}必须是非空 POSIX 相对路径")
    result = PurePosixPath(value)
    if result.is_absolute() or any(part in {"", ".", ".."} for part in result.parts):
        raise CandidateBuildError(f"{label}必须是规范 POSIX 相对路径")
    return result


def _require_string_list(value: Any, label: str, *, sorted_unique: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise CandidateBuildError(f"{label}必须是非空字符串数组")
    result = list(value)
    if sorted_unique and (result != sorted(result) or len(result) != len(set(result))):
        raise CandidateBuildError(f"{label}必须排序且去重")
    return result


def _validate_file_binding(value: Any, label: str, *, absolute: bool = True) -> dict[str, Any]:
    binding = _require_fields(value, {"path", "sha256", "bytes"}, label)
    if absolute:
        _require_absolute(binding["path"], f"{label}.path")
    else:
        _require_relative(binding["path"], f"{label}.path")
    _require_sha(binding["sha256"], f"{label}.sha256")
    if not isinstance(binding.get("bytes"), int) or binding["bytes"] <= 0:
        raise CandidateBuildError(f"{label}.bytes 非法")
    return binding


def _read_bound_json(binding: Mapping[str, Any], label: str) -> dict[str, Any]:
    path = Path(str(binding["path"]))
    if path.is_symlink() or not path.is_file():
        raise CandidateBuildError(f"{label}不存在或不是可信普通文件")
    if path.stat().st_size != binding["bytes"] or file_sha256(path) != binding["sha256"]:
        raise CandidateBuildError(f"{label}大小或摘要漂移")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateBuildError(f"{label}不是合法 JSON") from error
    if not isinstance(value, dict):
        raise CandidateBuildError(f"{label}必须是 JSON 对象")
    return value


def validate_build_parameters(
    value: Any,
    *,
    candidate_id: str,
    source_root: Path,
    git_commit: str,
    binary_path: Path,
    binary_sha256: str,
    binary_bytes: int,
    build_tree: Path,
    docker_context: Path,
    frontend_dist_source: Path,
    target_architecture: str,
    image_id: str,
) -> dict[str, Any]:
    """校验严格构建参数及所有 CLI／参数跨字段等式。"""

    payload = _require_fields(
        value,
        {
            "schema_version",
            "candidate_id",
            "source",
            "build_tree",
            "frontend",
            "go_build",
            "docker_build",
            "binary",
        },
        "Candidate 构建参数",
    )
    if payload.get("schema_version") != BUILD_PARAMETERS_SCHEMA:
        raise CandidateBuildError("Candidate 构建参数必须使用严格 v2 schema")
    if payload.get("candidate_id") != candidate_id or not SAFE_ID_RE.fullmatch(candidate_id):
        raise CandidateBuildError("CLI 与构建参数 candidate_id 不一致")

    source = _require_fields(payload["source"], {"root", "git_commit"}, "source")
    tree = _require_fields(
        payload["build_tree"], {"root", "git_commit", "umask"}, "build_tree"
    )
    _require_absolute(source.get("root"), "source.root")
    _require_absolute(tree.get("root"), "build_tree.root")
    if (
        Path(str(source.get("root"))).resolve(strict=True) != source_root.resolve(strict=True)
        or source.get("git_commit") != git_commit
    ):
        raise CandidateBuildError("source 未绑定 CLI 的实际源码树与 Git commit")
    if (
        Path(str(tree.get("root"))).resolve(strict=True) != build_tree.resolve(strict=True)
        or tree.get("git_commit") != git_commit
        or tree.get("umask") != "0022"
    ):
        raise CandidateBuildError("build_tree 根、commit 或 umask 不满足严格合同")
    roots = tuple(
        path.resolve(strict=True)
        for path in (source_root, build_tree, docker_context, frontend_dist_source)
    )
    if len(set(roots)) != len(roots) or any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise CandidateBuildError("source/build tree/context/dist 根必须互不嵌套")

    binary = _validate_file_binding(payload["binary"], "binary")
    if (
        Path(binary["path"]).resolve(strict=True) != binary_path.resolve(strict=True)
        or binary["sha256"] != binary_sha256
        or binary["bytes"] != binary_bytes
    ):
        raise CandidateBuildError("binary 参数与 CLI 实物不一致")

    frontend = _require_fields(
        payload["frontend"],
        {
            "source_root",
            "package_manifest",
            "lockfile",
            "build_command",
            "node_version",
            "pnpm_version",
            "builder",
            "dist_source_root",
            "dist_build_tree_path",
            "toolchain_policy",
        },
        "frontend",
    )
    _require_relative(frontend["source_root"], "frontend.source_root")
    _require_relative(frontend["package_manifest"], "frontend.package_manifest")
    _require_relative(frontend["lockfile"], "frontend.lockfile")
    _require_relative(frontend["dist_build_tree_path"], "frontend.dist_build_tree_path")
    _require_string_list(frontend["build_command"], "frontend.build_command")
    _require_absolute(frontend.get("dist_source_root"), "frontend.dist_source_root")
    if Path(str(frontend.get("dist_source_root"))).resolve(strict=True) != frontend_dist_source.resolve(strict=True):
        raise CandidateBuildError("frontend.dist_source_root 与 CLI 实物不一致")
    if not isinstance(frontend.get("node_version"), str) or not VERSION_OUTPUT_RE.fullmatch(frontend["node_version"]):
        raise CandidateBuildError("frontend.node_version 非法")
    if not isinstance(frontend.get("pnpm_version"), str) or not VERSION_OUTPUT_RE.fullmatch(frontend["pnpm_version"]):
        raise CandidateBuildError("frontend.pnpm_version 非法")
    builder = _require_fields(frontend["builder"], {"kind", "identity", "receipt"}, "frontend.builder")
    if builder.get("kind") not in {"release_pipeline", "approved_local"}:
        raise CandidateBuildError("frontend.builder.kind 非法")
    if not isinstance(builder.get("identity"), str) or not builder["identity"].strip():
        raise CandidateBuildError("frontend.builder.identity 为空")
    _validate_file_binding(builder["receipt"], "frontend.builder.receipt")
    policy = _require_fields(
        frontend["toolchain_policy"],
        {"required_node_major", "deviation_approval"},
        "frontend.toolchain_policy",
    )
    if policy.get("required_node_major") != 20:
        raise CandidateBuildError("前端默认工具链必须冻结 Node 20")
    node_major = int(VERSION_OUTPUT_RE.fullmatch(frontend["node_version"]).group("major"))
    deviation_required = node_major != 20 or builder["kind"] != "release_pipeline"
    if deviation_required:
        _validate_file_binding(
            policy.get("deviation_approval"),
            "frontend.toolchain_policy.deviation_approval",
        )
    elif policy.get("deviation_approval") is not None:
        raise CandidateBuildError("标准发布工具链不得夹带 deviation approval")

    go_build = _require_fields(
        payload["go_build"],
        {"command", "working_directory", "environment", "required_tags"},
        "go_build",
    )
    _require_string_list(go_build["command"], "go_build.command")
    _require_relative(go_build["working_directory"], "go_build.working_directory")
    environment = _require_fields(
        go_build["environment"], {"CGO_ENABLED", "GOOS", "GOARCH", "GOFLAGS"}, "go_build.environment"
    )
    if any(not isinstance(value, str) for value in environment.values()):
        raise CandidateBuildError("go_build.environment 值必须是字符串")
    tags = set(_require_string_list(go_build["required_tags"], "go_build.required_tags", sorted_unique=True))
    if not REQUIRED_TAGS.issubset(tags):
        raise CandidateBuildError("go_build.required_tags 缺少 embed 或 candidatecapture")
    target_os, target_arch = target_architecture.split("/", 1)
    if environment["GOOS"] != target_os or environment["GOARCH"] != target_arch:
        raise CandidateBuildError("GOOS/GOARCH 与目标架构不一致")

    docker = _require_fields(
        payload["docker_build"],
        {
            "context_root",
            "dockerfile",
            "platform",
            "image_id",
            "labels",
            "entrypoint",
            "assembly",
        },
        "docker_build",
    )
    _require_absolute(docker.get("context_root"), "docker_build.context_root")
    if Path(str(docker.get("context_root"))).resolve(strict=True) != docker_context.resolve(strict=True):
        raise CandidateBuildError("docker_build.context_root 与 CLI 实物不一致")
    _require_relative(docker["dockerfile"], "docker_build.dockerfile")
    if docker.get("platform") != target_architecture:
        raise CandidateBuildError("docker_build.platform 与目标架构不一致")
    if docker.get("image_id") != image_id:
        raise CandidateBuildError("docker_build.image_id 与 CLI image ID 不一致")
    labels = _require_fields(
        docker["labels"],
        {"org.opencontainers.image.revision", "org.opencontainers.image.version"},
        "docker_build.labels",
    )
    if labels["org.opencontainers.image.revision"] != git_commit:
        raise CandidateBuildError("OCI revision 与源码 commit 不一致")
    version_label = labels["org.opencontainers.image.version"]
    if not isinstance(version_label, str) or not version_label.endswith(f"-{candidate_id}"):
        raise CandidateBuildError("OCI version 未以当前 Candidate ID 结尾")
    if docker.get("entrypoint") != EXPECTED_ENTRYPOINT:
        raise CandidateBuildError("docker_build.entrypoint 不是批准入口")
    assembly = docker.get("assembly")
    if not isinstance(assembly, list) or not assembly:
        raise CandidateBuildError("docker_build.assembly 不能为空")
    normalized: list[tuple[str, str, str]] = []
    for index, raw in enumerate(assembly, 1):
        row = _require_fields(
            raw, {"context_path", "source_kind", "source_path"}, f"assembly[{index}]"
        )
        context_path = _require_relative(row["context_path"], f"assembly[{index}].context_path").as_posix()
        if row["source_kind"] == "build_tree":
            source_path = _require_relative(row["source_path"], f"assembly[{index}].source_path").as_posix()
        elif row["source_kind"] == "binary":
            source_path = str(_require_absolute(row["source_path"], f"assembly[{index}].source_path"))
            if Path(source_path).resolve(strict=True) != binary_path.resolve(strict=True):
                raise CandidateBuildError("assembly 的 binary 来源不是当前 Candidate 二进制")
        else:
            raise CandidateBuildError("assembly.source_kind 只能是 build_tree 或 binary")
        normalized.append((context_path, row["source_kind"], source_path))
    if normalized != sorted(normalized) or len({item[0] for item in normalized}) != len(normalized):
        raise CandidateBuildError("docker_build.assembly 必须按 context_path 排序且目标不重复")
    if docker["dockerfile"] not in {item[0] for item in normalized}:
        raise CandidateBuildError("docker_build.assembly 未覆盖 Dockerfile")
    return payload


def _entry_mode(mode: int) -> str:
    return f"{stat.S_IMODE(mode):04o}"


def scan_tree_inventory(root: Path) -> dict[str, Any]:
    """扫描真实目录，记录 path/type/mode/size/sha256 并拒绝链接。"""

    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise CandidateBuildError(f"inventory 根目录不存在或不可信：{root}")
    resolved = root.resolve(strict=True)
    entries: list[dict[str, Any]] = []
    directory_paths: list[Path] = [resolved]
    file_paths: list[Path] = []
    for current, directories, files in os.walk(resolved, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise CandidateBuildError(f"inventory 禁止符号链接：{path}")
            if not path.is_dir():
                raise CandidateBuildError(f"inventory 禁止特殊目录项：{path}")
            directory_paths.append(path)
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise CandidateBuildError(f"inventory 禁止符号链接：{path}")
            if not path.is_file():
                raise CandidateBuildError(f"inventory 禁止特殊文件：{path}")
            file_paths.append(path)

    file_rows: dict[str, dict[str, Any]] = {}
    for path in file_paths:
        relative = path.relative_to(resolved).as_posix()
        info = path.stat()
        row = {
            "path": relative,
            "type": "file",
            "mode": _entry_mode(info.st_mode),
            "size": info.st_size,
            "sha256": file_sha256(path),
        }
        file_rows[relative] = row

    directory_rows: dict[str, dict[str, Any]] = {}
    for path in sorted(directory_paths, key=lambda item: len(item.parts), reverse=True):
        relative = "." if path == resolved else path.relative_to(resolved).as_posix()
        info = path.stat()
        child_records: list[dict[str, str]] = []
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            child_relative = child.relative_to(resolved).as_posix()
            child_row = file_rows.get(child_relative) or directory_rows.get(child_relative)
            if child_row is None:
                raise CandidateBuildError(f"inventory 子项类型不可复核：{child}")
            child_records.append(
                {
                    "name": child.name,
                    "type": str(child_row["type"]),
                    "sha256": str(child_row["sha256"]),
                }
            )
        directory_rows[relative] = {
            "path": relative,
            "type": "directory",
            "mode": _entry_mode(info.st_mode),
            "traversal_mode": f"{stat.S_IMODE(info.st_mode) & 0o111:04o}",
            "size": 0,
            "sha256": digest(child_records),
        }
    entries.extend(directory_rows.values())
    entries.extend(file_rows.values())
    entries.sort(key=lambda row: (str(row["path"]), str(row["type"])))
    return {
        "root": str(resolved),
        "entry_count": len(entries),
        "file_count": len(file_rows),
        "directory_count": len(directory_rows),
        "total_file_bytes": sum(int(row["size"]) for row in file_rows.values()),
        "tree_sha256": directory_rows["."]["sha256"],
        "inventory_sha256": digest(entries),
        "entries": entries,
    }


def _inventory_rows(inventory: Mapping[str, Any], kind: str | None = None) -> dict[str, dict[str, Any]]:
    rows = inventory.get("entries")
    if not isinstance(rows, list):
        raise CandidateBuildError("inventory.entries 非法")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise CandidateBuildError("inventory entry 非法")
        if kind is None or row.get("type") == kind:
            result[row["path"]] = row
    return result


def _expand_assembly(
    parameters: Mapping[str, Any],
    *,
    build_tree: Path,
    docker_context: Path,
    binary_path: Path,
    context_inventory: Mapping[str, Any],
) -> list[dict[str, Any]]:
    context_files = _inventory_rows(context_inventory, "file")
    expected_context_paths: set[str] = set()
    transitions: list[dict[str, Any]] = []
    for raw in parameters["docker_build"]["assembly"]:
        context_prefix = PurePosixPath(raw["context_path"])
        if raw["source_kind"] == "binary":
            sources = [(binary_path, PurePosixPath("."))]
        else:
            source_root = build_tree / PurePosixPath(raw["source_path"])
            if source_root.is_symlink() or not source_root.exists():
                raise CandidateBuildError(f"assembly 来源不存在或不可信：{source_root}")
            if source_root.is_file():
                sources = [(source_root, PurePosixPath("."))]
            elif source_root.is_dir():
                source_inventory = scan_tree_inventory(source_root)
                sources = [
                    (source_root / PurePosixPath(path), PurePosixPath(path))
                    for path in _inventory_rows(source_inventory, "file")
                ]
            else:
                raise CandidateBuildError(f"assembly 来源类型非法：{source_root}")
        for source, relative in sources:
            context_relative = (
                context_prefix if relative == PurePosixPath(".") else context_prefix / relative
            ).as_posix()
            if context_relative in expected_context_paths:
                raise CandidateBuildError(f"assembly 展开后目标重复：{context_relative}")
            expected_context_paths.add(context_relative)
            destination = docker_context / PurePosixPath(context_relative)
            if destination.is_symlink() or not destination.is_file():
                raise CandidateBuildError(f"Docker context 缺少 assembly 文件：{context_relative}")
            source_info = source.stat()
            source_binding = {
                "path": str(source.resolve(strict=True)),
                "mode": _entry_mode(source_info.st_mode),
                "size": source_info.st_size,
                "sha256": file_sha256(source),
            }
            destination_row = context_files.get(context_relative)
            if destination_row is None:
                raise CandidateBuildError(f"Docker context inventory 缺少：{context_relative}")
            if (
                source_binding["mode"] != destination_row["mode"]
                or source_binding["size"] != destination_row["size"]
                or source_binding["sha256"] != destination_row["sha256"]
            ):
                raise CandidateBuildError(f"build tree → context 装配内容或 mode 漂移：{context_relative}")
            transitions.append(
                {
                    "context_path": context_relative,
                    "source_kind": raw["source_kind"],
                    "source": source_binding,
                    "context": {
                        "mode": destination_row["mode"],
                        "size": destination_row["size"],
                        "sha256": destination_row["sha256"],
                    },
                }
            )
    if expected_context_paths != set(context_files):
        missing = sorted(set(context_files) - expected_context_paths)
        extra = sorted(expected_context_paths - set(context_files))
        raise CandidateBuildError(f"Docker context assembly 文件闭集不一致：未登记={missing} 缺失={extra}")
    expected_directories = {"."}
    for path in expected_context_paths:
        parent = PurePosixPath(path).parent
        while parent.as_posix() not in {"", "."}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories = set(_inventory_rows(context_inventory, "directory"))
    if actual_directories != expected_directories:
        raise CandidateBuildError("Docker context 含未登记空目录或缺少父目录")
    transitions.sort(key=lambda row: row["context_path"])
    return transitions


def build_inventory_receipt(
    parameters: Mapping[str, Any],
    *,
    candidate_id: str,
    image_id: str,
    source_root: Path,
    build_tree: Path,
    docker_context: Path,
    binary_path: Path,
) -> dict[str, Any]:
    build_inventory = scan_tree_inventory(build_tree)
    context_inventory = scan_tree_inventory(docker_context)
    transitions = _expand_assembly(
        parameters,
        build_tree=build_tree,
        docker_context=docker_context,
        binary_path=binary_path,
        context_inventory=context_inventory,
    )
    for script in ("deploy/docker-entrypoint.sh", "deploy/container-healthcheck.sh"):
        row = next((item for item in transitions if item["context_path"] == script), None)
        if row is None or row["source_kind"] != "build_tree":
            raise CandidateBuildError(f"入口脚本未从 build tree 装配：{script}")
        approved_source = source_root / PurePosixPath(script)
        if approved_source.is_symlink() or not approved_source.is_file():
            raise CandidateBuildError(f"批准源码缺少可信入口脚本：{script}")
        approved_binding = {
            "path": str(approved_source.resolve(strict=True)),
            "mode": _entry_mode(approved_source.stat().st_mode),
            "size": approved_source.stat().st_size,
            "sha256": file_sha256(approved_source),
        }
        if (
            approved_binding["mode"] != "0755"
            or row["source"]["mode"] != "0755"
            or row["context"]["mode"] != "0755"
            or any(
                approved_binding[field] != row["source"][field]
                or approved_binding[field] != row["context"][field]
                for field in ("size", "sha256")
            )
        ):
            raise CandidateBuildError(
                f"入口脚本必须从批准源码到 build tree/context 严格保持内容与 0755：{script}"
            )
        row["approved_source"] = approved_binding
    binary_row = next((item for item in transitions if item["context_path"] == "sub2api"), None)
    if binary_row is None or binary_row["source_kind"] != "binary" or binary_row["context"]["mode"] != "0755":
        raise CandidateBuildError("context/sub2api 必须来自当前 binary 且严格为 0755")
    return _self_bound(
        {
            "schema_version": INVENTORY_RECEIPT_SCHEMA,
            "candidate_id": candidate_id,
            "image_id": image_id,
            "build_tree": build_inventory,
            "docker_context": context_inventory,
            "assembly": transitions,
            "assembly_sha256": digest(transitions),
        }
    )


def validate_inventory_receipt(value: Any, *, candidate_id: str, image_id: str) -> dict[str, Any]:
    payload = _require_fields(
        value,
        {
            "schema_version",
            "candidate_id",
            "image_id",
            "build_tree",
            "docker_context",
            "assembly",
            "assembly_sha256",
            "receipt_digest",
        },
        "build inventory 收据",
    )
    if (
        payload.get("schema_version") != INVENTORY_RECEIPT_SCHEMA
        or payload.get("candidate_id") != candidate_id
        or payload.get("image_id") != image_id
        or digest(payload.get("assembly")) != payload.get("assembly_sha256")
    ):
        raise CandidateBuildError("build inventory 收据身份或 assembly 摘要不一致")
    _verify_self_digest(payload, "build inventory 收据")
    return payload


def replay_inventory_receipt(
    value: Any,
    parameters: Mapping[str, Any],
    *,
    candidate_id: str,
    image_id: str,
    source_root: Path,
    build_tree: Path,
    docker_context: Path,
    binary_path: Path,
) -> dict[str, Any]:
    recorded = validate_inventory_receipt(value, candidate_id=candidate_id, image_id=image_id)
    current = build_inventory_receipt(
        parameters,
        candidate_id=candidate_id,
        image_id=image_id,
        source_root=source_root,
        build_tree=build_tree,
        docker_context=docker_context,
        binary_path=binary_path,
    )
    if current != recorded:
        raise CandidateBuildError("build tree/context inventory 或装配关系在 replay 时漂移")
    return recorded


def _validate_frontend_builder_receipt(
    value: Any,
    *,
    builder_identity: str,
    source_git_commit: str,
    build_command: list[str],
    node_version: str,
    pnpm_version: str,
    package_manifest_sha256: str,
    lockfile_sha256: str,
    dist_inventory_sha256: str,
) -> dict[str, Any]:
    payload = _require_fields(
        value,
        {
            "schema_version",
            "status",
            "builder_identity",
            "source_git_commit",
            "build_command",
            "node_version",
            "pnpm_version",
            "package_manifest_sha256",
            "lockfile_sha256",
            "dist_inventory_sha256",
            "live_request_count",
            "built_at_utc",
            "receipt_digest",
        },
        "前端 builder 收据",
    )
    expected = {
        "schema_version": FRONTEND_BUILDER_SCHEMA,
        "status": "complete",
        "builder_identity": builder_identity,
        "source_git_commit": source_git_commit,
        "build_command": build_command,
        "node_version": node_version,
        "pnpm_version": pnpm_version,
        "package_manifest_sha256": package_manifest_sha256,
        "lockfile_sha256": lockfile_sha256,
        "dist_inventory_sha256": dist_inventory_sha256,
        "live_request_count": 0,
    }
    if any(payload.get(key) != expected_value for key, expected_value in expected.items()):
        raise CandidateBuildError("前端 builder 收据未绑定当前源码、工具链或 dist")
    if not isinstance(payload.get("built_at_utc"), str) or not payload["built_at_utc"]:
        raise CandidateBuildError("前端 builder 收据 built_at_utc 为空")
    _verify_self_digest(payload, "前端 builder 收据")
    return payload


def _validate_toolchain_approval(
    value: Any,
    *,
    candidate_id: str,
    node_version: str,
    builder_kind: str,
) -> dict[str, Any]:
    payload = _require_fields(
        value,
        {
            "schema_version",
            "status",
            "candidate_id",
            "expected",
            "actual",
            "reason",
            "approved_by",
            "approved_at_utc",
            "receipt_digest",
        },
        "前端工具链偏差批准收据",
    )
    if (
        payload.get("schema_version") != TOOLCHAIN_APPROVAL_SCHEMA
        or payload.get("status") != "approved"
        or payload.get("candidate_id") != candidate_id
        or payload.get("expected")
        != {"node_major": 20, "builder_kind": "release_pipeline"}
        or payload.get("actual")
        != {"node_version": node_version, "builder_kind": builder_kind}
        or not all(
            isinstance(payload.get(key), str) and payload[key].strip()
            for key in ("reason", "approved_by", "approved_at_utc")
        )
    ):
        raise CandidateBuildError("前端工具链偏差批准收据未精确批准当前偏差")
    _verify_self_digest(payload, "前端工具链偏差批准收据")
    return payload


def build_frontend_provenance(
    parameters: Mapping[str, Any],
    *,
    candidate_id: str,
    image_id: str,
    source_root: Path,
    git_commit: str,
    build_tree: Path,
    frontend_dist_source: Path,
) -> dict[str, Any]:
    frontend = parameters["frontend"]
    frontend_source = source_root / PurePosixPath(frontend["source_root"])
    package_manifest = frontend_source / PurePosixPath(frontend["package_manifest"])
    lockfile = frontend_source / PurePosixPath(frontend["lockfile"])
    for path, label in ((package_manifest, "package manifest"), (lockfile, "lockfile")):
        if path.is_symlink() or not path.is_file():
            raise CandidateBuildError(f"前端 {label} 不存在或不可信")
    source_dist_inventory = scan_tree_inventory(frontend_dist_source)
    build_tree_dist = build_tree / PurePosixPath(frontend["dist_build_tree_path"])
    build_tree_dist_inventory = scan_tree_inventory(build_tree_dist)
    comparable_fields = (
        "entry_count",
        "file_count",
        "directory_count",
        "total_file_bytes",
        "tree_sha256",
        "inventory_sha256",
        "entries",
    )
    if any(source_dist_inventory[field] != build_tree_dist_inventory[field] for field in comparable_fields):
        raise CandidateBuildError("前端 dist 注入 build tree 后内容或 mode 漂移")
    package_sha = file_sha256(package_manifest)
    lockfile_sha = file_sha256(lockfile)
    builder = frontend["builder"]
    builder_binding = _validate_file_binding(builder["receipt"], "frontend.builder.receipt")
    builder_receipt = _validate_frontend_builder_receipt(
        _read_bound_json(builder_binding, "前端 builder 收据"),
        builder_identity=builder["identity"],
        source_git_commit=git_commit,
        build_command=frontend["build_command"],
        node_version=frontend["node_version"],
        pnpm_version=frontend["pnpm_version"],
        package_manifest_sha256=package_sha,
        lockfile_sha256=lockfile_sha,
        dist_inventory_sha256=source_dist_inventory["inventory_sha256"],
    )
    policy = frontend["toolchain_policy"]
    approval_binding = policy["deviation_approval"]
    approval_digest: str | None = None
    if approval_binding is not None:
        approval_binding = _validate_file_binding(approval_binding, "toolchain deviation approval")
        approval = _validate_toolchain_approval(
            _read_bound_json(approval_binding, "前端工具链偏差批准收据"),
            candidate_id=candidate_id,
            node_version=frontend["node_version"],
            builder_kind=builder["kind"],
        )
        approval_digest = approval["receipt_digest"]
    payload = {
        "schema_version": FRONTEND_PROVENANCE_SCHEMA,
        "candidate_id": candidate_id,
        "image_id": image_id,
        "source": {
            "git_commit": git_commit,
            "frontend_root": str(frontend_source.resolve(strict=True)),
            "package_manifest": {
                "path": str(package_manifest.resolve(strict=True)),
                "sha256": package_sha,
                "bytes": package_manifest.stat().st_size,
            },
            "lockfile": {
                "path": str(lockfile.resolve(strict=True)),
                "sha256": lockfile_sha,
                "bytes": lockfile.stat().st_size,
            },
        },
        "build": {
            "command": list(frontend["build_command"]),
            "node_version": frontend["node_version"],
            "pnpm_version": frontend["pnpm_version"],
            "builder_kind": builder["kind"],
            "builder_identity": builder["identity"],
            "builder_receipt_digest": builder_receipt["receipt_digest"],
            "required_node_major": 20,
            "deviation_approval_digest": approval_digest,
        },
        "dist_transition": {
            "source": source_dist_inventory,
            "build_tree": build_tree_dist_inventory,
            "content_and_mode_equal": True,
        },
        "live_request_count": 0,
    }
    return _self_bound(payload)


def validate_frontend_provenance(value: Any, *, candidate_id: str, image_id: str) -> dict[str, Any]:
    payload = _require_fields(
        value,
        {
            "schema_version",
            "candidate_id",
            "image_id",
            "source",
            "build",
            "dist_transition",
            "live_request_count",
            "receipt_digest",
        },
        "前端 provenance 收据",
    )
    transition = payload.get("dist_transition")
    if (
        payload.get("schema_version") != FRONTEND_PROVENANCE_SCHEMA
        or payload.get("candidate_id") != candidate_id
        or payload.get("image_id") != image_id
        or payload.get("live_request_count") != 0
        or not isinstance(transition, Mapping)
        or set(transition) != {"source", "build_tree", "content_and_mode_equal"}
        or transition.get("content_and_mode_equal") is not True
    ):
        raise CandidateBuildError("前端 provenance 身份、transition 或请求边界非法")
    source_inventory = transition["source"]
    build_tree_inventory = transition["build_tree"]
    comparable_fields = (
        "entry_count",
        "file_count",
        "directory_count",
        "total_file_bytes",
        "tree_sha256",
        "inventory_sha256",
        "entries",
    )
    if (
        not isinstance(source_inventory, Mapping)
        or not isinstance(build_tree_inventory, Mapping)
        or any(
            source_inventory.get(field) != build_tree_inventory.get(field)
            for field in comparable_fields
        )
    ):
        raise CandidateBuildError("前端 provenance 的 dist transition 并不逐项相等")
    _verify_self_digest(payload, "前端 provenance 收据")
    return payload


def replay_frontend_provenance(
    value: Any,
    parameters: Mapping[str, Any],
    *,
    candidate_id: str,
    image_id: str,
    source_root: Path,
    git_commit: str,
    build_tree: Path,
    frontend_dist_source: Path,
) -> dict[str, Any]:
    recorded = validate_frontend_provenance(value, candidate_id=candidate_id, image_id=image_id)
    current = build_frontend_provenance(
        parameters,
        candidate_id=candidate_id,
        image_id=image_id,
        source_root=source_root,
        git_commit=git_commit,
        build_tree=build_tree,
        frontend_dist_source=frontend_dist_source,
    )
    if current != recorded:
        raise CandidateBuildError("前端源码、lockfile、toolchain、builder 或 dist 在 replay 时漂移")
    return recorded


def _default_runner(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CandidateBuildError(f"构建实物命令无法执行：{arguments[0]}") from error


def _run_checked(arguments: Sequence[str], label: str, runner: CommandRunner) -> str:
    completed = runner(arguments)
    if completed.returncode != 0:
        raise CandidateBuildError(f"{label}失败：{completed.stderr.strip()}")
    return completed.stdout


def _parse_go_build_info(output: str) -> dict[str, Any]:
    lines = output.splitlines()
    if not lines or ": go" not in lines[0]:
        raise CandidateBuildError("go version -m 输出非法")
    build: dict[str, str] = {}
    for line in lines[1:]:
        parts = line.strip().split("\t")
        if len(parts) >= 2 and parts[0] == "build" and "=" in parts[1]:
            key, value = parts[1].split("=", 1)
            build[key] = value.strip('"')
    tags = sorted(tag for tag in build.get("-tags", "").split(",") if tag)
    return {
        "go_version": lines[0].rsplit(": ", 1)[-1],
        "tags": tags,
        "goos": build.get("GOOS", ""),
        "goarch": build.get("GOARCH", ""),
        "cgo_enabled": build.get("CGO_ENABLED", ""),
        "vcs_revision": build.get("vcs.revision", ""),
        "vcs_modified": build.get("vcs.modified", ""),
    }


def _inspect_image_files(image_id: str, runner: CommandRunner) -> dict[str, dict[str, Any]]:
    script = (
        "set -eu; "
        "for p in /app/docker-entrypoint.sh /app/container-healthcheck.sh /app/sub2api; do "
        "test -f \"$p\"; test ! -L \"$p\"; "
        "printf '%s\\t%s\\t%s\\t%s\\n' \"$p\" \"$(stat -c %a \"$p\")\" "
        "\"$(stat -c %s \"$p\")\" \"$(sha256sum \"$p\" | cut -d' ' -f1)\"; done"
    )
    output = _run_checked(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            image_id,
            "-c",
            script,
        ],
        "镜像入口文件检查",
        runner,
    )
    result: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] not in REQUIRED_IMAGE_FILES:
            raise CandidateBuildError("镜像入口文件检查输出非法")
        path, mode, raw_size, sha256 = parts
        try:
            size = int(raw_size)
        except ValueError as error:
            raise CandidateBuildError("镜像入口文件大小非法") from error
        if mode != "755" or size <= 0 or not SHA256_RE.fullmatch(sha256):
            raise CandidateBuildError(f"镜像文件必须为普通 0755 且不可 group/other 写：{path}")
        result[path] = {"type": "file", "mode": "0755", "size": size, "sha256": sha256}
    if set(result) != set(REQUIRED_IMAGE_FILES):
        raise CandidateBuildError("镜像入口文件检查未覆盖三项闭集")
    return result


def build_image_inspection(
    parameters: Mapping[str, Any],
    *,
    candidate_id: str,
    runtime_image: str,
    image_id: str,
    binary_path: Path,
    docker_context: Path,
    git_commit: str,
    target_architecture: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    runner = runner or _default_runner
    raw = _run_checked(["docker", "image", "inspect", image_id], "Docker image inspect", runner)
    try:
        inspected = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CandidateBuildError("Docker image inspect 输出不是合法 JSON") from error
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise CandidateBuildError("Docker image inspect 输出闭集非法")
    image = inspected[0]
    config = image.get("Config") if isinstance(image.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    repo_digests = image.get("RepoDigests")
    target_os, target_arch = target_architecture.split("/", 1)
    expected_labels = parameters["docker_build"]["labels"]
    if (
        image.get("Id") != image_id
        or not isinstance(repo_digests, list)
        or runtime_image not in repo_digests
        or image.get("Os") != target_os
        or image.get("Architecture") != target_arch
        or config.get("Entrypoint") != EXPECTED_ENTRYPOINT
        or labels.get("org.opencontainers.image.revision") != git_commit
        or labels.get("org.opencontainers.image.version")
        != expected_labels["org.opencontainers.image.version"]
    ):
        raise CandidateBuildError("镜像 ID、RepoDigest、架构、Entrypoint 或 OCI 标签与构建合同不一致")
    if not str(labels["org.opencontainers.image.version"]).endswith(f"-{candidate_id}"):
        raise CandidateBuildError("镜像实际 OCI version 未以当前 Candidate ID 结尾")

    files = _inspect_image_files(image_id, runner)
    external_binary = {
        "size": binary_path.stat().st_size,
        "sha256": file_sha256(binary_path),
    }
    if any(files["/app/sub2api"][field] != external_binary[field] for field in ("size", "sha256")):
        raise CandidateBuildError("receipt/parameters 二进制与镜像内 /app/sub2api 不一致")
    context_sources = {
        "/app/docker-entrypoint.sh": docker_context / "deploy/docker-entrypoint.sh",
        "/app/container-healthcheck.sh": docker_context / "deploy/container-healthcheck.sh",
    }
    for image_path, context_path in context_sources.items():
        if context_path.is_symlink() or not context_path.is_file():
            raise CandidateBuildError(f"Docker context 入口脚本不存在：{context_path}")
        if (
            _entry_mode(context_path.stat().st_mode) != "0755"
            or files[image_path]["sha256"] != file_sha256(context_path)
            or files[image_path]["size"] != context_path.stat().st_size
        ):
            raise CandidateBuildError(f"context 与镜像入口脚本内容或 mode 不一致：{image_path}")

    go_output = _run_checked(["go", "version", "-m", str(binary_path)], "Go build info 检查", runner)
    go_info = _parse_go_build_info(go_output)
    required_tags = set(parameters["go_build"]["required_tags"])
    if (
        not required_tags.issubset(set(go_info["tags"]))
        or go_info["vcs_revision"] != git_commit
        or go_info["goos"] != target_os
        or go_info["goarch"] != target_arch
        or go_info["cgo_enabled"] != parameters["go_build"]["environment"]["CGO_ENABLED"]
        or go_info["vcs_modified"] != "false"
    ):
        raise CandidateBuildError("实际 Go build tags、revision、平台或 clean 状态不满足构建合同")
    selected_labels = {
        key: labels[key]
        for key in sorted(expected_labels)
    }
    return _self_bound(
        {
            "schema_version": IMAGE_INSPECTION_SCHEMA,
            "candidate_id": candidate_id,
            "image_id": image_id,
            "runtime_reference": runtime_image,
            "repo_digests": sorted(repo_digests),
            "os": target_os,
            "architecture": target_arch,
            "labels": selected_labels,
            "entrypoint": list(config["Entrypoint"]),
            "files": files,
            "go_build_info": go_info,
        }
    )


def validate_image_inspection(value: Any, *, candidate_id: str, image_id: str) -> dict[str, Any]:
    payload = _require_fields(
        value,
        {
            "schema_version",
            "candidate_id",
            "image_id",
            "runtime_reference",
            "repo_digests",
            "os",
            "architecture",
            "labels",
            "entrypoint",
            "files",
            "go_build_info",
            "receipt_digest",
        },
        "镜像 inspection 收据",
    )
    files = payload.get("files")
    labels = payload.get("labels")
    go_info = payload.get("go_build_info")
    runtime_reference = payload.get("runtime_reference")
    repo_digests = payload.get("repo_digests")
    if (
        payload.get("schema_version") != IMAGE_INSPECTION_SCHEMA
        or payload.get("candidate_id") != candidate_id
        or payload.get("image_id") != image_id
        or payload.get("entrypoint") != EXPECTED_ENTRYPOINT
        or not isinstance(runtime_reference, str)
        or not IMAGE_REFERENCE_RE.fullmatch(runtime_reference)
        or not isinstance(repo_digests, list)
        or runtime_reference not in repo_digests
        or not isinstance(labels, Mapping)
        or set(labels)
        != {"org.opencontainers.image.revision", "org.opencontainers.image.version"}
        or not GIT_COMMIT_RE.fullmatch(str(labels.get("org.opencontainers.image.revision", "")))
        or not isinstance(files, Mapping)
        or set(files) != set(REQUIRED_IMAGE_FILES)
        or not isinstance(go_info, Mapping)
    ):
        raise CandidateBuildError("镜像 inspection 收据身份、入口或文件闭集非法")
    for path, row in files.items():
        if (
            not isinstance(row, Mapping)
            or set(row) != {"type", "mode", "size", "sha256"}
            or row.get("type") != "file"
            or row.get("mode") != "0755"
            or not isinstance(row.get("size"), int)
            or row["size"] <= 0
            or not SHA256_RE.fullmatch(str(row.get("sha256", "")))
        ):
            raise CandidateBuildError(f"镜像 inspection 文件事实非法：{path}")
    _verify_self_digest(payload, "镜像 inspection 收据")
    return payload


def replay_image_inspection(
    value: Any,
    parameters: Mapping[str, Any],
    *,
    candidate_id: str,
    runtime_image: str,
    image_id: str,
    binary_path: Path,
    docker_context: Path,
    git_commit: str,
    target_architecture: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    recorded = validate_image_inspection(value, candidate_id=candidate_id, image_id=image_id)
    current = build_image_inspection(
        parameters,
        candidate_id=candidate_id,
        runtime_image=runtime_image,
        image_id=image_id,
        binary_path=binary_path,
        docker_context=docker_context,
        git_commit=git_commit,
        target_architecture=target_architecture,
        runner=runner,
    )
    if current != recorded:
        raise CandidateBuildError("Docker 镜像实物在 replay 时漂移")
    return recorded


def build_capability_probe(
    *,
    candidate_id: str,
    image_id: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    runner = runner or _default_runner
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/app/sub2api",
        image_id,
        "--candidate-capture-capability-probe",
    ]
    output = _run_checked(command, "candidatecapture capability probe", runner).strip()
    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        raise CandidateBuildError("candidatecapture capability probe 输出不是合法 JSON") from error
    expected = {
        "schema_version": CAPABILITY_OUTPUT_SCHEMA,
        "capability": "candidatecapture",
        "status": "available",
        "provider_check_passed": True,
        "provider_generate_passed": True,
        "live_request_count": 0,
    }
    if not isinstance(result, dict) or result != expected:
        raise CandidateBuildError("candidatecapture provider 未通过镜像内实际能力探针")
    return _self_bound(
        {
            "schema_version": CAPABILITY_PROBE_SCHEMA,
            "candidate_id": candidate_id,
            "image_id": image_id,
            "network_mode": "none",
            "command": command,
            "result": result,
            "live_request_count": 0,
        }
    )


def validate_capability_probe(value: Any, *, candidate_id: str, image_id: str) -> dict[str, Any]:
    payload = _require_fields(
        value,
        {
            "schema_version",
            "candidate_id",
            "image_id",
            "network_mode",
            "command",
            "result",
            "live_request_count",
            "receipt_digest",
        },
        "capability probe 收据",
    )
    result = payload.get("result")
    expected_command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/app/sub2api",
        image_id,
        "--candidate-capture-capability-probe",
    ]
    expected_result = {
        "schema_version": CAPABILITY_OUTPUT_SCHEMA,
        "capability": "candidatecapture",
        "status": "available",
        "provider_check_passed": True,
        "provider_generate_passed": True,
        "live_request_count": 0,
    }
    if (
        payload.get("schema_version") != CAPABILITY_PROBE_SCHEMA
        or payload.get("candidate_id") != candidate_id
        or payload.get("image_id") != image_id
        or payload.get("network_mode") != "none"
        or payload.get("live_request_count") != 0
        or payload.get("command") != expected_command
        or result != expected_result
    ):
        raise CandidateBuildError("capability probe 未绑定当前 image 或产生了未登记 live request")
    _verify_self_digest(payload, "capability probe 收据")
    return payload


def replay_capability_probe(
    value: Any,
    *,
    candidate_id: str,
    image_id: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    recorded = validate_capability_probe(value, candidate_id=candidate_id, image_id=image_id)
    current = build_capability_probe(candidate_id=candidate_id, image_id=image_id, runner=runner)
    if current != recorded:
        raise CandidateBuildError("candidatecapture capability probe 在 replay 时漂移")
    return recorded
