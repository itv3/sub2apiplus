#!/usr/bin/env python3
"""在 ARM64 上受独立监督器保护地启用 Codex 0.154 工具和文档。

本脚本只负责受管工具和活动文档的同一部署事务，不执行官方请求，也不读取证据。
所有外部命令都经由 ``codex_upgrade_supervisor.SupervisorClient``，文件操作前后均
写入事件账本。发布后的任一校验失败时，会同时恢复旧工具和旧文档，并保留失败
的新版本以便审计。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


# 2026-09-09：candidate_test_fact_map_0_151_0.json 与 candidate_test_trace.py 在合并
# Sub2API v0.2.3 后重绑五份生产源码摘要；事实和规则语义不变，受管工具树摘要随之更新。
# 2026-09-11：codex_upgrade.py 新增候选层运行坐标覆盖并把 campaign-run 强制派发泛化到
# 全部未来目标版本，codex_upgrade_supervisor.py 修正 campaign-mark 与派发超时的竞争，
# codex_upgrade_legacy_boundary.py 去掉版本字面量；受管工具树与监督器摘要随之更新。
# 2026-09-11（批次 2）：codex_upgrade.py 新增 reuse-official-evidence 正式命令，把已封存官方
# 阶段只读导入新 Campaign；受管工具树摘要随之更新。
# 2026-09-11（0.154）：新增目标版本清单、Astra Lite 轨模型政策和 0.154 升级对；部署坐标
# 统一迁入 /root/docker/capture-cli/data，受管工具树摘要随之更新。
# 2026-09-12（0.154 补丁绑定修复）：画像补丁清单改为绑定活动画像文件 SHA-256，
# 并增加穿过正式画像派生校验的离线回归；规则语义与网络行为不变。
# 2026-09-12（canonical 生产链修复）：fresh Candidate attempt 可原生初始化
# checkpoint，并由显式版本参数决定旧 Previous 退休项；受管工具摘要随之更新。
DEFAULT_TOOL_DIGEST = (
    "124856cbcd0a61aa63db6edec7919af64c2ae9df1294d592fa8739485cf968d7"
)
DEFAULT_SUPERVISOR_DIGEST = (
    "f12daa3edb619546081141ac736af56dfb34b4cadff3bf63fe8dacabf22d80b4"
)
DEFAULT_ASSERTION_PREPARER_DIGEST = (
    "b9ca7b6f48b3c33a63a864a9ce7ebd617335378ea52fe9148ea161ab3b276ada"
)
RENAME_EXCHANGE = 2
AT_FDCWD = -100
SAFE_SUFFIXES = {".py", ".sh", ".json"}
MANAGED_DOCUMENTS = (
    "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
)
# 计时账本会在运行时从仓库根读取这些来源链文档。它们不是普通说明文档，
# 而是历史 producer 摘要承接的直接依赖，必须与工具树放在同一部署事务中。
MANAGED_RUNTIME_DOCUMENTS = (
    "egress/maintenance/codex-cli-0151-model-policy-tool-successor-source-transition.json",
    "egress/maintenance/codex-cli-0151-container-path-recovery-tool-successor-source-transition.json",
    "egress/maintenance/codex-cli-0151-timing-producer-replay-tool-successor-source-transition.json",
    "egress/maintenance/codex-cli-0151-producer-coordinate-decoupling-source-transition.json",
    "egress/maintenance/codex-cli-0151-worktree-successor.json",
)
MANAGED_ASSERTION_PREPARER = "prepare_assertion_bundle.sh"
TARGET_SCENARIO_MANIFEST = "codex_upgrade_scenarios_0_154_0.json"
SOURCE_SPEC_HEADINGS = {
    "第二章": "# 第二部分 Codex CLI 客户端规则画像",
    "第二部分": "# 第二部分 Codex CLI 客户端规则画像",
    "第二部分-规则": "# 第二部分 Codex CLI 客户端规则画像",
}
WG1_CONFIG = Path("/etc/wireguard/wg1.conf")
WG1_RUNTIME_MTU = Path("/sys/class/net/wg1/mtu")
EXPECTED_DMIT_WG1_MTU = 1420


class DeploymentError(RuntimeError):
    """部署前提或发布后校验失败。"""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tool_entries(root: Path) -> list[dict[str, str]]:
    """按官方编排器相同的边界生成工具文件清单。"""

    if root.is_symlink() or not root.is_dir():
        raise DeploymentError(f"工具树不是可信目录：{root}")
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if (
            path.suffix not in SAFE_SUFFIXES
            or "tests" in relative.parts
            or "versions" in relative.parts
            or "__pycache__" in relative.parts
        ):
            continue
        entries.append(
            {
                "path": relative.as_posix(),
                "sha256": file_sha256(path),
            }
        )
    return entries


def tool_digest(root: Path) -> tuple[str, int]:
    entries = tool_entries(root)
    return sha256_bytes(canonical({"entries": entries})), len(entries)


def reject_untrusted_tree(root: Path) -> int:
    """拒绝符号链接、非 root 属主和 group/other 写权限。"""

    count = 0
    paths = [root, *root.rglob("*")]
    for path in paths:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise DeploymentError(f"工具树含符号链接：{path}")
        if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
            count += 1
    if count:
        raise DeploymentError(f"工具树存在 {count} 个属主或写权限不安全项：{root}")
    return len(paths)


def normalize_tree_permissions(root: Path) -> int:
    """把新候选树统一为 root:root，并移除 group/other 写权限。"""

    count = 0
    for path in [root, *root.rglob("*")]:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise DeploymentError(f"候选树含符号链接：{path}")
        os.chown(path, 0, 0)
        os.chmod(path, stat.S_IMODE(metadata.st_mode) & ~0o022)
        count += 1
    return count


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_exchange(first: Path, second: Path) -> None:
    """使用 Linux renameat2(RENAME_EXCHANGE) 交换两个同类型文件系统对象。"""

    if first.is_symlink() or second.is_symlink():
        raise DeploymentError("原子交换目标不得是符号链接。")
    if not first.exists() or not second.exists():
        raise DeploymentError("原子交换目标必须存在。")
    if first.is_dir() != second.is_dir() or first.is_file() != second.is_file():
        raise DeploymentError("原子交换目标类型不一致。")
    if first.parent.stat().st_dev != second.parent.stat().st_dev:
        raise DeploymentError("原子交换目标不在同一文件系统。")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.renameat2
    except AttributeError as error:
        raise DeploymentError("ARM64 内核/运行库不提供 renameat2。") from error
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        AT_FDCWD,
        os.fsencode(first),
        AT_FDCWD,
        os.fsencode(second),
        RENAME_EXCHANGE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    fsync_directory(first.parent)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        data = canonical(payload) + b"\n"
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def reject_untrusted_file(path: Path, *, label: str) -> None:
    """拒绝非 root 所有、可被其他用户写入或由符号链接替代的文件。"""

    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"{label}不是可信普通文件：{path}")
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
        raise DeploymentError(f"{label}属主或写权限不安全：{path}")


def managed_document_path(
    root: Path,
    relative: str,
    *,
    label: str,
    allow_missing: bool,
) -> Path:
    """解析受管文档坐标，并拒绝任一路径组件被符号链接替代。"""

    parsed = Path(relative)
    if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
        raise DeploymentError(f"{label}不是规范相对路径：{relative}")
    current = root
    for index, part in enumerate(parsed.parts):
        current /= part
        if current.is_symlink():
            raise DeploymentError(f"{label}路径包含符号链接：{current}")
        if current.exists() and index < len(parsed.parts) - 1 and not current.is_dir():
            raise DeploymentError(f"{label}父路径不是目录：{current}")
        if not current.exists() and not allow_missing:
            raise DeploymentError(f"{label}缺失：{current}")
    return current


def legacy_production_document_facts(path: Path) -> dict[str, Any]:
    """冻结首次纳管的旧文档；只豁免属主，不豁免类型或写权限。"""

    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"旧生产活动文档不是可信普通文件：{path}")
    metadata = path.stat()
    if metadata.st_mode & 0o022:
        raise DeploymentError(f"旧生产活动文档可被组或其他用户写入：{path}")
    return {
        "sha256": file_sha256(path),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def source_spec_section_sha256(source_path: Path, fragment: str) -> str:
    """按受管场景清单的章节锚点复算活动文档摘要。"""

    expected_heading = SOURCE_SPEC_HEADINGS.get(fragment)
    if expected_heading is None:
        raise DeploymentError(f"场景清单章节锚点不受支持：{fragment}")
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as error:
        raise DeploymentError("无法读取活动规格章节。") from error
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.rstrip("\r\n") == expected_heading
        ),
        None,
    )
    if start is None:
        raise DeploymentError(f"活动规格缺少章节：{expected_heading}")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("# ")
        ),
        len(lines),
    )
    return sha256_bytes("".join(lines[start:end]).encode("utf-8"))


def verify_scenario_source_spec(
    runtime_root: Path,
    tool_root: Path,
) -> dict[str, str]:
    """现场验证 0.151 场景清单绑定的活动文档章节。"""

    manifest_path = tool_root / TARGET_SCENARIO_MANIFEST
    reject_untrusted_file(manifest_path, label="目标场景清单")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentError("无法读取目标场景清单。") from error
    source_spec = manifest.get("source_spec") if isinstance(manifest, dict) else None
    if not isinstance(source_spec, dict):
        raise DeploymentError("目标场景清单缺少 source_spec。")
    path_text = source_spec.get("path")
    fragment = source_spec.get("fragment")
    expected_sha256 = source_spec.get("sha256")
    if (
        not isinstance(path_text, str)
        or not path_text
        or Path(path_text).is_absolute()
        or ".." in Path(path_text).parts
        or not isinstance(fragment, str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256))
    ):
        raise DeploymentError("目标场景清单 source_spec 非法。")
    source_path = runtime_root / path_text
    reject_untrusted_file(source_path, label="活动规格文档")
    actual_sha256 = source_spec_section_sha256(source_path, fragment)
    if actual_sha256 != expected_sha256:
        raise DeploymentError("活动规格第二章摘要与目标场景清单不一致。")
    return {
        "manifest": str(manifest_path),
        "source_spec": f"{path_text}#{fragment}",
        "source_spec_sha256": actual_sha256,
    }


def load_supervisor(staging_root: Path) -> Any:
    module_path = staging_root / "tools" / "official_client_capture" / "codex_upgrade_supervisor.py"
    if module_path.is_symlink() or not module_path.is_file():
        raise DeploymentError("暂存监督器文件不存在或不可信。")
    spec = importlib.util.spec_from_file_location("arm64_staging_supervisor", module_path)
    if spec is None or spec.loader is None:
        raise DeploymentError("无法加载暂存监督器。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_checked(
    client: Any,
    operation: str,
    argv: list[str],
    *,
    timeout_seconds: float = 120.0,
) -> str:
    result = client.run_command(
        argv,
        operation=operation,
        timeout_seconds=timeout_seconds,
        capture_output=True,
        text=True,
        merge_stderr=True,
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        raise DeploymentError(f"{operation} 返回码 {result.returncode}")
    return output


def record_step(
    client: Any,
    operation: str,
    action: Callable[[], Mapping[str, Any] | None],
) -> dict[str, Any]:
    client.event_start(operation)
    try:
        result = dict(action() or {})
    except BaseException as error:
        try:
            client.event_fail(operation, reason=type(error).__name__)
        except BaseException:
            pass
        raise
    client.event_end(operation, metadata=result)
    return result


def parse_container_network(output: str, name: str) -> str:
    try:
        networks = json.loads(output)
    except json.JSONDecodeError as error:
        raise DeploymentError(f"无法解析 {name} 容器网络事实。") from error
    if not isinstance(networks, dict):
        raise DeploymentError(f"{name} 容器网络事实不是对象。")
    addresses = {
        str(value.get("IPAddress"))
        for value in networks.values()
        if isinstance(value, dict) and value.get("IPAddress")
    }
    if name == "capture-cli" and "172.30.0.10" not in addresses:
        raise DeploymentError(f"capture-cli 固定 IP 漂移：{sorted(addresses)}")
    if name == "sub2apiplus" and "172.25.0.3" not in addresses:
        raise DeploymentError(f"sub2apiplus 固定 IP 漂移：{sorted(addresses)}")
    return ",".join(sorted(addresses))


def verify_wg1_mtu() -> dict[str, Any]:
    """验证 ARM64 wg1 的持久配置和运行时值均匹配 DMIT。"""

    if WG1_CONFIG.is_symlink() or not WG1_CONFIG.is_file():
        raise DeploymentError("ARM64 wg1 配置不是可信普通文件。")
    metadata = WG1_CONFIG.stat()
    if (
        metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise DeploymentError("ARM64 wg1 配置必须为 root:root 0600。")
    try:
        raw = WG1_CONFIG.read_bytes()
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise DeploymentError("ARM64 wg1 配置不可读。") from error
    section: str | None = None
    configured_values: list[int] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "interface" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().lower() != "mtu":
            continue
        try:
            configured_values.append(int(value.strip()))
        except ValueError as error:
            raise DeploymentError("ARM64 wg1 配置 MTU 非整数。") from error
    try:
        runtime_mtu = int(WG1_RUNTIME_MTU.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as error:
        raise DeploymentError("ARM64 wg1 运行时 MTU 不可读。") from error
    if configured_values != [EXPECTED_DMIT_WG1_MTU]:
        raise DeploymentError(
            f"ARM64 wg1 配置 MTU 必须唯一且等于 DMIT {EXPECTED_DMIT_WG1_MTU}。"
        )
    if runtime_mtu != EXPECTED_DMIT_WG1_MTU:
        raise DeploymentError(
            f"ARM64 wg1 运行时 MTU 与 DMIT {EXPECTED_DMIT_WG1_MTU} 不一致。"
        )
    return {
        "interface": "wg1",
        "configured_mtu": configured_values[0],
        "runtime_mtu": runtime_mtu,
        "expected_dmit_mtu": EXPECTED_DMIT_WG1_MTU,
        "config_sha256": sha256_bytes(raw),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=Path("/root/docker/capture-cli/data/staging/codex-0.154.0-managed-tools"),
    )
    parser.add_argument(
        "--production-root",
        type=Path,
        default=Path("/root/docker/capture-cli/data/tools/official_client_capture"),
    )
    parser.add_argument(
        "--production-doc-root",
        type=Path,
        default=Path("/root/docker/capture-cli/data/docs"),
    )
    parser.add_argument(
        "--control-root",
        type=Path,
        default=Path("/root/docker/capture-cli/data/control"),
    )
    parser.add_argument("--expected-tool-digest", default=DEFAULT_TOOL_DIGEST)
    parser.add_argument("--expected-supervisor-digest", default=DEFAULT_SUPERVISOR_DIGEST)
    parser.add_argument(
        "--expected-assertion-preparer-digest",
        default=DEFAULT_ASSERTION_PREPARER_DIGEST,
    )
    parser.add_argument("--max-wall-seconds", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    staging_root = arguments.staging_root.resolve(strict=True)
    production_root = arguments.production_root.resolve(strict=True)
    production_assertion_preparer = production_root.parent / MANAGED_ASSERTION_PREPARER
    production_doc_root = arguments.production_doc_root.resolve(strict=True)
    control_root = arguments.control_root.resolve(strict=True)
    if os.geteuid() != 0:
        raise DeploymentError("ARM64 启用必须由 root 执行。")
    if platform.machine() != "aarch64":
        raise DeploymentError("启用脚本只能在 ARM64 主机执行。")
    if control_root.is_symlink() or not control_root.is_dir():
        raise DeploymentError("控制目录不可信。")
    if stat.S_IMODE(control_root.stat().st_mode) != 0o700 or control_root.stat().st_uid != 0:
        raise DeploymentError("控制目录必须是 root 拥有的 0700 目录。")
    staging_tool = staging_root / "tools" / "official_client_capture"
    if staging_tool.is_symlink() or not staging_tool.is_dir():
        raise DeploymentError("暂存工具树不存在或不可信。")
    if production_root.is_symlink() or not production_root.is_dir():
        raise DeploymentError("生产工具树不存在或不可信。")
    supervisor = load_supervisor(staging_root)
    stamp = utc_stamp().lower()
    campaign_id = f"c0154-supervisor-enable-{stamp}-{secrets.token_hex(4)}"
    attached_client = supervisor.SupervisorClient.attach_from_environment()
    client = attached_client or supervisor.SupervisorClient(
        control_root,
        campaign_id=campaign_id,
        phase="bootstrap",
        deadline_at_epoch=time.time() + float(arguments.max_wall_seconds),
        heartbeat_seconds=5,
        watchdog_timeout_seconds=20,
        ledger_interval_seconds=60,
        terminate_owner=False,
    )
    candidate: Path | None = None
    backup: Path | None = None
    assertion_preparer_candidate: Path | None = None
    assertion_preparer_backup: Path | None = None
    transaction_root: Path | None = None
    switched_documents: list[str] = []
    switched_archived_documents: list[str] = []
    installed_documents: list[str] = []
    tool_switched = False
    assertion_preparer_switched = False
    receipt_path = control_root / f"codex-0154-supervisor-enable-{stamp}.json"

    def switch_tool_tree() -> Mapping[str, Any]:
        """在事件结束写入失败时也保留已经发生的交换状态。"""

        nonlocal candidate, tool_switched
        if candidate is None or backup is None:
            raise DeploymentError("工具树候选或回滚坐标缺失。")
        result = _exchange_and_backup(candidate, production_root, backup)
        tool_switched = True
        candidate = None
        return result

    def switch_assertion_preparer() -> Mapping[str, Any]:
        """切换后立即置位，确保后续事件写入失败仍会共同回滚。"""

        nonlocal assertion_preparer_candidate, assertion_preparer_switched
        if assertion_preparer_candidate is None or assertion_preparer_backup is None:
            raise DeploymentError("assertion bundle 入口候选或回滚坐标缺失。")
        result = _switch_assertion_preparer(
            assertion_preparer_candidate,
            production_assertion_preparer,
            assertion_preparer_backup,
        )
        assertion_preparer_switched = True
        assertion_preparer_candidate = None
        return result

    # campaign-run 子进程复用父监督器；独立调用才创建并结束自己的监督器。
    with (nullcontext(client) if client.attached else client):
        try:
            record_step(
                client,
                "enable:preflight",
                lambda: _preflight(
                    client,
                    staging_root,
                    staging_tool,
                    production_root,
                    production_doc_root,
                    arguments.expected_tool_digest,
                    arguments.expected_supervisor_digest,
                    arguments.expected_assertion_preparer_digest,
                ),
            )
            candidate = control_root / (
                f"codex-0154-tool-candidate-{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:copy-candidate",
                lambda: _copy_candidate(staging_tool, candidate),
            )
            record_step(
                client,
                "enable:normalize-candidate",
                lambda: {"entry_count": normalize_tree_permissions(candidate)},
            )
            record_step(
                client,
                "enable:verify-candidate",
                lambda: _verify_candidate(
                    candidate,
                    arguments.expected_tool_digest,
                    arguments.expected_supervisor_digest,
                ),
            )
            assertion_preparer_candidate = control_root / (
                f"assertion-preparer-candidate-{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:prepare-assertion-preparer",
                lambda: _prepare_assertion_preparer_candidate(
                    staging_root,
                    assertion_preparer_candidate,
                    arguments.expected_assertion_preparer_digest,
                ),
            )
            transaction_root = production_doc_root / (
                f".codex-0154-deploy-{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:prepare-document-candidates",
                lambda: _prepare_document_candidates(
                    staging_root / "docs",
                    production_doc_root,
                    transaction_root,
                ),
            )
            backup = control_root / (
                # 生产目录当前可能已经是某个 0.151 工具树；备份名不能把未知
                # 的旧树伪装成 0.149.1，具体版本由既有启用收据和摘要确定。
                f"managed-tools-backup-before-{arguments.expected_tool_digest[:12]}-"
                f"{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:atomic-exchange",
                switch_tool_tree,
            )
            assertion_preparer_backup = control_root / (
                f"assertion-preparer-backup-before-"
                f"{arguments.expected_assertion_preparer_digest[:12]}-"
                f"{stamp}-{secrets.token_hex(4)}"
            )
            record_step(
                client,
                "enable:switch-assertion-preparer",
                switch_assertion_preparer,
            )
            record_step(
                client,
                "enable:switch-documents",
                lambda: _switch_documents(
                    transaction_root,
                    production_doc_root,
                    switched_documents,
                    switched_archived_documents,
                    installed_documents=installed_documents,
                ),
            )
            record_step(
                client,
                "enable:post-switch-verify",
                lambda: _post_switch_verify(
                    client,
                    staging_root,
                    production_root,
                    production_doc_root,
                    arguments.expected_tool_digest,
                    arguments.expected_supervisor_digest,
                    arguments.expected_assertion_preparer_digest,
                ),
            )
            receipt = {
                "schema_version": "codex-arm64-supervisor-enable/v1",
                "status": "passed",
                "campaign_id": campaign_id,
                "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "architecture": platform.machine(),
                "production_tool_root": str(production_root),
                "production_doc_root": str(production_doc_root),
                "tool_files_sha256": arguments.expected_tool_digest,
                "supervisor_sha256": arguments.expected_supervisor_digest,
                "assertion_preparer_sha256": (
                    arguments.expected_assertion_preparer_digest
                ),
                "rollback_backup": str(backup) if backup else None,
                "assertion_preparer_rollback_backup": (
                    str(assertion_preparer_backup)
                    if assertion_preparer_backup
                    else None
                ),
                "document_rollback_backup": (
                    str(transaction_root / "backups") if transaction_root else None
                ),
                "switched_archived_documents": list(switched_archived_documents),
                "installed_runtime_documents": list(installed_documents),
                "supervisor_run_dir": str(client.run_dir),
            }
            record_step(
                client,
                "enable:write-receipt",
                lambda: (
                    write_json_atomic(receipt_path, receipt)
                    or {"bytes": receipt_path.stat().st_size}
                ),
            )
        except BaseException:
            # 必须在监督器仍运行时记录回滚；不能等上下文退出后再补写。
            if (
                (
                    tool_switched
                    or assertion_preparer_switched
                    or switched_documents
                    or installed_documents
                )
                and backup is not None
                and backup.is_dir()
                and production_root.is_dir()
            ):
                try:
                    record_step(
                        client,
                        "enable:rollback",
                        lambda: _rollback_deployment(
                            backup,
                            production_root,
                            transaction_root,
                            production_doc_root,
                            switched_documents,
                            switched_archived_documents,
                            installed_documents=installed_documents,
                            auxiliary_backup=assertion_preparer_backup,
                            auxiliary_production=production_assertion_preparer,
                            auxiliary_switched=assertion_preparer_switched,
                        ),
                    )
                except BaseException:
                    pass
            raise
    print(
        json.dumps(
            {
                "status": "passed",
                "campaign_id": campaign_id,
                "receipt": str(receipt_path),
                "supervisor_run_dir": str(client.run_dir),
                "rollback_backup": str(backup) if backup else None,
                "assertion_preparer_rollback_backup": (
                    str(assertion_preparer_backup)
                    if assertion_preparer_backup
                    else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _preflight(
    client: Any,
    staging_root: Path,
    staging_tool: Path,
    production_root: Path,
    production_doc_root: Path,
    expected_tool_digest: str,
    expected_supervisor_digest: str,
    expected_assertion_preparer_digest: str,
) -> dict[str, Any]:
    staging_digest, staging_count = tool_digest(staging_tool)
    if staging_digest != expected_tool_digest:
        raise DeploymentError(f"暂存工具摘要不符：{staging_digest}")
    if file_sha256(staging_tool / "codex_upgrade_supervisor.py") != expected_supervisor_digest:
        raise DeploymentError("暂存监督器摘要不符。")
    reject_untrusted_tree(staging_tool)
    reject_untrusted_tree(production_root)
    staging_assertion_preparer = (
        staging_root / "tools" / MANAGED_ASSERTION_PREPARER
    )
    production_assertion_preparer = (
        production_root.parent / MANAGED_ASSERTION_PREPARER
    )
    reject_untrusted_file(
        staging_assertion_preparer,
        label="暂存 assertion bundle 入口",
    )
    reject_untrusted_file(
        production_assertion_preparer,
        label="生产 assertion bundle 入口",
    )
    if file_sha256(staging_assertion_preparer) != expected_assertion_preparer_digest:
        raise DeploymentError("暂存 assertion bundle 入口摘要不符。")
    if production_doc_root.is_symlink() or not production_doc_root.is_dir():
        raise DeploymentError("生产活动文档目录不存在或不可信。")
    if production_doc_root.stat().st_uid != 0 or production_doc_root.stat().st_mode & 0o022:
        raise DeploymentError("生产活动文档目录属主或写权限不安全。")
    legacy_document_facts = {
        name: legacy_production_document_facts(production_doc_root / name)
        for name in MANAGED_DOCUMENTS
    }
    legacy_runtime_document_facts: list[dict[str, Any]] = []
    for name in MANAGED_RUNTIME_DOCUMENTS:
        production_document = managed_document_path(
            production_doc_root,
            name,
            label="生产运行时依赖文档",
            allow_missing=True,
        )
        legacy_runtime_document_facts.append(
            (
                {
                    "path": name,
                    "status": "present",
                    **legacy_production_document_facts(production_document),
                }
                if production_document.exists()
                else {"path": name, "status": "absent"}
            )
        )
    capture_network = parse_container_network(
        run_checked(
            client,
            "enable:inspect-capture-network",
            ["docker", "inspect", "capture-cli", "--format", "{{json .NetworkSettings.Networks}}"],
        ),
        "capture-cli",
    )
    service_network = parse_container_network(
        run_checked(
            client,
            "enable:inspect-service-network",
            ["docker", "inspect", "sub2apiplus", "--format", "{{json .NetworkSettings.Networks}}"],
        ),
        "sub2apiplus",
    )
    rules = run_checked(client, "enable:inspect-policy", ["ip", "-4", "rule", "show"])
    routes = run_checked(client, "enable:inspect-route", ["ip", "-4", "route", "show", "table", "51830"])
    if "172.30.0.10" not in rules or "172.25.0.3" not in rules or "default dev wg1" not in routes:
        raise DeploymentError("固定 DMIT 出口路由策略不完整。")
    wireguard = verify_wg1_mtu()
    repository_docs = staging_root / "docs" / "repository-docs"
    if repository_docs.is_symlink() or not repository_docs.is_dir():
        raise DeploymentError("暂存文档归档目录不存在或不可信。")
    document_sha256: dict[str, str] = {}
    runtime_document_bindings: list[dict[str, str]] = []
    for name in MANAGED_DOCUMENTS:
        runtime_document = staging_root / "docs" / name
        archived_document = repository_docs / name
        if (
            runtime_document.is_symlink()
            or archived_document.is_symlink()
            or not runtime_document.is_file()
            or not archived_document.is_file()
            or file_sha256(runtime_document) != file_sha256(archived_document)
        ):
            raise DeploymentError(f"暂存文档运行副本与归档副本不一致：{name}")
        reject_untrusted_file(runtime_document, label="暂存活动文档")
        reject_untrusted_file(archived_document, label="暂存归档文档")
        document_sha256[name] = file_sha256(runtime_document)
    for name in MANAGED_RUNTIME_DOCUMENTS:
        runtime_document = managed_document_path(
            staging_root / "docs",
            name,
            label="暂存运行时依赖文档",
            allow_missing=False,
        )
        reject_untrusted_file(runtime_document, label="暂存运行时依赖文档")
        runtime_document_bindings.append(
            {"path": name, "sha256": file_sha256(runtime_document)}
        )
    source_spec = verify_scenario_source_spec(staging_root, staging_tool)
    return {
        "staging_file_count": staging_count,
        "staging_tool_sha256": staging_digest,
        "staging_assertion_preparer_sha256": expected_assertion_preparer_digest,
        "capture_ip": capture_network,
        "service_ip": service_network,
        "network_policy": "fixed-dmit",
        "wireguard": wireguard,
        "document_sha256": document_sha256,
        "runtime_document_bindings": runtime_document_bindings,
        "scenario_source_spec": source_spec,
        "legacy_production_documents": legacy_document_facts,
        "legacy_runtime_documents": legacy_runtime_document_facts,
    }


def _copy_candidate(source: Path, destination: Path) -> Mapping[str, Any]:
    if destination.exists() or destination.is_symlink():
        raise DeploymentError("候选目录已存在，拒绝覆盖。")
    shutil.copytree(source, destination, symlinks=True)
    return {"candidate": destination.name}


def _prepare_assertion_preparer_candidate(
    staging_root: Path,
    candidate: Path,
    expected_digest: str,
) -> Mapping[str, Any]:
    """在 control 文件系统准备主工具树外的 bundle 入口候选。"""

    source = staging_root / "tools" / MANAGED_ASSERTION_PREPARER
    reject_untrusted_file(source, label="暂存 assertion bundle 入口")
    if file_sha256(source) != expected_digest:
        raise DeploymentError("暂存 assertion bundle 入口摘要不符。")
    if candidate.exists() or candidate.is_symlink():
        raise DeploymentError("assertion bundle 入口候选已存在。")
    shutil.copyfile(source, candidate)
    os.chown(candidate, 0, 0)
    os.chmod(candidate, 0o755)
    with candidate.open("rb") as stream:
        os.fsync(stream.fileno())
    fsync_directory(candidate.parent)
    if file_sha256(candidate) != expected_digest:
        raise DeploymentError("assertion bundle 入口候选复制后摘要漂移。")
    return {
        "candidate": candidate.name,
        "sha256": expected_digest,
    }


def _switch_assertion_preparer(
    candidate: Path,
    production: Path,
    backup: Path,
) -> Mapping[str, Any]:
    """原子切换外层 bundle 入口，并保留同文件系统回滚副本。"""

    reject_untrusted_file(candidate, label="assertion bundle 入口候选")
    reject_untrusted_file(production, label="生产 assertion bundle 入口")
    if backup.exists() or backup.is_symlink():
        raise DeploymentError("assertion bundle 入口回滚副本已存在。")
    exchanged = False
    try:
        atomic_exchange(candidate, production)
        exchanged = True
        os.rename(candidate, backup)
        os.chown(backup, 0, 0)
        os.chmod(backup, stat.S_IMODE(backup.stat().st_mode) & ~0o022)
        fsync_directory(backup.parent)
    except BaseException:
        if exchanged and candidate.exists() and production.exists():
            try:
                atomic_exchange(candidate, production)
            except BaseException:
                pass
        raise
    return {"backup": backup.name}


def _verify_candidate(
    candidate: Path,
    expected_tool_digest: str,
    expected_supervisor_digest: str,
) -> Mapping[str, Any]:
    digest, count = tool_digest(candidate)
    if digest != expected_tool_digest:
        raise DeploymentError(f"候选工具摘要不符：{digest}")
    if file_sha256(candidate / "codex_upgrade_supervisor.py") != expected_supervisor_digest:
        raise DeploymentError("候选监督器摘要不符。")
    reject_untrusted_tree(candidate)
    return {"candidate_file_count": count, "candidate_tool_sha256": digest}


def _prepare_document_candidates(
    staging_doc_root: Path,
    production_doc_root: Path,
    transaction_root: Path,
) -> Mapping[str, Any]:
    """在活动文档同一文件系统内准备候选和回滚目录。"""

    if transaction_root.exists() or transaction_root.is_symlink():
        raise DeploymentError("文档事务目录已存在，拒绝覆盖。")
    candidate_root = transaction_root / "candidates"
    candidate_archive_root = candidate_root / "repository-docs"
    backup_root = transaction_root / "backups"
    candidate_root.mkdir(mode=0o700, parents=True)
    candidate_archive_root.mkdir(mode=0o700)
    backup_root.mkdir(mode=0o700)
    os.chown(transaction_root, 0, 0)
    os.chmod(transaction_root, 0o700)
    os.chown(candidate_root, 0, 0)
    os.chmod(candidate_root, 0o700)
    os.chown(candidate_archive_root, 0, 0)
    os.chmod(candidate_archive_root, 0o700)
    os.chown(backup_root, 0, 0)
    os.chmod(backup_root, 0o700)
    digests: dict[str, str] = {}
    runtime_document_bindings: list[dict[str, str]] = []
    for name in MANAGED_DOCUMENTS:
        source = staging_doc_root / name
        production = production_doc_root / name
        reject_untrusted_file(source, label="暂存活动文档")
        legacy_production_document_facts(production)
        destination = candidate_root / name
        shutil.copyfile(source, destination)
        archive_destination = candidate_archive_root / name
        shutil.copyfile(source, archive_destination)
        os.chown(destination, 0, 0)
        os.chmod(destination, 0o644)
        os.chown(archive_destination, 0, 0)
        os.chmod(archive_destination, 0o644)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        with archive_destination.open("rb") as stream:
            os.fsync(stream.fileno())
        if file_sha256(destination) != file_sha256(source):
            raise DeploymentError(f"文档候选复制后摘要漂移：{name}")
        if file_sha256(archive_destination) != file_sha256(source):
            raise DeploymentError(f"文档归档候选复制后摘要漂移：{name}")
        digests[name] = file_sha256(destination)
    for name in MANAGED_RUNTIME_DOCUMENTS:
        source = managed_document_path(
            staging_doc_root,
            name,
            label="暂存运行时依赖文档",
            allow_missing=False,
        )
        reject_untrusted_file(source, label="暂存运行时依赖文档")
        destination = candidate_root / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(destination.parent, 0, 0)
        os.chmod(destination.parent, 0o700)
        shutil.copyfile(source, destination)
        os.chown(destination, 0, 0)
        os.chmod(destination, 0o644)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        if file_sha256(destination) != file_sha256(source):
            raise DeploymentError(f"运行时依赖文档候选复制后摘要漂移：{name}")
        runtime_document_bindings.append(
            {"path": name, "sha256": file_sha256(destination)}
        )
    fsync_directory(candidate_archive_root)
    fsync_directory(candidate_root)
    fsync_directory(backup_root)
    fsync_directory(transaction_root)
    fsync_directory(production_doc_root)
    return {
        "transaction_root": transaction_root.name,
        "document_sha256": digests,
        "runtime_document_bindings": runtime_document_bindings,
    }


def _switch_documents(
    transaction_root: Path,
    production_doc_root: Path,
    switched_documents: list[str],
    switched_archived_documents: list[str] | None = None,
    *,
    installed_documents: list[str] | None = None,
) -> Mapping[str, Any]:
    """在同一事务中交换活动文档和归档副本，并保存回滚坐标。"""

    candidate_root = transaction_root / "candidates"
    candidate_archive_root = candidate_root / "repository-docs"
    backup_root = transaction_root / "backups"
    installed = installed_documents if installed_documents is not None else []
    for name in (*MANAGED_DOCUMENTS, *MANAGED_RUNTIME_DOCUMENTS):
        candidate = managed_document_path(
            candidate_root,
            name,
            label="文档候选",
            allow_missing=False,
        )
        production = managed_document_path(
            production_doc_root,
            name,
            label="生产文档",
            allow_missing=True,
        )
        backup = managed_document_path(
            backup_root,
            name,
            label="文档回滚副本",
            allow_missing=True,
        )
        if backup.exists() or backup.is_symlink():
            raise DeploymentError(f"文档回滚副本已存在：{name}")
        production.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(production.parent, 0, 0)
        os.chmod(production.parent, 0o700)
        os.chown(backup.parent, 0, 0)
        os.chmod(backup.parent, 0o700)
        if not production.exists():
            os.rename(candidate, production)
            installed.append(name)
            fsync_directory(candidate.parent)
            fsync_directory(production.parent)
            continue
        exchanged = False
        try:
            atomic_exchange(candidate, production)
            exchanged = True
            os.rename(candidate, backup)
            fsync_directory(candidate_root)
            fsync_directory(backup_root)
            switched_documents.append(name)
        except BaseException:
            if exchanged and candidate.exists() and production.exists():
                try:
                    atomic_exchange(candidate, production)
                except BaseException:
                    pass
            raise
    archived = (
        switched_archived_documents
        if switched_archived_documents is not None
        else []
    )
    production_archive_root = production_doc_root / "repository-docs"
    if production_archive_root.is_dir() and candidate_archive_root.is_dir():
        backup_archive_root = backup_root / "repository-docs"
        backup_archive_root.mkdir(mode=0o700, exist_ok=False)
        os.chown(backup_archive_root, 0, 0)
        os.chmod(backup_archive_root, 0o700)
        for name in MANAGED_DOCUMENTS:
            candidate = candidate_archive_root / name
            production = production_archive_root / name
            backup = backup_archive_root / name
            if backup.exists() or backup.is_symlink():
                raise DeploymentError(f"归档文档回滚副本已存在：{name}")
            exchanged = False
            try:
                atomic_exchange(candidate, production)
                exchanged = True
                os.rename(candidate, backup)
                os.chown(backup, 0, 0)
                os.chmod(backup, 0o644)
                fsync_directory(candidate_archive_root)
                fsync_directory(backup_archive_root)
                archived.append(name)
            except BaseException:
                if exchanged and candidate.exists() and production.exists():
                    try:
                        atomic_exchange(candidate, production)
                    except BaseException:
                        pass
                raise
    return {
        "switched_documents": list(switched_documents),
        "installed_documents": list(installed),
        "switched_archived_documents": list(archived),
        "backup_root": str(backup_root),
    }


def _exchange_and_backup(
    candidate: Path,
    production: Path,
    backup: Path,
) -> Mapping[str, Any]:
    if backup.exists() or backup.is_symlink():
        raise DeploymentError("回滚备份路径已存在，拒绝覆盖。")
    exchanged = False
    backup_ready = False
    try:
        atomic_exchange(candidate, production)
        exchanged = True
        os.rename(candidate, backup)
        backup_ready = True
        os.chown(backup, 0, 0)
        os.chmod(backup, stat.S_IMODE(backup.stat().st_mode) & ~0o022)
        fsync_directory(backup.parent)
    except BaseException:
        # 交换或备份阶段出错时，尽力恢复旧目录；失败树仍保留在可识别路径。
        try:
            if backup_ready and backup.is_dir() and production.is_dir():
                atomic_exchange(backup, production)
            elif exchanged and candidate.is_dir() and production.is_dir():
                atomic_exchange(candidate, production)
        except BaseException:
            pass
        raise
    return {"backup": backup.name}


def _post_switch_verify(
    client: Any,
    staging_root: Path,
    production: Path,
    production_doc_root: Path,
    expected_tool_digest: str,
    expected_supervisor_digest: str,
    expected_assertion_preparer_digest: str,
) -> Mapping[str, Any]:
    digest, count = tool_digest(production)
    if digest != expected_tool_digest:
        raise DeploymentError(f"生产工具摘要不符：{digest}")
    supervisor_path = production / "codex_upgrade_supervisor.py"
    if file_sha256(supervisor_path) != expected_supervisor_digest:
        raise DeploymentError("生产监督器摘要不符。")
    assertion_preparer = production.parent / MANAGED_ASSERTION_PREPARER
    reject_untrusted_file(assertion_preparer, label="生产 assertion bundle 入口")
    if file_sha256(assertion_preparer) != expected_assertion_preparer_digest:
        raise DeploymentError("生产 assertion bundle 入口摘要不符。")
    document_sha256: dict[str, str] = {}
    runtime_document_bindings: list[dict[str, str]] = []
    repository_docs = staging_root / "docs" / "repository-docs"
    production_repository_docs = production_doc_root / "repository-docs"
    if not production_repository_docs.is_dir():
        raise DeploymentError("生产文档归档目录不存在或不可信。")
    for name in MANAGED_DOCUMENTS:
        production_document = production_doc_root / name
        staging_document = staging_root / "docs" / name
        archived_document = repository_docs / name
        production_archived_document = production_repository_docs / name
        reject_untrusted_file(production_document, label="生产活动文档")
        reject_untrusted_file(production_archived_document, label="生产归档文档")
        production_sha256 = file_sha256(production_document)
        if (
            production_sha256 != file_sha256(staging_document)
            or production_sha256 != file_sha256(archived_document)
            or production_sha256 != file_sha256(production_archived_document)
        ):
            raise DeploymentError(f"生产活动文档与暂存/归档副本不一致：{name}")
        document_sha256[name] = production_sha256
    for name in MANAGED_RUNTIME_DOCUMENTS:
        production_document = managed_document_path(
            production_doc_root,
            name,
            label="生产运行时依赖文档",
            allow_missing=False,
        )
        staging_document = managed_document_path(
            staging_root / "docs",
            name,
            label="暂存运行时依赖文档",
            allow_missing=False,
        )
        reject_untrusted_file(production_document, label="生产运行时依赖文档")
        production_sha256 = file_sha256(production_document)
        if production_sha256 != file_sha256(staging_document):
            raise DeploymentError(f"生产运行时依赖文档与暂存副本不一致：{name}")
        runtime_document_bindings.append(
            {"path": name, "sha256": production_sha256}
        )
    source_spec = verify_scenario_source_spec(
        production_doc_root.parent,
        production,
    )
    wireguard = verify_wg1_mtu()
    run_checked(
        client,
        "enable:compile-production-supervisor",
        ["python3", "-m", "py_compile", str(supervisor_path)],
    )
    run_checked(
        client,
        "enable:check-assertion-preparer",
        ["bash", "-n", str(assertion_preparer)],
    )
    version = run_checked(
        client,
        "enable:supervisor-help",
        ["python3", str(supervisor_path), "--help"],
    )
    container_probe = (
        "import hashlib,json,pathlib,stat;"
        "p=pathlib.Path('/root/oauth-capture/tools/official_client_capture/capture.py');"
        "s=p.stat();"
        "print(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),"
        "'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)}))"
    )
    container_output = run_checked(
        client,
        "enable:verify-container-tool",
        ["docker", "exec", "capture-cli", "python3", "-c", container_probe],
    )
    try:
        container_payload = json.loads(container_output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as error:
        raise DeploymentError("容器内工具校验输出无法解析。") from error
    if not isinstance(container_payload, dict) or container_payload.get("uid") != 0 or container_payload.get("gid") != 0 or int(container_payload.get("mode", 0)) & 0o022:
        raise DeploymentError("容器内抓包执行源属主或权限不安全。")
    run_checked(
        client,
        "enable:verify-0154-binary",
        ["docker", "exec", "capture-cli", "/opt/codex-0.154.0/bin/codex", "--version"],
    )
    return {
        "production_file_count": count,
        "production_tool_sha256": digest,
        "production_assertion_preparer_sha256": expected_assertion_preparer_digest,
        "container_capture_sha256": container_payload.get("sha256"),
        "supervisor_help_bytes": len(version.encode("utf-8")),
        "wireguard": wireguard,
        "document_sha256": document_sha256,
        "runtime_document_bindings": runtime_document_bindings,
        "scenario_source_spec": source_spec,
    }


def _rollback_deployment(
    backup: Path,
    production: Path,
    transaction_root: Path | None,
    production_doc_root: Path,
    switched_documents: list[str],
    switched_archived_documents: list[str] | None = None,
    *,
    installed_documents: list[str] | None = None,
    auxiliary_backup: Path | None = None,
    auxiliary_production: Path | None = None,
    auxiliary_switched: bool = False,
) -> Mapping[str, Any]:
    """尽力恢复全部文档和工具；单项失败不得阻止其余回滚。"""

    failures: list[str] = []
    restored_documents: list[str] = []
    removed_installed_documents: list[str] = []
    restored_archived_documents: list[str] = []
    archived = switched_archived_documents or []
    if transaction_root is None and (switched_documents or installed_documents):
        failures.append("缺少文档事务目录")
    elif transaction_root is not None:
        backup_root = transaction_root / "backups"
        for name in reversed(switched_documents):
            try:
                atomic_exchange(backup_root / name, production_doc_root / name)
                restored_documents.append(name)
            except BaseException as error:
                failures.append(f"文档 {name}: {type(error).__name__}")
        candidate_root = transaction_root / "candidates"
        for name in reversed(installed_documents or []):
            try:
                production_document = production_doc_root / name
                candidate_document = candidate_root / name
                candidate_document.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.rename(production_document, candidate_document)
                fsync_directory(production_document.parent)
                fsync_directory(candidate_document.parent)
                removed_installed_documents.append(name)
            except BaseException as error:
                failures.append(f"新增文档 {name}: {type(error).__name__}")
        backup_archive_root = backup_root / "repository-docs"
        production_archive_root = production_doc_root / "repository-docs"
        for name in reversed(archived):
            try:
                atomic_exchange(
                    backup_archive_root / name,
                    production_archive_root / name,
                )
                restored_archived_documents.append(name)
            except BaseException as error:
                failures.append(f"归档文档 {name}: {type(error).__name__}")
    if auxiliary_switched:
        if auxiliary_backup is None or auxiliary_production is None:
            failures.append("缺少 assertion bundle 入口回滚坐标")
        else:
            try:
                atomic_exchange(auxiliary_backup, auxiliary_production)
            except BaseException as error:
                failures.append(f"assertion bundle 入口: {type(error).__name__}")
    try:
        atomic_exchange(backup, production)
    except BaseException as error:
        failures.append(f"工具树: {type(error).__name__}")
    if failures:
        raise DeploymentError("部署回滚不完整：" + "; ".join(failures))
    return {
        "failed_tree": backup.name,
        "restored_documents": restored_documents,
        "removed_installed_documents": removed_installed_documents,
        "restored_archived_documents": restored_archived_documents,
        "restored_assertion_preparer": auxiliary_switched,
        "rollback": "completed",
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DeploymentError, OSError, subprocess.SubprocessError) as error:
        print(f"ARM64 监督部署失败：{error}", file=sys.stderr)
        raise SystemExit(1)
