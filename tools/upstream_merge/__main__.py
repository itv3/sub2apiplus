"""完整 Sub2API 上游合并命令行入口。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .canonical import bind_identity, canonical_bytes, expect_object, load_json, write_json_once
from .baseline import seal_baseline_acceptance, validate_baseline_acceptance
from .contracts import create_plan, load_plan
from .disposition_draft import DISPOSITION_PURPOSES, draft_candidate_disposition, parse_campaigns
from .errors import UpstreamMergeError
from .freeze import generate_freeze_successor
from .plan_inputs import AWAITING_MANUAL_INPUT
from .plan_replay import replay_trial_tree, seal_merge_with_replay
from .request_render import render_request
from .revision_advance import advance_revision
from .version_sync import DEFAULT_MAX_ATTEMPTS, sync_released_version
from .workflow import (
    apply_candidate_to_managed_branch,
    carry_forward_inventory,
    delete_ci_branch,
    finalize_upstream_merge,
    generate_change_decision_suggestion,
    generate_impact_matrix,
    import_ci_evidence,
    push_candidate_for_ci,
    replay_upstream_merge,
    preflight_revisions,
    run_verification_gates,
    run_preflight,
    scan_surfaces,
    seal_candidate_disposition,
    seal_change_decision,
    seal_merge,
    seal_source_candidate,
    seal_surfaces,
    generate_source_transition,
    validate_source_transition,
    start_merge,
)


TIMING_LEDGER_SCHEMA = "official-egress-upstream-timing-ledger/v1"
TIMING_LEDGER_NAME = "timing-ledger.jsonl"
# 账本落点按此顺序推断：显式 --timing-ledger，其次 Plan 目录（plan.json 所在目录即
# evidence root），再次各类输出／收据／输入所在目录。--repository 故意不参与推断，
# 避免把账本写进主仓库。
TIMING_LEDGER_ANCHORS = ("plan", "output", "receipt", "transition", "input", "request")


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

    preflight = commands.add_parser(
        "preflight",
        help="正式 U-0 前在临时 detached worktree 执行离线预检",
    )
    _add_repository(preflight)
    preflight.add_argument("--request", required=True, type=_absolute)
    preflight.add_argument(
        "--output",
        type=_absolute,
        help="可选的非权威预检报告路径；不会覆盖既有文件",
    )

    render = commands.add_parser(
        "request-render",
        help="按标准模板生成正式 request 与当前 HEAD 的运行态、回退点（plan-create 之前可重渲染）",
    )
    _add_repository(render)
    render.add_argument(
        "--plan-root",
        required=True,
        type=_absolute,
        help="Plan 私有目录，命名为 <上游 tag>-<yyyymmdd>-<序号>；不存在时创建（0700）",
    )
    render.add_argument("--upstream-tag", required=True, help="目标上游 tag，例如 v0.2.13")
    render.add_argument(
        "--baseline-acceptance",
        required=True,
        type=_absolute,
        help="最近一次 baseline-seal 的收据绝对路径",
    )
    render.add_argument(
        "--previous-evidence",
        type=_absolute,
        help="上一次走完 U-6 的 Plan evidence 根（四份 Inventory 基线）；默认在 Plan 目录同级里找最近一次",
    )

    baseline = commands.add_parser(
        "baseline-seal",
        help="将基线功能/证据检查草稿绑定到当前干净提交并封存",
    )
    _add_repository(baseline)
    baseline.add_argument("--input", required=True, type=_absolute)
    baseline.add_argument("--output", required=True, type=_absolute)

    baseline_validate = commands.add_parser(
        "baseline-validate",
        help="只读校验基线验收收据及其当前提交绑定",
    )
    _add_repository(baseline_validate)
    baseline_validate.add_argument("--receipt", required=True, type=_absolute)

    transition = commands.add_parser(
        "source-transition",
        help="从 Git 差异生成追加式 source-transition 链尾节点",
    )
    _add_repository(transition)
    transition.add_argument("--before", required=True, help="前序提交完整 SHA-1")
    transition.add_argument("--after", required=True, help="当前提交完整 SHA-1")
    transition.add_argument("--output", required=True, type=_absolute)
    transition.add_argument("--predecessor-register", type=_absolute)
    transition.add_argument("--reason")

    transition_validate = commands.add_parser(
        "source-transition-validate",
        help="复算 source-transition 链尾及其文件摘要",
    )
    _add_repository(transition_validate)
    transition_validate.add_argument("--transition", required=True, type=_absolute)

    freeze = commands.add_parser(
        "freeze-successor-generate",
        help="在最终 revision 一次性生成全部冻结台账的 successor 收据",
    )
    _add_repository(freeze)
    freeze.add_argument("--before", required=True, help="源码变化前的提交完整 SHA-1")
    freeze.add_argument(
        "--after",
        help="源码最终提交完整 SHA-1；省略时以当前工作树为后继状态",
    )
    freeze.add_argument("--tag", required=True, help="上游 tag 或批次标识，用于 scope 与文件命名")
    freeze.add_argument(
        "--output",
        type=_absolute,
        help="收据绝对路径，仓库内只能写入 docs/egress/maintenance/；--dry-run 时可省略",
    )
    freeze.add_argument("--reason", help="统一原因；每条 transition 会追加 path")
    freeze.add_argument(
        "--extra-worktree-path",
        action="append",
        default=[],
        help="commit 模式下追加工作树中已定稿的文件（如引用本收据的门禁文件），登记 before 提交摘要到当前摘要的边；可重复",
    )
    freeze.add_argument(
        "--deletion-reason",
        help="区间内删除冻结路径时的删除原因；给出后收据携带 deletion_proof，result 为 passed_with_deletions",
    )
    freeze.add_argument(
        "--historical-reader",
        action="append",
        default=[],
        help="承接被删除 Python 模块历史读取的仓库相对模块路径；删除 .py 时至少一个，可重复",
    )

    version_sync = commands.add_parser(
        "release-version-sync",
        help="发版后回写 VERSION 并生成冻结承接收据，两个提交一次推送（release 工作流调用）",
    )
    _add_repository(version_sync)
    version_sync.add_argument("--version", required=True, help="发版版本号，不带 v 前缀")
    version_sync.add_argument("--remote", default="origin", help="推送的远端；默认 origin")
    version_sync.add_argument("--branch", default="main", help="受维护分支；默认 main")
    version_sync.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"推送因主干前进被拒时的最多尝试次数；默认 {DEFAULT_MAX_ATTEMPTS}",
    )
    version_sync.add_argument(
        "--no-push",
        action="store_true",
        help="只在本地生成回写与承接两个提交，不推送（演练用）",
    )
    freeze.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出冻结命中、未登记路径与特殊待办，不落盘",
    )

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

    plan_replay = commands.add_parser(
        "plan-replay",
        help="U-1 把试验区定型树与主干新增提交合成本 Plan 合并候选（merge-start 之后、merge-seal 之前）",
    )
    _add_plan(plan_replay)
    plan_replay.add_argument("--trial-tree", required=True, help="试验区定型树（tree 或 commit）")
    plan_replay.add_argument("--trial-base", required=True, help="试验区合并时的 fork 基点提交")
    plan_replay.add_argument(
        "--previous-plan",
        type=_absolute,
        help="前序 Plan 的 evidence root；给出时输出与其合并候选树的差异清单",
    )

    merge_seal = commands.add_parser("merge-seal", help="U-1 封存冲突台账和双父 merge commit")
    _add_plan(merge_seal)
    merge_seal.add_argument("--conflict-decisions", type=_absolute)
    merge_seal.add_argument(
        "--replay-from",
        type=_absolute,
        help="前序 Plan 的 evidence root：三方 stage 对象与解决结果一致的冲突复用其决定，其余交人工",
    )

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

    impact_suggest = commands.add_parser(
        "impact-suggest",
        aliases=["change-decision-suggest"],
        help="U-3 按版本化组件映射生成安全分级 ChangeDecision 草稿",
    )
    _add_plan(impact_suggest)
    impact_suggest.add_argument("--output", required=True, type=_absolute)


    impact_seal = commands.add_parser("impact-seal", help="U-3 封存逐文件与调用边处置")
    _add_plan(impact_seal)
    impact_seal.add_argument("--decision", required=True, type=_absolute)

    revision_advance = commands.add_parser(
        "revision-advance",
        help="推进一个源码 revision：source-seal 到 revision-preflight 一条命令，停在人工输入时 --resume 续跑",
    )
    _add_plan(revision_advance)
    revision_advance.add_argument(
        "--source-changes",
        type=_absolute,
        help="本轮 SourceChangeInput；可只含 entries（路径与理由），机械字段由工具补齐",
    )
    revision_advance.add_argument("--resume", action="store_true", help="续跑最近一轮未封存完的 revision")

    revision_preflight = commands.add_parser(
        "revision-preflight",
        help="只读复核最新 U-2/U-3 收据及可选 source-transition 链",
    )
    _add_plan(revision_preflight)
    revision_preflight.add_argument(
        "--transition",
        action="append",
        type=_absolute,
        help="要复核的 source-transition 链尾；可重复指定",
    )

    gates = commands.add_parser("gates-run", help="U-4 执行全部固定门禁并生成 attempt 收据")
    _add_plan(gates)
    gates.add_argument("--attempt-id", required=True)
    gates.add_argument(
        "--only",
        help="只执行指定门禁 id 或 category（逗号分隔）；其余从上一 attempt 复用",
    )
    gates.add_argument(
        "--from-attempt",
        help="上一 attempt 的安全标识、目录或 evidence root 内的 receipt.json",
    )

    ci_push = commands.add_parser(
        "ci-push",
        help="U-4 把封存的候选提交推到 upstream-merge/<plan_id> 跑 CI；不推送受维护分支",
    )
    _add_plan(ci_push)
    ci_push.add_argument("--remote", default="origin", help="推送的远端；默认 origin")

    import_ci = commands.add_parser(
        "gates-import-ci",
        help="U-4 导入同一候选提交的 CI 证据，只补齐本机未执行的检查（前序须为 awaiting_ci）",
    )
    _add_plan(import_ci)
    import_ci.add_argument("--attempt-id", required=True, help="新 attempt 标识")
    import_ci.add_argument("--from-attempt", required=True, help="awaiting_ci 的前序 attempt")
    import_ci.add_argument("--run", type=int, help="CI run id；省略时按候选提交 SHA 查找")
    import_ci.add_argument("--repository-slug", help="GitHub owner/repo；默认从 origin 解析")

    ci_cleanup = commands.add_parser(
        "ci-cleanup",
        help="U-6 之后删除临时 CI 分支 upstream-merge/<plan_id>",
    )
    _add_plan(ci_cleanup)
    ci_cleanup.add_argument("--remote", default="origin", help="远端；默认 origin")

    disposition_draft = commands.add_parser(
        "disposition-draft",
        help="U-5 由验收收据派生原业务回归收据、共享合同草稿与处置输入",
    )
    _add_plan(disposition_draft)
    disposition_draft.add_argument("--attempt-id", required=True, help="通过的 U-4 attempt")
    disposition_draft.add_argument(
        "--campaign",
        action="append",
        help="受影响客户端的 Campaign 收据：claude=绝对路径 或 codex=绝对路径，可重复",
    )
    disposition_draft.add_argument("--purpose", choices=DISPOSITION_PURPOSES, default="validation_only")
    disposition_draft.add_argument("--dry-run", action="store_true", help="只打印三份文档，不写文件")

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
    registered: set[int] = set()
    for subparser in commands.choices.values():
        # 带别名的子命令在 choices 中出现多次，但只能注册一次。
        if id(subparser) in registered:
            continue
        registered.add(id(subparser))
        subparser.add_argument(
            "--timing-ledger",
            type=_absolute,
            help="时间账本 jsonl 绝对路径；默认追加到 Plan 目录或输出所在目录的 timing-ledger.jsonl",
        )
    return parser


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _inside_git_worktree(path: Path) -> bool:
    """推断出的账本目录若在任何 Git 工作树内，就不能自动写入，以免污染仓库。"""

    probe = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def timing_ledger_path(arguments: argparse.Namespace) -> tuple[Path | None, str | None]:
    """推断本次命令的时间账本路径。

    返回 (路径, 跳过原因)。显式 --timing-ledger 始终生效；推断路径位于 Git 工作树内
    （例如把收据写进 docs/egress/maintenance）或没有任何输出锚点时返回 None。
    """

    explicit = getattr(arguments, "timing_ledger", None)
    if isinstance(explicit, Path):
        return explicit, None
    plan_root = getattr(arguments, "plan_root", None)
    if isinstance(plan_root, Path):
        # request-render 的锚点是 Plan 目录本身，账本就写在它下面；命令失败且目录未建时不写。
        if plan_root.is_dir() and not plan_root.is_symlink():
            return plan_root / TIMING_LEDGER_NAME, None
        return None, "Plan 目录尚未创建"
    for attribute in TIMING_LEDGER_ANCHORS:
        anchor = getattr(arguments, attribute, None)
        if isinstance(anchor, list) and anchor and isinstance(anchor[0], Path):
            anchor = anchor[0]
        if isinstance(anchor, Path):
            inferred = anchor.parent / TIMING_LEDGER_NAME
            if inferred.parent.is_dir() and _inside_git_worktree(inferred.parent):
                return None, f"推断路径位于 Git 工作树内：{inferred}"
            return inferred, None
    return None, "命令没有输出锚点"


def timing_record(
    arguments: argparse.Namespace,
    started: float,
    ended: float,
    status: str,
    exit_code: int,
    error: str | None,
) -> dict[str, Any]:
    """一条时间账本记录：命令、参数、起止时间、耗时与结果。"""

    return {
        "schema_version": TIMING_LEDGER_SCHEMA,
        "command": arguments.command,
        "arguments": {
            key: _jsonable(value)
            for key, value in sorted(vars(arguments).items())
            if key not in {"command", "timing_ledger"} and value is not None
        },
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "ended_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended)),
        "duration_seconds": round(ended - started, 3),
        "status": status,
        "exit_code": exit_code,
        "error": error,
        "cwd": str(Path.cwd()),
    }


def append_timing_record(path: Path, record: dict[str, Any]) -> None:
    """以追加方式写入一行 canonical JSON；只追加，不改写既有行。"""

    if not path.is_absolute():
        raise UpstreamMergeError("时间账本路径必须是绝对路径")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise UpstreamMergeError(f"时间账本必须是普通文件：{path}")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise UpstreamMergeError(f"时间账本父目录不存在或不可信：{parent}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(canonical_bytes(record))


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
    if command == "preflight":
        report = run_preflight(arguments.request, arguments.repository, arguments.output)
        return {
            "result": report["result"],
            "plan_id": report["plan_id"],
            "identity_sha256": report["identity_sha256"],
            "report": str(arguments.output) if arguments.output else None,
            "blockers": report["blockers"],
        }
    if command == "request-render":
        return render_request(
            arguments.repository,
            plan_root=arguments.plan_root,
            upstream_tag=arguments.upstream_tag,
            baseline_acceptance=arguments.baseline_acceptance,
            previous_evidence=arguments.previous_evidence,
        )
    if command == "baseline-seal":
        receipt = seal_baseline_acceptance(
            arguments.repository,
            arguments.input,
            arguments.output,
        )
        return {
            "result": receipt["result"],
            "receipt": str(arguments.output),
            "commit": receipt["repository"]["commit"],
            "tree": receipt["repository"]["tree"],
            "known_drift_count": len(receipt["evidence"]["known_drift"]),
            "identity_sha256": receipt["identity_sha256"],
        }
    if command == "baseline-validate":
        receipt = validate_baseline_acceptance(arguments.repository, arguments.receipt)
        return {
            "result": receipt["result"],
            "receipt": str(arguments.receipt),
            "commit": receipt["repository"]["commit"],
            "tree": receipt["repository"]["tree"],
            "known_drift_count": len(receipt["evidence"]["known_drift"]),
            "identity_sha256": receipt["identity_sha256"],
        }
    if command == "source-transition":
        return generate_source_transition(
            arguments.repository,
            arguments.before,
            arguments.after,
            arguments.output,
            predecessor_register=arguments.predecessor_register,
            reason=arguments.reason,
        )
    if command == "source-transition-validate":
        return validate_source_transition(arguments.repository, arguments.transition)
    if command == "freeze-successor-generate":
        return generate_freeze_successor(
            arguments.repository,
            arguments.before,
            arguments.after,
            arguments.output,
            tag=arguments.tag,
            reason=arguments.reason,
            dry_run=arguments.dry_run,
            extra_worktree_paths=arguments.extra_worktree_path,
            deletion_reason=arguments.deletion_reason,
            historical_readers=arguments.historical_reader,
        )
    if command == "release-version-sync":
        return sync_released_version(
            arguments.repository,
            arguments.version,
            remote=arguments.remote,
            branch=arguments.branch,
            max_attempts=arguments.max_attempts,
            push=not arguments.no_push,
        )
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
    if command == "plan-replay":
        return replay_trial_tree(
            plan,
            arguments.trial_tree,
            arguments.trial_base,
            arguments.previous_plan,
        )
    if command == "merge-seal":
        if arguments.replay_from is not None:
            return seal_merge_with_replay(plan, arguments.replay_from, arguments.conflict_decisions)
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
    if command in {"impact-suggest", "change-decision-suggest"}:
        suggestion = generate_change_decision_suggestion(plan, arguments.output)
        return {
            "result": suggestion["result"],
            "output": str(arguments.output),
            "auto_accepted_count": suggestion["auto_accepted_count"],
            "manual_required_count": suggestion["manual_required_count"],
            "unresolved_paths": suggestion["unresolved_paths"],
        }
    if command == "impact-seal":
        return seal_change_decision(plan, arguments.decision)
    if command == "revision-advance":
        return advance_revision(plan, arguments.source_changes, resume=arguments.resume)
    if command == "revision-preflight":
        return preflight_revisions(plan, arguments.transition)
    if command == "gates-run":
        # 门禁输出边跑边写进 attempt 目录（UM-19）；全量门禁约 16 分钟，运行中据此查看进度。
        attempt_root = plan.evidence_root / plan.output_relative("gate_attempts_root") / arguments.attempt_id
        print(f"门禁输出实时写入 {attempt_root}/<执行组>.stdout.txt，运行中可用 tail -f 查看", file=sys.stderr, flush=True)
        return run_verification_gates(
            plan,
            arguments.attempt_id,
            only=arguments.only,
            from_attempt=arguments.from_attempt,
        )
    if command == "ci-push":
        return push_candidate_for_ci(plan, remote=arguments.remote)
    if command == "gates-import-ci":
        receipt = import_ci_evidence(
            plan,
            arguments.attempt_id,
            arguments.from_attempt,
            run_id=arguments.run,
            repository_slug=arguments.repository_slug,
        )
        return {
            "result": receipt["result"],
            "attempt_id": receipt["attempt_id"],
            "ci_run_id": receipt["ci_evidence"]["run_id"],
            "covers": receipt["ci_evidence"]["covers"],
            "identity_sha256": receipt["identity_sha256"],
        }
    if command == "ci-cleanup":
        return delete_ci_branch(plan, remote=arguments.remote)
    if command == "disposition-draft":
        return draft_candidate_disposition(
            plan,
            arguments.attempt_id,
            parse_campaigns(arguments.campaign),
            purpose=arguments.purpose,
            dry_run=arguments.dry_run,
        )
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
    arguments = parser.parse_args(argv)
    started = time.time()
    status, error_text, exit_code, result = "ok", None, 0, None
    try:
        result = execute(arguments)
        if isinstance(result, dict) and result.get("result") == AWAITING_MANUAL_INPUT:
            # 停在人工输入：已完成的步骤保留，结果照常输出，退出码 4 让脚本不会误以为已完成。
            status, exit_code = "awaiting_input", 4
    except UpstreamMergeError as error:
        status, error_text, exit_code = "rejected", str(error), 2
        print(f"上游合并工具拒绝：{error}", file=sys.stderr)
    except OSError as error:
        status, error_text, exit_code = "system_error", str(error), 3
        print(f"上游合并工具系统错误：{error}", file=sys.stderr)
    ended = time.time()
    ledger, skipped = timing_ledger_path(arguments)
    if ledger is None and skipped is not None and getattr(arguments, "dry_run", False) is False:
        if "工作树" in skipped:
            print(f"时间账本未写入：{skipped}；请用 --timing-ledger 指定 Plan 目录", file=sys.stderr)
    if ledger is not None:
        try:
            append_timing_record(
                ledger,
                timing_record(arguments, started, ended, status, exit_code, error_text),
            )
        except (OSError, UpstreamMergeError) as error:
            # 账本是发版前置条件之一：写不进去必须可见，成功的命令也按系统错误返回。
            print(f"时间账本写入失败：{error}", file=sys.stderr)
            if exit_code == 0:
                exit_code = 3
    if exit_code in (0, 4) and result is not None:
        sys.stdout.buffer.write(canonical_bytes(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
