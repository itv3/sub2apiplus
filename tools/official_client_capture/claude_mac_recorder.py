"""真实产品 recorder 的受管调用顺序和制品测量，不内置未经验证的启动参数。

真实 Mac 观测后端只由静态来源适配器提供。当前没有正式登记，故本模块具备合同
接线和本机模拟能力，但不宣称已交付真实产品取证。后端须先记录状态恢复 journal
和进程监测，再启动产品；失败时仍执行回收、恢复和原件封存。
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Any, Mapping, Protocol

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source


class ProductRecorder(Protocol):
    """每个方法取得真实观测；模拟实现只能在测试作用域登记。"""

    def prepare_state(self, task: Mapping[str, Any], bundle_root: Path) -> None: ...
    def start_monitoring(self, task: Mapping[str, Any], bundle_root: Path) -> None: ...
    def recheck_dispatch(self, task: Mapping[str, Any]) -> None: ...
    def launch_desktop(self, task: Mapping[str, Any], recipe: Mapping[str, Any]) -> None: ...
    def launch_vscode(self, task: Mapping[str, Any], recipe: Mapping[str, Any]) -> None: ...
    def drive_scenario(self, task: Mapping[str, Any], scenario: Mapping[str, Any]) -> None: ...
    def stop_product(self, task: Mapping[str, Any]) -> None: ...
    def restore_state(self, task: Mapping[str, Any], bundle_root: Path) -> None: ...
    def seal_sources(self, task: Mapping[str, Any], bundle_root: Path, *, status: str) -> None: ...


def measure_artifact(path: Path) -> dict[str, Any]:
    """文件与目录树采用不同绑定；目录内链接只能指向同一制品内部。"""
    path = Path(path)
    c.require_plain_path(path)
    if path.is_file():
        raw = path.read_bytes()
        return {"kind": "file", "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    c.require(path.is_dir(), "制品不是目录或普通文件")
    entries, size = [], 0
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix()
        mode = child.lstat().st_mode
        if stat.S_ISLNK(mode):
            c.require(child.resolve(strict=True).is_relative_to(path), "制品链接越出冻结目录树")
            entries.append({"path": relative, "kind": "symlink", "target": child.readlink().as_posix()})
        elif stat.S_ISREG(mode):
            raw = child.read_bytes()
            entries.append({"path": relative, "kind": "file", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
            size += len(raw)
        else:
            c.require(stat.S_ISDIR(mode), "制品目录包含特殊文件")
            entries.append({"path": relative, "kind": "directory"})
    return {"kind": "directory", "path": str(path), "sha256": c.digest(entries), "bytes": size}


def isolation_recipe(task: Mapping[str, Any], bundle_root: Path) -> dict[str, Any]:
    """驱动只转调共享来源语义；不在 wire producer 再写一份材料解释。"""
    return source.verify_isolation_recipe_v2(task, bundle_root)


def record_product(task: Mapping[str, Any], bundle_root: Path, *, recorder: ProductRecorder) -> dict[str, Any]:
    """先 journal/monitor，后产品；无论成功或失败都回收、恢复并封存已取得原件。"""
    task = source.validate_task_v2(task)
    root = Path(bundle_root)
    adapter = c.SOURCE_ADAPTERS.get(task["execution"]["recorder_id"])
    c.require(adapter is not None and callable(getattr(adapter, "owns_recorder", None)) and adapter.owns_recorder(recorder), "记录器不属于受管静态来源")
    source.verify_reservation_v2(task, Path(task["execution"]["capture_root"]))
    recipe = isolation_recipe(task, root)
    status, error = "failed", None
    try:
        recorder.prepare_state(task, root)
        # 两份准备原件必须在实际启动前存在；后端不能在结束时伪造准备顺序。
        c.secure_file(root, "state-before.bin")
        c.secure_file(root, "recorder-journal.jsonl")
        recorder.start_monitoring(task, root)
        c.secure_file(root, "monitor-ready.json")
        # 后端转调既有候选 lease/预算/身份复核；耗时的准备完成后再检查，不另算预算。
        c.require(callable(getattr(recorder, "recheck_dispatch", None)), "缺既有候选派发前复核 service")
        recorder.recheck_dispatch(task)
        if task["client_id"] == "claude-desktop":
            from tools.official_client_capture import claude_drive_desktop
            claude_drive_desktop.drive_product(task, recipe, recorder=recorder)
        else:
            from tools.official_client_capture import claude_drive_vscode
            claude_drive_vscode.drive_product(task, recipe, recorder=recorder)
        status = "complete"
    except BaseException as caught:
        error = caught
    finally:
        try:
            recorder.stop_product(task)
        except BaseException as caught:
            error = error or caught
            status = "failed"
        try:
            recorder.restore_state(task, root)
        except BaseException as caught:
            error = error or caught
            status = "truncated"
        # 失败也封存；缺材料不补造成功／零值，独立预约索引仍由发现入口标为未决。
        recorder.seal_sources(task, root, status=status)
    if error is not None:
        raise error
    return {"status": status, "source_root": str(root), "producer_run_id": task["execution"]["producer_run_id"]}
