"""VS Code 宿主与真实 Claude 扩展驱动；扩展加载和内置 CLI 参与由来源读侧重验。"""

from __future__ import annotations

from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c


def drive_product(task: Mapping[str, Any], recipe: Mapping[str, Any], *, recorder: Any) -> None:
    c.require(task["client_id"] == recipe["client_id"] == "claude-vscode", "VS Code 驱动收到其他产品")
    c.require(callable(getattr(recorder, "launch_vscode", None)), "真实 VS Code 启动后端尚未实现")
    recorder.launch_vscode(task, recipe)
    inherited = {item for reuse in task["scenario_reuse"] for item in reuse["scenario_ids"]}
    for scenario in task["scenarios"]:
        if scenario["id"] not in inherited:
            recorder.drive_scenario(task, scenario)
