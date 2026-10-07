#!/usr/bin/env python3
"""Claude 的 VC-1 封存派发（Claude 升级 B2-11 第二部分，``vc1-claude.sh`` 调用）：首批之后的两种 VC-1 批次计划与收尾摘要。

为什么要有：VC-0 收口只派发 VC-1 首批（恰好一个 ``capture-official run`` 动作——共享监督器的 VC-1 普通恢复链要求首批恰有
一个动作）。Claude 的 seal 不在 run 里，VC-1 静态分析（seal 要的两份静态输入：目标 SinkInventory 与 sink containment）也不在
首批里，都由首批之后的 VC-1 批次派发，静态分析排在 seal 之前（设计 B2-11）：

* 预览批次：``vc1-01-analyze-target`` → ``vc1-02-seal-preview``（seal 预览停在等批准，是批次内的合法暂停点）；只读复用前序
  的 Campaign 在这一批里直接完成封存；
* 批准批次：``vc1-03-seal-approve``（带审核过的 review_sha256）。

动作取受管 ``claude_upgrade_static.vc1_seal_actions``（受管 Python 直接调 ``claude_upgrade.py``），计划经受管
``claude_upgrade_batch.action_plan`` 按共享合同（VC-1）校验后写进本轮输入目录 W。

子命令（只读，或只写本轮输入目录 W 下的动作计划）：

* ``plan --output <W 下的计划> [--approve-seal-sha256 <摘要>] [--discovery-dispositions <Campaign 内文件>]``：写批次计划并
  打印各动作（发现项的人工处置文件预览与批准要给同一份）；
* ``summary``：Campaign 状态与下一步、各代 seal 预览（代号、attempt、闭合状态、review_sha256，最新的在前）、官方阶段结果
  与 VC-1 checkpoint 是否已写。

只给 VC-1 派发脚本为 ``vc1-claude.sh`` 的客户端用（客户端表 ``vc1_dispatch_script``），其余客户端拒绝。退出码：0 成功；3 拒绝。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get("D", str(Path(__file__).resolve().parents[3])))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_env  # noqa: E402

DISPATCH_SCRIPT = "vc1-claude.sh"
PHASE = "VC-1"
SEAL_ROOT = Path("official") / "seal"
STAGE_RESULT = Path("official") / "result.json"
VC1_CHECKPOINT = Path("control") / "vc" / "vc-1-checkpoint.json"


def _upgrade() -> Any:
    """本轮客户端的编排器模块；VC-1 派发脚本不是本脚本的客户端在加载编排器之前就拒绝。"""

    table = parse_env.client_table()
    row = table.spec(table.resolve_client(os.environ.get(table.CLIENT_PARAM)))
    if row["vc1_dispatch_script"] != DISPATCH_SCRIPT:
        raise ValueError(f"本轮客户端（{row['client']}）的 VC-1 派发脚本是 {row['vc1_dispatch_script']}，不是 {DISPATCH_SCRIPT}")
    return importlib.import_module(row["orchestrator_module"])


def _campaign() -> Path:
    return Path(os.environ["NEWDIR"])


def _write_plan(path: Path, plan: dict[str, Any]) -> None:
    """计划只写进本轮输入目录 W（同一路径重跑时覆盖，0600）。"""

    if path.parent.resolve(strict=True) != Path(os.environ["W"]).resolve(strict=True):
        raise ValueError(f"动作计划只能写在本轮输入目录 W 下：{path}")
    if path.is_symlink():
        raise ValueError(f"动作计划路径是符号链接：{path}")
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def plan(output: Path, *, approve: str | None, dispositions: Path | None) -> dict[str, Any]:
    _upgrade()
    from tools.official_client_capture import claude_upgrade_batch
    from tools.official_client_capture import claude_upgrade_static

    actions = claude_upgrade_static.vc1_seal_actions(_campaign(), approve_seal_sha256=approve, discovery_dispositions=dispositions)
    payload = claude_upgrade_batch.action_plan(actions, phase=PHASE)
    _write_plan(output, payload)
    for action in payload["actions"]:
        print(f"动作 {action['action_id']}（{action['operation']}）：{' '.join(action['command'][2:5])} …")
    print(f"VC-1 批次计划 -> {output}")
    return payload


def summary() -> None:
    upgrade = _upgrade()
    campaign = _campaign()
    status = upgrade.campaign_status(campaign)
    print(f"Campaign 状态：{status.get('status')}；下一步：{status.get('next_command')}")
    previews = sorted((campaign / SEAL_ROOT).glob("*/seal-preview.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in previews:
        preview = json.loads(path.read_text(encoding="utf-8"))
        print("seal 预览：", json.dumps({
            "generation": preview.get("generation"), "attempt_id": preview.get("attempt_id"),
            "closure": (preview.get("closure") or {}).get("status"), "review_sha256": preview.get("review_sha256"),
        }, ensure_ascii=False))
    if not previews:
        print("seal 预览：没有")
    print(f"官方阶段结果：{'已写' if (campaign / STAGE_RESULT).is_file() else '未写'}；"
          f"VC-1 checkpoint：{'已写' if (campaign / VC1_CHECKPOINT).is_file() else '未写'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude 的 VC-1 封存派发（B2-11 第二部分）")
    sub = parser.add_subparsers(dest="action", required=True)
    plan_parser = sub.add_parser("plan", help="VC-1 封存批次计划（预览批次或批准批次）")
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--approve-seal-sha256", default=None, help="审核过的 seal 预览 review_sha256（批准批次）")
    plan_parser.add_argument("--discovery-dispositions", type=Path, default=None, help="发现项的人工处置文件（Campaign 内）")
    sub.add_parser("summary", help="Campaign 状态、seal 预览、阶段结果与 VC-1 checkpoint")
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            plan(args.output, approve=args.approve_seal_sha256, dispositions=args.discovery_dispositions)
        else:
            summary()
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(f"Claude VC-1 派发拒绝：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
