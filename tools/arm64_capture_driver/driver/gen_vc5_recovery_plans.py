#!/usr/bin/env python3
"""生成 VC-5 候选采集续跑的两份动作计划（修好接着跑）。

用法：gen_vc5_recovery_plans.py <输出目录> <Campaign 目录> <已授权的 recovery-preview 绝对路径>

从 Campaign 最新的、唯一动作为候选采集／续跑预览／续跑补跑的 VC-5 批次清单取解释器前缀、Campaign 目录与
候选身份参数（其后可能还有 seal 链批次，例如工具演进作废作业后 seal 被拒），调用监督器后继协议同一组构造函数生成：
  action-plan-vc5-recovery-preview.json：零请求恢复预览（resume --rerun-failed --preview-recovery）；
  action-plan-vc5-recovery-run.json：按已批准预览的真实补跑（resume --rerun-failed --recovery-preview …）。
两份计划的 execute／reuse 与父批次相同（execute=candidate-run，reuse 为空），满足续跑后继协议的逐字要求。
只读 Campaign，不写账本、不派发。
"""

import json
import pathlib
import sys

sys.dont_write_bytecode = True

out = pathlib.Path(sys.argv[1])
campaign_dir = pathlib.Path(sys.argv[2]).resolve(strict=True)
preview_path = pathlib.Path(sys.argv[3])
if not preview_path.is_absolute() or preview_path.is_symlink() or not preview_path.is_file():
    raise SystemExit(f"恢复预览必须是可信绝对路径普通文件：{preview_path}")
data_root = campaign_dir.parents[2]
sys.path.insert(0, str(data_root))
from tools.official_client_capture import codex_upgrade_supervisor as supervisor  # noqa: E402

preview = json.loads(preview_path.read_text(encoding="utf-8"))
if preview.get("phase") != "candidate" or preview.get("recovery_revision") is not None:
    raise SystemExit("恢复预览不是候选采集非段模式的预览")
manifests = sorted((campaign_dir / "control" / "vc" / "run-manifests").glob("*-vc-5.json"))
if not manifests:
    raise SystemExit("Campaign 没有 VC-5 批次清单")
parent_path = None
for candidate_path in reversed(manifests):
    try:
        prefix, campaign, identity = supervisor.candidate_recovery_parent_identity(
            json.loads(candidate_path.read_text(encoding="utf-8"))
        )
    except supervisor.SupervisorError:
        continue
    parent_path = candidate_path
    break
if parent_path is None:
    raise SystemExit("Campaign 没有候选采集或续跑批次清单，无法取候选身份参数")
if pathlib.Path(campaign).resolve(strict=True) != campaign_dir:
    raise SystemExit("父批次命令的 Campaign 目录与参数不一致")
if identity["--candidate-id"] != preview.get("candidate_id"):
    raise SystemExit("恢复预览的候选与父批次命令的候选不一致")


def plan(action_id: str, command: list[str], timeout: int) -> dict:
    return {
        "schema_version": "codex-upgrade-vc-action-plan/v1",
        "execute_item_ids": ["candidate-run"],
        "reuse_item_ids": [],
        "actions": [
            {
                "action_id": action_id,
                "operation": supervisor.CANDIDATE_RECOVERY_OPERATION,
                "timeout_seconds": timeout,
                "command": command,
                "item_ids": ["candidate-run"],
            }
        ],
    }


out.mkdir(parents=True, exist_ok=True)
plans = {
    "action-plan-vc5-recovery-preview.json": plan(
        supervisor.CANDIDATE_RECOVERY_PREVIEW_ACTION_ID,
        supervisor.candidate_recovery_preview_command(prefix, campaign, identity),
        1800,
    ),
    "action-plan-vc5-recovery-run.json": plan(
        supervisor.CANDIDATE_RECOVERY_RUN_ACTION_ID,
        supervisor.candidate_recovery_run_command(prefix, campaign, identity, str(preview_path)),
        21600,
    ),
}
for name, payload in plans.items():
    target = out / name
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
print("recovery plans ->", out, sorted(plans), "parent", parent_path.name,
      "execute", preview.get("execute_job_ids"), "reuse", preview.get("reuse_job_ids"))
