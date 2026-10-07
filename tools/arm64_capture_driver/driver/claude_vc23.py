#!/usr/bin/env python3
"""Claude 的 VC-2／VC-3 派发（Claude 升级 B5b-01，``vc23-claude.sh`` 调用）：八种批次计划与收尾摘要。

为什么不沿用 Codex 的 ``vc23.sh``：它手写 ``codex_upgrade.py`` 的 prepare-profile／classify／stage-profile 命令与 Codex 的五件套
联合摘要，而 Claude 的 VC-2、VC-3 是「机器草案 → 审核人修订 → 批准」的 revision 流程（设计 B3-02、B3-03），每一步的命令形态
都由 ``claude_upgrade_batch`` 的阶段幂等重派合同（``STAGE_REPLAY_CONTRACTS``）精确限定。所以计划一律经受管
``claude_upgrade_batch.managed_action``／``action_plan`` 生成（受管 Python 直接调 ``claude_upgrade.py``，旗标成对闭合），生成后
再按合同自检形态，与 ``claude_vc4.py`` 同一做法；审核人给出的审核摘要、联合摘要与批准人原样作参数传入，本脚本不自动批准。

八个步骤（步骤 → 阶段、动作 ID、命令形态）：

* VC-2（前序 VC-1）：``classify-draft``（只有 ``--campaign-dir``：分母等输入取 VC-1 封存）、``classify-revise``
  （``--from-revision``＋``--revision-input``）、``classify-approve``（``--approve-revision``＋``--approve-review-sha256``）；
* VC-3（前序 VC-2）：``prepare-draft``（Active 输入取总计划冻结的副本，命令行不另给）、``prepare-revise``、
  ``prepare-approve``（另带 ``--approved-by``）、``stage-preview``（只有 ``--campaign-dir``）、``stage-approve``
  （``--approve-joint-sha256``＋``--approved-by``）。

草案、修订与预览在父批次里是合法暂停点（退出 0）；审核摘要由审核人在批次之外读取（修订与准备草案可复跑同一命令，同内容
复用、不写文件），暂存预览的联合摘要见 ``summary`` 打印的冻结联合清单。

子命令（只读，或只写本轮输入目录 W 下的动作计划）：

* ``plan --step <步骤> --output <W 下的计划> [各步骤的参数]``：写批次计划并打印阶段、动作与形态；
* ``summary``：Campaign 状态、VC-2／VC-3 的 revision、批准结果、暂存预览的联合摘要与两个 checkpoint 是否已写。

只给 VC-2／VC-3 派发脚本为 ``vc23-claude.sh`` 的客户端用（客户端表 ``vc23_dispatch_script``），其余客户端拒绝。
退出码：0 成功；3 拒绝。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, NamedTuple

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get("D", str(Path(__file__).resolve().parents[3])))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_env  # noqa: E402

DISPATCH_SCRIPT = "vc23-claude.sh"
ACTION_TIMEOUT_SECONDS = 1800


class Step(NamedTuple):
    """一个步骤：阶段、前序阶段、子命令、合同模式与动作 ID，以及要求的参数（参数名 → 旗标）。驱动脚本按路径加载、不进
    sys.modules，所以用 NamedTuple 而不用 dataclass。"""

    phase: str
    predecessor: str
    subcommand: str
    mode: str
    action_id: str
    options: tuple[tuple[str, str], ...] = ()


STEPS: dict[str, Step] = {
    "classify-draft": Step("VC-2", "VC-1", "classify", "draft", "vc2-classify-draft"),
    "classify-revise": Step(
        "VC-2", "VC-1", "classify", "revise", "vc2-classify-revise",
        (("from_revision", "--from-revision"), ("revision_input", "--revision-input")),
    ),
    "classify-approve": Step(
        "VC-2", "VC-1", "classify", "approve", "vc2-classify-approve",
        (("approve_revision", "--approve-revision"), ("approve_review_sha256", "--approve-review-sha256")),
    ),
    "prepare-draft": Step("VC-3", "VC-2", "prepare-profile", "draft", "vc3-prepare-draft"),
    "prepare-revise": Step(
        "VC-3", "VC-2", "prepare-profile", "revise", "vc3-prepare-revise",
        (("from_revision", "--from-revision"), ("revision_input", "--revision-input")),
    ),
    "prepare-approve": Step(
        "VC-3", "VC-2", "prepare-profile", "approve", "vc3-prepare-approve",
        (("approve_revision", "--approve-revision"), ("approve_review_sha256", "--approve-review-sha256"), ("approved_by", "--approved-by")),
    ),
    "stage-preview": Step("VC-3", "VC-2", "stage-profile", "preview", "vc3-stage-preview"),
    "stage-approve": Step(
        "VC-3", "VC-2", "stage-profile", "approve", "vc3-stage-approve",
        (("approve_joint_sha256", "--approve-joint-sha256"), ("approved_by", "--approved-by")),
    ),
}
OPTION_NAMES = ("from_revision", "revision_input", "approve_revision", "approve_review_sha256", "approve_joint_sha256", "approved_by")


def _upgrade() -> Any:
    """本轮客户端的编排器模块；VC-2／VC-3 派发脚本不是本脚本的客户端在加载编排器之前就拒绝。"""

    table = parse_env.client_table()
    row = table.spec(table.resolve_client(os.environ.get(table.CLIENT_PARAM)))
    if row["vc23_dispatch_script"] != DISPATCH_SCRIPT:
        raise ValueError(f"本轮客户端（{row['client']}）的 VC-2／VC-3 派发脚本是 {row['vc23_dispatch_script']}，不是 {DISPATCH_SCRIPT}")
    return importlib.import_module(row["orchestrator_module"])


def _batch() -> Any:
    from tools.official_client_capture import claude_upgrade_batch

    return claude_upgrade_batch


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


def step_argv(name: str, values: dict[str, str | None]) -> list[str]:
    """步骤的编排器参数：要求的参数一个不少，其余参数一个不多（多给即拒绝，免得审核过的摘要被丢掉而不自知）。"""

    if name not in STEPS:
        raise ValueError(f"未知步骤：{name}（可选：{' '.join(STEPS)}）")
    step = STEPS[name]
    wanted = {option for option, _flag in step.options}
    extra = sorted(option for option in OPTION_NAMES if values.get(option) and option not in wanted)
    missing = sorted(option for option in wanted if not values.get(option))
    if extra or missing:
        raise ValueError(f"步骤 {name} 的参数不符：缺 {missing or '无'}，多余 {extra or '无'}")
    argv = [step.subcommand, "--campaign-dir", str(_campaign())]
    for option, flag in step.options:
        value = str(values[option])
        if option == "revision_input":
            directory = Path(value)
            if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"--revision-input 必须是审核人交回的绝对路径目录：{value}")
        argv += [flag, value]
    return argv


def plan(name: str, output: Path, values: dict[str, str | None]) -> dict[str, Any]:
    _upgrade()
    batch = _batch()
    argv = step_argv(name, values)
    step = STEPS[name]
    action = batch.managed_action(
        step.action_id, f"{step.phase}:{step.action_id}", argv, item_ids=[step.action_id], timeout_seconds=ACTION_TIMEOUT_SECONDS
    )
    payload = batch.action_plan([action], phase=step.phase)
    # 按阶段幂等重派合同自检形态（不闭合即拒绝，不等派发失败）。
    subcommand, mode, _flags = batch._parse_contract(step.phase, batch._orchestrator_argv(action["command"]), _campaign())
    if (subcommand, mode) != (step.subcommand, step.mode):
        raise ValueError(f"步骤 {name} 的命令形态被判为 {subcommand}（{mode}），不是 {step.subcommand}（{step.mode}）")
    _write_plan(output, payload)
    print(f"阶段 {step.phase}（前序 {step.predecessor}）动作 {step.action_id}：{' '.join(argv[:1] + argv[3:])}")
    print(f"VC-2／VC-3 批次计划 -> {output}")
    return payload


def _revisions(root: Path) -> list[str]:
    return sorted(path.name for path in root.glob("rev-*") if path.is_dir()) if root.is_dir() else []


def summary() -> None:
    upgrade = _upgrade()
    from tools.official_client_capture import claude_upgrade_classify as classify
    from tools.official_client_capture import claude_upgrade_profile as profile
    from tools.official_client_capture import claude_upgrade_vc

    campaign = _campaign()
    status = upgrade.campaign_status(campaign)
    print(f"Campaign 状态：{status.get('status')}；下一步：{status.get('next_command')}")
    print(f"VC-2 分类草案 revision：{_revisions(campaign / classify.DRAFT_ROOT) or '没有'}；"
          f"批准：{'已写' if (campaign / classify.STAGE_RESULT_RELATIVE).is_file() else '未写'}")
    print(f"VC-3 画像准备 revision：{_revisions(campaign / profile.PREPARE_DRAFT_ROOT) or '没有'}；"
          f"已批准准备结果：{sorted(path.stem for path in (campaign / profile.PREPARE_RESULTS_ROOT).glob('*.json')) or '没有'}")
    for joint in sorted((campaign / profile.STAGE_ROOT).glob(f"*/{profile.JOINT_MANIFEST_FILENAME}")):
        value = json.loads(joint.read_text(encoding="utf-8"))
        print("暂存预览：", json.dumps({"generation": joint.parent.name, "joint_sha256": value.get("joint_sha256")}, ensure_ascii=False))
    print(f"VC-3 阶段结果：{'已写' if (campaign / profile.STAGE_RESULT_RELATIVE).is_file() else '未写'}")
    for phase in ("VC-2", "VC-3"):
        path = claude_upgrade_vc.vc_checkpoint_path(campaign, phase)
        print(f"{phase} checkpoint：{'已写' if path.is_file() else '未写'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude 的 VC-2／VC-3 派发（B5b-01）")
    sub = parser.add_subparsers(dest="action", required=True)
    plan_parser = sub.add_parser("plan", help="VC-2／VC-3 批次计划")
    plan_parser.add_argument("--step", required=True, choices=sorted(STEPS))
    plan_parser.add_argument("--output", type=Path, required=True)
    for option in OPTION_NAMES:
        plan_parser.add_argument("--" + option.replace("_", "-"), dest=option, default=None)
    sub.add_parser("phase", help="打印步骤所属的阶段与前序阶段（供 vc23-claude.sh 派发）").add_argument("--step", required=True, choices=sorted(STEPS))
    sub.add_parser("summary", help="Campaign 状态、revision、批准结果、暂存预览与 checkpoint")
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            plan(args.step, args.output, {option: getattr(args, option) for option in OPTION_NAMES})
        elif args.action == "phase":
            _upgrade()
            step = STEPS[args.step]
            print(f"{step.phase} {step.predecessor}")
        else:
            summary()
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(f"Claude VC-2／VC-3 派发拒绝：{error}", file=sys.stderr)
        return 3
    except Exception as error:  # 编排器读侧的 ConfigurationError 等：照样以拒绝退出，不丢 traceback 给操作员
        print(f"Claude VC-2／VC-3 派发拒绝：{type(error).__name__}: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
