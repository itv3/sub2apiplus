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
import os
import pathlib
import sys

sys.dont_write_bytecode = True

from driver_config import load_config
from phase_context import resolve
from round_context import binding
CONFIG = load_config()
CONTEXT = resolve(CONFIG, mode="build")
PARAMS = CONTEXT["parameters"]
out = pathlib.Path(sys.argv[1])
campaign_dir = pathlib.Path(sys.argv[2])
if str(campaign_dir) != CONFIG["NEWDIR"] or out != pathlib.Path(CONFIG["W"]):
    raise SystemExit("恢复计划的 Campaign 或输出目录与本轮不一致")
preview_path = pathlib.Path(sys.argv[3])
preview_binding = binding(preview_path)
if not preview_path.is_absolute() or preview_path.is_symlink() or not preview_path.is_file():
    raise SystemExit(f"恢复预览必须是可信绝对路径普通文件：{preview_path}")
data_root = pathlib.Path(CONFIG["D"])
sys.path.insert(0, str(data_root))
from tools.official_client_capture import codex_upgrade_supervisor as supervisor  # noqa: E402

preview = json.loads(preview_path.read_text(encoding="utf-8"))
if preview.get("phase") != "candidate" or preview.get("recovery_revision") is not None:
    raise SystemExit("恢复预览不是候选采集非段模式的预览")
manifests = sorted((campaign_dir / "control" / "vc" / "run-manifests").glob("*-vc-5.json"))
if not manifests:
    raise SystemExit("Campaign 没有 VC-5 批次清单")
parent_path = None
parents = []
for candidate_path in manifests:
    if candidate_path.is_symlink():
        raise SystemExit("恢复父批次清单不能是符号链接")
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    if (payload.get("campaign_id"), payload.get("candidate_id"), payload.get("candidate_revision")) != (
        CONFIG["NEW"], CONFIG["CAND"], int(PARAMS["CANDIDATE_REVISION"])
    ):
        continue
    try:
        prefix, campaign, identity = supervisor.candidate_recovery_parent_identity(
            payload
        )
    except supervisor.SupervisorError:
        continue
    sequence = payload.get("batch_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise SystemExit("恢复父批次序号非法")
    parents.append((sequence, candidate_path, prefix, campaign, identity))
if parents:
    if len({row[0] for row in parents}) != len(parents):
        raise SystemExit("恢复父批次序号不唯一")
    _, parent_path, prefix, campaign, identity = max(parents, key=lambda row: row[0])
if parent_path is None:
    raise SystemExit("Campaign 没有候选采集或续跑批次清单，无法取候选身份参数")
parent_binding = binding(parent_path)
if pathlib.Path(campaign).resolve(strict=True) != campaign_dir:
    raise SystemExit("父批次命令的 Campaign 目录与参数不一致")
if identity["--candidate-id"] != preview.get("candidate_id") or preview.get("candidate_id") != CONFIG["CAND"]:
    raise SystemExit("恢复预览的候选与父批次命令的候选不一致")

expected = {
    "--candidate-id": CONFIG["CAND"], "--build-receipt": PARAMS["BUILD_RECEIPT"],
    "--candidate-image-id": PARAMS["IMAGE_ID"], "--candidate-source": PARAMS["SOURCE_ROOT"],
    "--build-id": PARAMS["BUILD_ID"], "--deployed-version": PARAMS["DEPLOYED"],
    "--profile-id": PARAMS["PROFILE_ID"], "--profile-digest": PARAMS["PROFILE_DIGEST"],
    "--candidate-purpose": "production_replacement",
}
if any(identity.get(key) != value for key, value in expected.items()):
    raise SystemExit("恢复父命令的构建、源码或画像参数与当前收据不一致")
if identity.get("--runtime-image") not in {PARAMS["IMAGE_REF"], CONFIG["CANDIDATE_IMAGE_REPOSITORY"] + "@" + PARAMS["IMAGE_ID"]}:
    raise SystemExit("恢复父命令的运行镜像与当前收据不一致")
if prefix != ["/usr/bin/python3", str(data_root / "tools/official_client_capture/codex_upgrade.py")]:
    raise SystemExit("恢复父命令不属于本轮受管工具")
# 这里只解析身份；批准与计费授权仍由原监督器在派发前重放，不调用可能写授权事件的检查入口。


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
if binding(preview_path) != preview_binding or binding(parent_path) != parent_binding:
    raise SystemExit("恢复预览或父批次在解析期间发生变化")
if len(sys.argv) > 4:
    if sys.argv[4:] != ["--dry-run"]:
        raise SystemExit("恢复计划可选参数只接受 --dry-run")
    print("恢复计划只读预检通过，未写入输出目录")
    raise SystemExit(0)
out.mkdir(parents=True, exist_ok=True)
for name, payload in plans.items():
    target = out / name
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
print("recovery plans ->", out, sorted(plans), "parent", parent_path.name,
      "execute", preview.get("execute_job_ids"), "reuse", preview.get("reuse_job_ids"))
