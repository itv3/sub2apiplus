"""完整 Sub2API 上游合并命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .canonical import bind_identity, canonical_bytes, expect_object, load_json, write_json_once
from .contracts import create_plan, load_plan
from .errors import UpstreamMergeError
from .workflow import (
    apply_candidate_to_managed_branch,
    carry_forward_inventory,
    finalize_upstream_merge,
    generate_impact_matrix,
    replay_upstream_merge,
    run_verification_gates,
    scan_surfaces,
    seal_candidate_disposition,
    seal_change_decision,
    seal_merge,
    seal_source_candidate,
    seal_surfaces,
    start_merge,
)


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("路径必须是绝对路径")
    return path


def _add_repository(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repository",
        type=_absolute,
        default=Path.cwd().resolve(),
        help="Sub2API fork 仓库绝对路径；默认当前目录",
    )


def _add_plan(parser: argparse.ArgumentParser) -> None:
    _add_repository(parser)
    parser.add_argument("--plan", required=True, type=_absolute, help="完整 v2 计划绝对路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.upstream_merge",
        description="Sub2API 上游合并 U-0～U-6 受管状态机",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("plan-create", help="从请求生成完整 U-0 计划")
    _add_repository(create)
    create.add_argument("--request", required=True, type=_absolute)

    validate = commands.add_parser("plan-validate", help="只读复算完整计划")
    _add_plan(validate)

    identity = commands.add_parser(
        "identity-seal",
        help="为人工决策草稿增加 identity_sha256，并写入新文件",
    )
    identity.add_argument("--input", required=True, type=_absolute)
    identity.add_argument("--output", required=True, type=_absolute)

    merge_start = commands.add_parser("merge-start", help="U-1 创建隔离 worktree 并开始合并")
    _add_plan(merge_start)

    merge_seal = commands.add_parser("merge-seal", help="U-1 封存冲突台账和双父 merge commit")
    _add_plan(merge_seal)
    merge_seal.add_argument("--conflict-decisions", type=_absolute)

    source_seal = commands.add_parser("source-seal", help="U-2 生成 overlay 并封存 source candidate")
    _add_plan(source_seal)
    source_seal.add_argument("--source-changes", type=_absolute)

    surface_scan = commands.add_parser("surface-scan", help="U-2 复算入口和 source-to-sink 差异")
    _add_plan(surface_scan)

    carry = commands.add_parser(
        "inventory-carry-forward",
        help="U-2 仅在相应发送面零差异时沿用 Inventory",
    )
    _add_plan(carry)
    carry.add_argument("--client", required=True, choices=("claude", "codex"))
    carry.add_argument("--kind", required=True, choices=("ingress", "egress"))

    surface_seal = commands.add_parser("surface-seal", help="U-2 封存两个 Persona 的发送面闭集")
    _add_plan(surface_seal)
    surface_seal.add_argument("--decisions", type=_absolute)

    impact_generate = commands.add_parser("impact-generate", help="U-3 生成完整影响矩阵")
    _add_plan(impact_generate)

    impact_seal = commands.add_parser("impact-seal", help="U-3 封存逐文件与调用边处置")
    _add_plan(impact_seal)
    impact_seal.add_argument("--decision", required=True, type=_absolute)

    gates = commands.add_parser("gates-run", help="U-4 执行全部固定门禁并生成 attempt 收据")
    _add_plan(gates)
    gates.add_argument("--attempt-id", required=True)

    disposition = commands.add_parser("disposition-seal", help="U-5 封存 candidate/Campaign 处置")
    _add_plan(disposition)
    disposition.add_argument("--input", required=True, type=_absolute)
    disposition.add_argument("--verification-receipt", required=True, type=_absolute)

    apply_parser = commands.add_parser(
        "apply",
        help="U-6 显式快进受维护分支；不会推送远端",
    )
    _add_plan(apply_parser)

    finalize = commands.add_parser("finalize", help="U-6 生成 UpstreamMergeReceipt")
    _add_plan(finalize)

    replay = commands.add_parser("replay", help="独立重建并核对 UpstreamMergeReceipt")
    _add_plan(replay)
    replay.add_argument("--receipt", required=True, type=_absolute)
    replay.add_argument(
        "--rerun-gates",
        metavar="ATTEMPT_ID",
        help="在全新隔离 worktree 重跑全部门禁，并以指定的新 attempt 封存",
    )
    return parser


def _loaded(arguments: argparse.Namespace):
    return load_plan(arguments.plan, arguments.repository)


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    command = arguments.command
    if command == "plan-create":
        plan = create_plan(arguments.request, arguments.repository)
        return {
            "result": "created",
            "plan": str(plan.path),
            "plan_id": plan.plan_id,
            "identity_sha256": plan.identity,
        }
    if command == "identity-seal":
        draft = expect_object(load_json(arguments.input, "identity draft"), "identity draft")
        if "identity_sha256" in draft:
            raise UpstreamMergeError("identity draft 已含 identity_sha256，禁止覆盖或重复签名")
        sealed = bind_identity(draft)
        write_json_once(arguments.output, sealed)
        return {
            "result": "sealed",
            "output": str(arguments.output),
            "identity_sha256": sealed["identity_sha256"],
        }
    plan = _loaded(arguments)
    if command == "plan-validate":
        return {
            "result": "valid",
            "plan_id": plan.plan_id,
            "identity_sha256": plan.identity,
        }
    if command == "merge-start":
        return start_merge(plan)
    if command == "merge-seal":
        return seal_merge(plan, arguments.conflict_decisions)
    if command == "source-seal":
        return seal_source_candidate(plan, arguments.source_changes)
    if command == "surface-scan":
        return scan_surfaces(plan)
    if command == "inventory-carry-forward":
        return carry_forward_inventory(plan, arguments.client, arguments.kind)
    if command == "surface-seal":
        return seal_surfaces(plan, arguments.decisions)
    if command == "impact-generate":
        return generate_impact_matrix(plan)
    if command == "impact-seal":
        return seal_change_decision(plan, arguments.decision)
    if command == "gates-run":
        return run_verification_gates(plan, arguments.attempt_id)
    if command == "disposition-seal":
        return seal_candidate_disposition(
            plan,
            arguments.input,
            arguments.verification_receipt,
        )
    if command == "apply":
        return apply_candidate_to_managed_branch(plan)
    if command == "finalize":
        return finalize_upstream_merge(plan)
    if command == "replay":
        return replay_upstream_merge(
            plan,
            arguments.receipt,
            arguments.rerun_gates,
        )
    raise UpstreamMergeError(f"未处理命令：{command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        result = execute(parser.parse_args(argv))
    except UpstreamMergeError as error:
        print(f"上游合并工具拒绝：{error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"上游合并工具系统错误：{error}", file=sys.stderr)
        return 3
    sys.stdout.buffer.write(canonical_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
