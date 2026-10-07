#!/usr/bin/env python3
"""ARM64 抓包驱动脚本的闭合清单、安装与复验（2026-09-22 驱动脚本入库）。

背景：v14r4 之前的 VC 驱动脚本散落在采集主机 /root/ 下、按轮次复制改名（arm64-v14r4-*.sh），
没有清单、没有摘要、没有与受管工具部署的绑定；批次 15 的 EvidenceManifest 漂移事故正是重跑
脚本时的权限收口造成的。老板要求：驱动脚本入库（非受管目录，不改工具身份），但必须有
闭合文件清单、逐文件 SHA、安装目标和权限，由组合部署收据绑定，guard 执行前复验。

三个子命令（全部只读校验失败即非零退出，不发请求、不读证据）：

* ``build-manifest --root <目录>``：开发侧生成／刷新 ``manifest.json``（闭合清单：``driver/`` 下全部文件
  + ``install.py`` + ``README.md``，逐文件 sha256 与安装模式；清单自摘要）。``--check`` 只核对不写。
* ``install --source <仓库内目录> --target <安装目标> --data-root <数据根>``：在采集主机以 root 执行。
  先核对源目录与其清单逐字一致，再复制到 ``<target>.staging-<stamp>`` 并原子替换安装目标，
  最后写安装收据 ``<data-root>/control/arm64-capture-driver-install-<stamp>.json``
  （``arm64-capture-driver-install/v1``），绑定：清单摘要、逐文件 sha256／mode、安装目标，以及
  ``control/`` 下**当前最新**的受管工具部署收据（``codex-*-supervisor-enable-*.json`` 的路径、
  sha256、``tool_files_sha256``）——这就是老板所说的组合部署收据。
* ``verify --target <安装目标> --data-root <数据根>``：guard 执行前复验。安装目标内 manifest 自摘要
  合法且自身 0600／root；顶层恰好只有 install.py／README.md／manifest.json／driver/；逐文件 sha256／mode／
  属主 root 与清单一致、无多余文件；存在一份安装收据其 manifest_sha256
  等于当前清单且其绑定的部署收据仍是 ``control/`` 下最新的一份（受管工具重新部署后必须重新
  ``install`` 确认，否则 guard 失败）。

Claude 升级 B5a-01 起两处补充：``control/`` 下有受监督部署的事务日志（``managed-deploy-*.journal``，部署进行中或
中断后尚未恢复）时 ``install`` 与 ``verify`` 一律拒绝——生产树可能处在半交换状态，只认已写出的收据；``install``／
``verify`` 加 ``--client``（缺省 codex），按客户端选部署收据的文件名模式与合同，Codex 的行为逐字不变。

B5a-06 起安装收据绑定**全部已纳管客户端**（``control/`` 里已有其部署收据的客户端，与受管部署器同一判据）各自最新的
部署收据：只有一个客户端纳管时照旧写 v1（单个 ``deployment_receipt``，只有 Codex 纳管时与改造前逐字相同）；两客户端都已
纳管时写 ``arm64-capture-driver-install/v2``（``deployment_receipts``：客户端 → 绑定）。这样交换共享根的一次部署（两份
收据）之后只需重装一次驱动，两客户端的守卫都能复验。``install --client <客户端>`` 显式给出时只绑定该客户端（v1，B5a-01
的用法）。``verify`` 按 ``--client``（缺省 codex）取该客户端当前最新的部署收据，在 v1／v2 安装收据里找绑定了它的那份；
v1 收据绑定的是哪个客户端按收据文件名模式判定。本文件是驱动完整性的复验者，不在复验前加载被复验的驱动文件
（``driver/client_table.py``），客户端规则在这里另存一份，测试核对与客户端表一致。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

MANIFEST_SCHEMA = "arm64-capture-driver-manifest/v1"
INSTALL_RECEIPT_SCHEMA = "arm64-capture-driver-install/v1"
# B5a-06：两客户端都已纳管时的安装收据（绑定每个已纳管客户端最新的部署收据）。
INSTALL_RECEIPT_SCHEMA_V2 = "arm64-capture-driver-install/v2"
MANIFEST_NAME = "manifest.json"
DEPLOY_RECEIPT_GLOB = "codex-*-supervisor-enable-*.json"
# B5a-01：按客户端选部署收据（与受管部署器 DEPLOY_RECEIPT_GLOBS、各客户端适配器 deploy_receipt_glob 同值）。文件名里
# 时间戳之前的分隔串用于按部署顺序排序；Claude 收据的工具身份在 identity 对象里，Codex 收据在顶层。
DEPLOY_RECEIPT_GLOBS = {"codex": DEPLOY_RECEIPT_GLOB, "claude": "claude-managed-deploy-*.json"}
DEPLOY_RECEIPT_STAMP_SEPARATORS = {"codex": "-supervisor-enable-", "claude": "claude-managed-deploy-"}
DEPLOY_RECEIPT_IDENTITY_KEYS = {"codex": None, "claude": "identity"}
DEPLOY_CLIENTS = tuple(DEPLOY_RECEIPT_GLOBS)
# 受监督部署的写前意图日志：存在即说明有部署在进行或中断后尚未恢复（arm64_supervised_deploy.SWITCH_JOURNAL_GLOB）。
SWITCH_JOURNAL_GLOB = "managed-deploy-*.journal"
INSTALL_RECEIPT_PREFIX = "arm64-capture-driver-install-"
# 安装模式：可执行脚本 0700，其余 0600；目录 0700。
EXECUTABLE_SUFFIXES = {".sh", ".py"}
INSTALL_ROOT_MODE = 0o700
# 顶层闭合：根目录只允许恰好这些条目——两份登记文件、清单自身与 driver/ 目录；其余任何文件或目录
#（包括 __pycache__、编辑器残留、unexpected.py）一律拒绝（2026-09-22 审核 P2）。
TOP_LEVEL_FILES = ("install.py", "README.md")
SCRIPTS_DIR = "driver"
MANIFEST_MODE = 0o600


class DriverError(RuntimeError):
    """清单、安装或复验失败。"""


def _running_as_root() -> bool:
    """属主处理（chown、属主核对）以 root 为准；离线测试可替换。"""

    return os.geteuid() == 0


def _require_root() -> None:
    """install 入口的 root 门禁；离线测试可替换。"""

    if os.geteuid() != 0:
        raise DriverError("驱动安装必须由 root 执行。")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    """时间戳 + 随机后缀：同一秒内重复安装也不会撞收据／暂存目录名。"""

    return datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz") + "-" + secrets.token_hex(3)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _install_mode(relative: str) -> str:
    return "0700" if Path(relative).suffix in EXECUTABLE_SUFFIXES else "0600"


def _enumerate(root: Path) -> list[str]:
    """闭合枚举：driver/ 下全部普通文件 + 顶层 install.py／README.md，按路径排序。

    根目录的条目集合必须恰好是 ``{install.py, README.md, manifest.json, driver/}``（manifest.json 在
    build-manifest 之前允许尚不存在）；任何额外顶层文件或目录都让枚举失败，避免"清单只看自己登记的
    路径、旁边多出一个 unexpected.py 也验证通过"。
    """

    root = Path(root)
    rows: list[str] = []
    allowed_top = {*TOP_LEVEL_FILES, MANIFEST_NAME, SCRIPTS_DIR}
    unexpected = sorted(entry.name for entry in root.iterdir() if entry.name not in allowed_top)
    if unexpected:
        raise DriverError(f"驱动根目录含清单之外的顶层条目：{unexpected}")
    manifest_path = root / MANIFEST_NAME
    if manifest_path.exists() and (manifest_path.is_symlink() or not manifest_path.is_file()):
        raise DriverError(f"驱动清单不是普通文件：{manifest_path}")
    scripts = root / SCRIPTS_DIR
    if not scripts.is_dir() or scripts.is_symlink():
        raise DriverError(f"缺少 {SCRIPTS_DIR}/ 目录：{root}")
    for path in sorted(scripts.rglob("*")):
        if path.is_symlink():
            raise DriverError(f"驱动目录不得含符号链接：{path}")
        if path.is_dir():
            if path.name == "__pycache__":
                raise DriverError(f"驱动目录不得含 __pycache__：{path}")
            continue
        if not path.is_file():
            raise DriverError(f"驱动目录含非普通文件：{path}")
        rows.append(path.relative_to(root).as_posix())
    for name in TOP_LEVEL_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise DriverError(f"缺少顶层文件：{path}")
        rows.append(name)
    return sorted(rows)


def build_manifest(root: Path) -> dict[str, Any]:
    files = [
        {"path": relative, "sha256": _sha256_file(root / relative), "mode": _install_mode(relative), "bytes": (root / relative).stat().st_size}
        for relative in _enumerate(root)
    ]
    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "file_count": len(files),
        "files": files,
    }
    payload["manifest_sha256"] = _sha256_bytes(_canonical(payload))
    return payload


def validate_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "file_count", "files", "manifest_sha256"}:
        raise DriverError("驱动清单字段不闭合。")
    if payload["schema_version"] != MANIFEST_SCHEMA:
        raise DriverError("驱动清单 schema 不受支持。")
    files = payload["files"]
    if not isinstance(files, list) or not files or payload["file_count"] != len(files):
        raise DriverError("驱动清单 files／file_count 非法。")
    seen: set[str] = set()
    for row in files:
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256", "mode", "bytes"}:
            raise DriverError("驱动清单条目字段不闭合。")
        relative = row["path"]
        if not isinstance(relative, str) or relative in seen or relative.startswith("/") or ".." in relative.split("/"):
            raise DriverError(f"驱动清单条目路径非法：{relative!r}")
        if not (relative == "install.py" or relative == "README.md" or relative.startswith(SCRIPTS_DIR + "/")):
            raise DriverError(f"驱动清单条目不在闭合范围：{relative}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"])):
            raise DriverError(f"驱动清单条目 sha256 非法：{relative}")
        if row["mode"] != _install_mode(relative):
            raise DriverError(f"驱动清单条目 mode 与安装规则不符：{relative}")
        if not isinstance(row["bytes"], int) or isinstance(row["bytes"], bool) or row["bytes"] < 0:
            raise DriverError(f"驱动清单条目 bytes 非法：{relative}")
        seen.add(relative)
    if [row["path"] for row in files] != sorted(seen):
        raise DriverError("驱动清单条目未按路径排序。")
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    if payload["manifest_sha256"] != _sha256_bytes(_canonical(unsigned)):
        raise DriverError("驱动清单自摘要不一致。")
    return dict(payload)


def load_manifest(root: Path) -> dict[str, Any]:
    path = Path(root) / MANIFEST_NAME
    if path.is_symlink() or not path.is_file():
        raise DriverError(f"驱动清单不存在或不可信：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DriverError(f"驱动清单不是合法 JSON：{error}") from error
    return validate_manifest(payload)


def verify_tree(root: Path, manifest: Mapping[str, Any], *, check_mode: bool, require_root_owner: bool) -> list[dict[str, Any]]:
    """逐文件核对：路径闭合（无多余、无缺失）、sha256、字节数，安装态还核对 mode 与属主。"""

    root = Path(root)
    expected = {row["path"]: row for row in manifest["files"]}
    actual = _enumerate(root)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        raise DriverError(f"驱动目录与清单不闭合：缺失={missing} 多余={extra}")
    rows: list[dict[str, Any]] = []
    for relative, row in expected.items():
        path = root / relative
        digest = _sha256_file(path)
        st = path.stat()
        if digest != row["sha256"] or st.st_size != row["bytes"]:
            raise DriverError(f"驱动文件摘要或字节数漂移：{relative}")
        mode = stat.S_IMODE(st.st_mode)
        if check_mode and mode != int(row["mode"], 8):
            raise DriverError(f"驱动文件安装模式不符：{relative} 实际 {mode:04o} 期望 {row['mode']}")
        if require_root_owner and (st.st_uid != 0 or st.st_gid != 0):
            raise DriverError(f"驱动文件属主不是 root:root：{relative}")
        rows.append({"path": relative, "sha256": digest, "mode": f"{mode:04o}"})
    if check_mode:
        for directory in _directories(root):
            dst = directory.stat()
            if stat.S_IMODE(dst.st_mode) != INSTALL_ROOT_MODE:
                raise DriverError(f"驱动目录模式不是 0700：{directory}")
            if require_root_owner and (dst.st_uid != 0 or dst.st_gid != 0):
                raise DriverError(f"驱动目录属主不是 root:root：{directory}")
        # 清单自身不在 files 里，但安装态同样要求 0600／root（2026-09-22 审核 P2）。
        manifest_path = root / MANIFEST_NAME
        mst = manifest_path.stat()
        if stat.S_IMODE(mst.st_mode) != MANIFEST_MODE:
            raise DriverError(f"驱动清单模式不是 0600：{manifest_path} 实际 {stat.S_IMODE(mst.st_mode):04o}")
        if require_root_owner and (mst.st_uid != 0 or mst.st_gid != 0):
            raise DriverError(f"驱动清单属主不是 root:root：{manifest_path}")
    return rows


def require_no_switch_journal(control_root: Path) -> None:
    """受监督部署的事务日志还在即拒绝（B5a-01 消费门禁）：生产树可能处在半交换状态，此时只认已写出的收据。"""

    journals = sorted(path.name for path in Path(control_root).glob(SWITCH_JOURNAL_GLOB))
    if journals:
        raise DriverError(
            f"control 目录有未完成的受监督部署事务日志 {journals}：部署进行中或中断后尚未恢复，"
            "重新执行受监督部署完成恢复后再派发。"
        )


def deploy_receipt_identity(payload: Mapping[str, Any], client: str = "codex") -> Mapping[str, Any]:
    """部署收据里的工具身份所在对象：Codex 收据是顶层，Claude 收据是 ``identity``。"""

    key = DEPLOY_RECEIPT_IDENTITY_KEYS[client]
    identity = payload if key is None else payload.get(key)
    return identity if isinstance(identity, Mapping) else {}


def latest_deploy_receipt(control_root: Path, client: str = "codex") -> tuple[Path, dict[str, Any]]:
    """control/ 下该客户端当前最新的受管工具部署收据（按文件名中的时间戳，即部署顺序）。"""

    if client not in DEPLOY_RECEIPT_GLOBS:
        raise DriverError(f"不认识的客户端：{client!r}（可选：{list(DEPLOY_CLIENTS)}）")
    control_root = Path(control_root)
    separator = DEPLOY_RECEIPT_STAMP_SEPARATORS[client]
    candidates = sorted((p for p in control_root.glob(DEPLOY_RECEIPT_GLOBS[client]) if p.is_file() and not p.is_symlink()),
                        key=lambda path: path.name.split(separator, 1)[1].lower())
    if not candidates:
        raise DriverError(f"control 目录没有受管工具部署收据：{control_root}")
    path = candidates[-1]
    if len(candidates) > 1 and path.name.split(separator, 1)[1].lower() == candidates[-2].name.split(separator, 1)[1].lower():
        raise DriverError("同一时刻存在两份部署收据，无法唯一确定最新部署")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DriverError(f"部署收据不是合法 JSON：{path}：{error}") from error
    if not isinstance(payload, Mapping) or not isinstance(deploy_receipt_identity(payload, client).get("tool_files_sha256"), str):
        raise DriverError(f"部署收据缺少 tool_files_sha256：{path}")
    return path, dict(payload)


def _deploy_binding(control_root: Path, client: str = "codex") -> dict[str, Any]:
    path, payload = latest_deploy_receipt(control_root, client)
    return {
        "path": path.name,
        "sha256": _sha256_file(path),
        "status": payload.get("status"),
        "tool_files_sha256": deploy_receipt_identity(payload, client)["tool_files_sha256"],
        "supervisor_sha256": payload.get("supervisor_sha256"),
    }


def managed_clients(control_root: Path) -> list[str]:
    """已纳管的客户端：control 目录里已有它的已写部署收据（与受管部署器 managed_clients 同一判据），按客户端表顺序。"""

    return [client for client in DEPLOY_CLIENTS
            if any(path.is_file() and not path.is_symlink() for path in Path(control_root).glob(DEPLOY_RECEIPT_GLOBS[client]))]


def _binding_client(binding: Mapping[str, Any]) -> str | None:
    """v1 安装收据绑定的部署收据属于哪个客户端（按文件名模式判定；认不出为 None）。"""

    name = str(binding.get("path") or "")
    matches = [client for client in DEPLOY_CLIENTS if fnmatch.fnmatchcase(name, DEPLOY_RECEIPT_GLOBS[client])]
    return matches[0] if len(matches) == 1 else None


def receipt_bindings(receipt: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """安装收据绑定的部署收据：客户端 → 绑定。v1 只有一份（按文件名判客户端），v2 是 deployment_receipts。"""

    if receipt.get("schema_version") == INSTALL_RECEIPT_SCHEMA_V2:
        return dict(receipt["deployment_receipts"])
    client = _binding_client(receipt["deployment_receipt"])
    return {client: receipt["deployment_receipt"]} if client is not None else {}


def _install_receipts(control_root: Path) -> list[Path]:
    return sorted(p for p in Path(control_root).glob(f"{INSTALL_RECEIPT_PREFIX}*.json") if p.is_file() and not p.is_symlink())


def validate_install_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    common = {"schema_version", "installed_at_utc", "install_target", "manifest_sha256", "file_count", "files", "receipt_sha256"}
    binding_keys = {"path", "sha256", "status", "tool_files_sha256", "supervisor_sha256"}
    if not isinstance(payload, Mapping):
        raise DriverError("安装收据字段不闭合。")
    if payload.get("schema_version") == INSTALL_RECEIPT_SCHEMA_V2:
        # v2（B5a-06）：每个已纳管客户端一份绑定，至少两个客户端（只有一个客户端纳管时写 v1）。
        if set(payload) != common | {"deployment_receipts"}:
            raise DriverError("安装收据字段不闭合。")
        bindings = payload["deployment_receipts"]
        if (not isinstance(bindings, Mapping) or len(bindings) < 2 or not set(bindings) <= set(DEPLOY_CLIENTS)
                or not all(isinstance(item, Mapping) and set(item) == binding_keys for item in bindings.values())):
            raise DriverError("安装收据的部署收据绑定字段不闭合。")
    else:
        expected_keys = common | {"deployment_receipt"}
        if set(payload) != expected_keys:
            raise DriverError("安装收据字段不闭合。")
        if payload["schema_version"] != INSTALL_RECEIPT_SCHEMA:
            raise DriverError("安装收据 schema 不受支持。")
        binding = payload["deployment_receipt"]
        if not isinstance(binding, Mapping) or set(binding) != binding_keys:
            raise DriverError("安装收据的部署收据绑定字段不闭合。")
    unsigned = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    if payload["receipt_sha256"] != _sha256_bytes(_canonical(unsigned)):
        raise DriverError("安装收据自摘要不一致。")
    return dict(payload)


def _directories(root: Path) -> list[Path]:
    """安装目标内全部目录：根、driver/ 及其子目录（如 driver/local/）。"""

    root = Path(root)
    return [root, root / SCRIPTS_DIR, *sorted(p for p in (root / SCRIPTS_DIR).rglob("*") if p.is_dir() and not p.is_symlink())]


def _apply_install_modes(target: Path, manifest: Mapping[str, Any]) -> None:
    for directory in _directories(target):
        os.chmod(directory, INSTALL_ROOT_MODE)
    for row in manifest["files"]:
        os.chmod(target / row["path"], int(row["mode"], 8))
    os.chmod(target / MANIFEST_NAME, MANIFEST_MODE)


def cmd_build_manifest(arguments: argparse.Namespace) -> int:
    root = Path(arguments.root).resolve(strict=True)
    manifest = build_manifest(root)
    path = root / MANIFEST_NAME
    if arguments.check:
        current = load_manifest(root)
        if current != manifest:
            raise DriverError("manifest.json 与目录内容不一致，请重新 build-manifest。")
        print(json.dumps({"status": "unchanged", "file_count": manifest["file_count"], "manifest_sha256": manifest["manifest_sha256"]}, ensure_ascii=False))
        return 0
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "written", "path": str(path), "file_count": manifest["file_count"], "manifest_sha256": manifest["manifest_sha256"]}, ensure_ascii=False))
    return 0


def cmd_install(arguments: argparse.Namespace) -> int:
    source = Path(arguments.source).resolve(strict=True)
    target = Path(arguments.target)
    data_root = Path(arguments.data_root).resolve(strict=True)
    control_root = data_root / "control"
    _require_root()
    require_no_switch_journal(control_root)
    if target.exists() and target.is_symlink():
        raise DriverError(f"安装目标不得是符号链接：{target}")
    manifest = load_manifest(source)
    verify_tree(source, manifest, check_mode=False, require_root_owner=False)
    # 绑定哪些客户端（B5a-06）：显式 --client 只绑定它；否则绑定全部已纳管客户端（只有 Codex 纳管时与改造前相同；
    # 一个都没有时照旧按 Codex 报「没有部署收据」）。
    requested = getattr(arguments, "client", None)
    clients = [requested] if requested else (managed_clients(control_root) or ["codex"])
    deploys = {client: _deploy_binding(control_root, client) for client in clients}
    stamp = _stamp()
    staging = target.parent / f"{target.name}.staging-{stamp}"
    backup = target.parent / f"{target.name}.previous-{stamp}"
    if staging.exists() or backup.exists():
        raise DriverError("安装暂存或备份目录已存在。")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, staging, symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))
    if _running_as_root():
        for path in [staging, *staging.rglob("*")]:
            os.chown(path, 0, 0)
    _apply_install_modes(staging, manifest)
    verify_tree(staging, manifest, check_mode=True, require_root_owner=_running_as_root())
    if target.exists():
        os.rename(target, backup)
    os.rename(staging, target)
    files = verify_tree(target, manifest, check_mode=True, require_root_owner=_running_as_root())
    receipt: dict[str, Any] = {
        "schema_version": INSTALL_RECEIPT_SCHEMA,
        "installed_at_utc": _utc_now(),
        "install_target": str(target),
        "manifest_sha256": manifest["manifest_sha256"],
        "file_count": manifest["file_count"],
        "files": files,
    }
    if len(deploys) == 1:
        receipt["deployment_receipt"] = next(iter(deploys.values()))
    else:
        receipt["schema_version"] = INSTALL_RECEIPT_SCHEMA_V2
        receipt["deployment_receipts"] = deploys
    receipt["receipt_sha256"] = _sha256_bytes(_canonical(receipt))
    receipt_path = control_root / f"{INSTALL_RECEIPT_PREFIX}{stamp}.json"
    if receipt_path.exists():
        raise DriverError(f"安装收据已存在：{receipt_path}")
    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    bound = next(iter(deploys.values()))["path"] if len(deploys) == 1 else {client: item["path"] for client, item in deploys.items()}
    print(json.dumps({"status": "installed", "install_target": str(target), "previous": str(backup) if backup.exists() else None, "receipt": str(receipt_path), "manifest_sha256": manifest["manifest_sha256"], "deployment_receipt": bound}, ensure_ascii=False))
    return 0


def verify_install(target: Path, data_root: Path, client: str = "codex") -> dict[str, Any]:
    target = Path(target)
    data_root = Path(data_root)
    control_root = data_root / "control"
    require_no_switch_journal(control_root)
    if target.is_symlink() or not target.is_dir():
        raise DriverError(f"安装目标不存在或不可信：{target}")
    manifest = load_manifest(target)
    verify_tree(target, manifest, check_mode=True, require_root_owner=_running_as_root())
    deploy = _deploy_binding(control_root, client)
    matched: dict[str, Any] | None = None
    stale: list[str] = []
    for path in reversed(_install_receipts(control_root)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            receipt = validate_install_receipt(payload)
        except (OSError, ValueError) as error:
            raise DriverError(f"安装收据不可读：{path}：{error}") from error
        if receipt["manifest_sha256"] != manifest["manifest_sha256"] or Path(receipt["install_target"]) != target:
            continue
        # 按客户端取绑定（B5a-06）：v1 收据绑定的是另一个客户端时，这份收据不证明本客户端的部署，照旧记作过期。
        bindings = receipt_bindings(receipt)
        bound = bindings.get(client) or (receipt.get("deployment_receipt") or {})
        if (client in bindings and bound["path"] == deploy["path"] and bound["sha256"] == deploy["sha256"]
                and bound["tool_files_sha256"] == deploy["tool_files_sha256"]):
            matched = {**receipt, "receipt_path": path.name}
            break
        stale.append(str(bound.get("path") or sorted(item["path"] for item in bindings.values())))
    if matched is None:
        if stale:
            raise DriverError(
                f"安装收据绑定的部署收据 {sorted(set(stale))} 不是当前最新 {deploy['path']}（受管工具已重新部署，驱动必须重新 install 确认）。"
            )
        raise DriverError("没有与当前驱动清单和安装目标匹配的安装收据，请先执行 install。")
    if [(row["path"], row["sha256"]) for row in matched["files"]] != [(row["path"], row["sha256"]) for row in manifest["files"]]:
        raise DriverError("安装收据的逐文件摘要与当前清单不一致。")
    return {
        "status": "verified",
        "install_target": str(target),
        "manifest_sha256": manifest["manifest_sha256"],
        "file_count": manifest["file_count"],
        "install_receipt": matched["receipt_path"],
        "deployment_receipt": deploy["path"],
        "tool_files_sha256": deploy["tool_files_sha256"],
    }


def cmd_verify(arguments: argparse.Namespace) -> int:
    result = verify_install(Path(arguments.target), Path(arguments.data_root), arguments.client)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-manifest", help="生成或核对 manifest.json")
    build.add_argument("--root", required=True)
    build.add_argument("--check", action="store_true")
    build.set_defaults(func=cmd_build_manifest)
    install = sub.add_parser("install", help="安装到采集主机并写安装收据")
    install.add_argument("--source", required=True)
    install.add_argument("--target", required=True)
    install.add_argument("--data-root", required=True)
    install.add_argument("--client", choices=DEPLOY_CLIENTS, default=None,
                         help="只绑定这个客户端的部署收据（缺省绑定全部已纳管客户端；只有 Codex 纳管时与改造前相同）")
    install.set_defaults(func=cmd_install)
    verify = sub.add_parser("verify", help="复验安装目标与安装收据")
    verify.add_argument("--target", required=True)
    verify.add_argument("--data-root", required=True)
    verify.add_argument("--client", choices=DEPLOY_CLIENTS, default="codex", help="按哪个客户端的部署收据复验（缺省 codex）")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        return int(arguments.func(arguments))
    except DriverError as error:
        print(f"驱动清单／安装失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
