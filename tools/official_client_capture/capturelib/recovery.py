"""抓包资源恢复账本；不记录命令、环境或凭据。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .model import CaptureCase, utc_now
from .security import secure_write_json


class RecoveryJournal:
    """原子记录当前 run 拥有的抓包 PID，供中断和下次启动审计。"""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "recovery.json"
        self.data: dict[str, Any] = {
            "schema_version": "official-client-capture-recovery/v1",
            "owner_pid": os.getpid(),
            "status": "running",
            "active_resource": None,
            "last_signal": None,
            "cleanup_successful": True,
            "updated_at": utc_now(),
        }
        self.write()

    def write(self) -> None:
        self.data["updated_at"] = utc_now()
        secure_write_json(self.path, self.data)

    def activate(
        self,
        *,
        case: CaptureCase,
        scenario: str,
        role: str,
        pid: int,
        pgid: int,
        output_dir: Path,
        port: int | None,
    ) -> None:
        """登记当前唯一资源；路径只保存 run 内相对值。"""

        self.data["active_resource"] = {
            "task": case.task,
            "subject": case.subject,
            "evidence": case.evidence,
            "scenario": scenario,
            "role": role,
            "pid": pid,
            "pgid": pgid,
            "port": port,
            "output": str(output_dir.relative_to(self.run_dir)),
            "started_at": utc_now(),
        }
        self.write()

    def deactivate(self, *, cleanup_successful: bool) -> None:
        if cleanup_successful:
            self.data["active_resource"] = None
        self.data["cleanup_successful"] = cleanup_successful
        self.write()

    def note_signal(self, signum: int) -> None:
        self.data["last_signal"] = signum
        self.write()

    def finalize(self, *, status: str, cleanup_successful: bool) -> None:
        self.data["status"] = status
        self.data["cleanup_successful"] = cleanup_successful
        self.write()


def find_unclean_journals(run_root: Path) -> list[str]:
    """只读检查历史账本；发现活动 PID 记录时拒绝开始新任务。"""

    import json

    if not run_root.is_dir():
        return []
    unclean: list[str] = []
    for path in run_root.glob("*/*/recovery.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unclean.append(str(path.relative_to(run_root)))
            continue
        if value.get("active_resource") is not None or not value.get(
            "cleanup_successful", False
        ):
            unclean.append(str(path.relative_to(run_root)))
    return sorted(unclean)
