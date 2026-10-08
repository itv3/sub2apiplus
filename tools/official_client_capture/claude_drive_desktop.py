"""Claude Desktop 真实产品驱动；不使用独立 CLI 或猜测的命令替代应用。"""

from __future__ import annotations

from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c


def drive_product(task: Mapping[str, Any], recipe: Mapping[str, Any], *, recorder: Any) -> None:
    c.require(task["client_id"] == recipe["client_id"] == "claude-desktop", "Desktop 驱动收到其他产品")
    c.require(callable(getattr(recorder, "launch_desktop", None)), "真实 Desktop 启动后端尚未实现")
    recorder.launch_desktop(task, recipe)
    inherited = {item for reuse in task["scenario_reuse"] for item in reuse["scenario_ids"]}
    for scenario in task["scenarios"]:
        if scenario["id"] not in inherited:
            recorder.drive_scenario(task, scenario)
