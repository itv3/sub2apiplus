"""任务 manifest 的原子更新。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .model import CampaignPlan, utc_now
from .security import inventory_artifacts, secure_write_json


class Manifest:
    """记录一套 OAuth 或 API 任务，失败时也保留可审计状态。"""

    def __init__(self, plan: CampaignPlan, run_dir: Path) -> None:
        self.path = run_dir / "manifest.json"
        self.run_dir = run_dir
        self.data: dict[str, Any] = {
            **plan.to_dict(),
            "status": "running",
            "started_at": utc_now(),
            "ended_at": None,
            "clients": {},
            "runtime": {},
            "case_results": [],
            "cleanup": {"attempted": False, "successful": None},
            "secret_scan": {
                "performed": False,
                "scope": [],
                "matches": [],
                "limitation": None,
            },
            "artifacts": [],
        }
        self.write()

    def write(self) -> None:
        """原子保存当前状态。"""

        secure_write_json(self.path, self.data)

    def set_clients(self, clients: dict[str, Any]) -> None:
        """记录精确版本、路径和二进制哈希。"""

        self.data["clients"] = copy.deepcopy(clients)
        self.write()

    def add_case_result(self, result: dict[str, Any]) -> None:
        """追加一个 case 的脱敏结果。"""

        self.data["case_results"].append(copy.deepcopy(result))
        self.write()

    def set_runtime(self, runtime: dict[str, Any]) -> None:
        """记录运行镜像、抓包工具、CA 与净化环境等可复现元数据。"""

        self.data["runtime"] = copy.deepcopy(runtime)
        self.write()

    def finalize(
        self,
        *,
        status: str,
        cleanup_successful: bool,
        secret_matches: list[str],
        secret_scan_scope: list[str] | None = None,
        secret_scan_limitation: str | None = None,
        error: str | None = None,
    ) -> None:
        """写入最终状态和产物索引。"""

        self.data["status"] = status
        self.data["ended_at"] = utc_now()
        self.data["cleanup"] = {
            "attempted": True,
            "successful": cleanup_successful,
        }
        self.data["secret_scan"] = {
            "performed": bool(secret_scan_scope),
            "scope": list(secret_scan_scope or []),
            "matches": list(secret_matches),
            "limitation": secret_scan_limitation,
        }
        if error:
            self.data["error"] = error
        self.data["artifacts"] = inventory_artifacts(self.run_dir)
        self.write()
