"""试验区快线（``make upstream-gate-fast``）：试验 worktree 定型后、建正式 Plan 之前的检查集合。

v0.2.17 试验区全量门禁 64 分 29 秒，其中采集工具线 33 分 38 秒、go 测试线 14 分 29 秒；这两条线查出的失败全部是
冻结摘要类（试验区还没生成台账，本来就会红），没有一条功能问题。快线跳过这两条整线，换成“冲突相关的定向回归”，
功能问题仍在试验区发现，正式 U-4 全量兜底。

检查线（按执行顺序，遇错不停，按实测耗时从短到长，首条结论在一分钟内给出）：

* ``toolchain``：候选 ``toolchain-check``（候选树自己声明的 Go／lint 版本与本机实际版本一致），约 0.1 秒；
* ``secret-scan``：候选密钥扫描，与 U-4 同一份 ``secret_scan`` 门禁（取自 request 或标准模板），不到 1 秒；
* ``lint``：golangci-lint 默认、unit、integration 三种标签（遵循 ``UPSTREAM_GATE_GOLANGCI_LINT``），约 12 秒；
* ``frontend``：lint:check、typecheck、关键 vitest，约 28 秒；
* ``targeted-regression``：定向回归清单（见 ``targeted_regression.py``）里的 go test、前端用例、部署脚本检查等；
  清单要求退回全量时跑三组全量 go test；v0.2.18 演示清单约 10 分钟；
* ``egress-spec``：``make -k check-egress-spec``，归因只认 ``egress-spec-go-test`` 里的冻结摘要比对。试验区没有台账时
  冻结测试失败，执行器还会单独重跑一次失败单元做诊断，实测约 17 分钟，是最长的一条，所以放在功能检查之后；
* ``worktree``：全部检查结束后候选树必须仍然干净。

失败归因：``functional`` 阻断；``frozen_digest_pending_ledger``（冻结摘要类，台账尚未生成）记为“待台账闭合”，
不阻断快线。归因不清的一律按功能失败处理，规则见 ``fast_attribution.py``。总结论：

* ``passed``：全部通过；
* ``passed_pending_ledger``：失败项**只有**冻结摘要类，可以建正式 Plan，台账在 Plan 的最终 revision 由工具一次性生成；
* ``failed``：有功能失败（含归因不清的），不得建 Plan，按现行修复规则处理。

快线记录不是 U-4 全量收据：状态文件用独立 schema（``FAST_STATUS_SCHEMA``），``workflow._load_runner_status``
只认 full 模式的 schema，拒绝快线记录；正式 U-4 保留完整验收，不因快线通过而缺项。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import expect_object, file_binding, load_json
from .errors import UpstreamMergeError
from .fast_attribution import CLASS_FROZEN_PENDING, CLASS_FUNCTIONAL, CLASSIFICATIONS
from .gate_runner import (
    ATTRIBUTION_EGRESS_SPEC,
    ATTRIBUTION_GO_TEST,
    BUILD_TAGS,
    FAST_STATUS_SCHEMA,
    STATUS_SCHEMA,
    Lane,
    Step,
    StepResult,
    _go_test_step,
    _lint_step,
    resolve_integration,
    run_lanes,
)
from .gitops import assert_git_repository, commit_tree, rev_parse, status_paths
from .targeted_regression import (
    GO_TAG_SETS,
    frontend_vitest_argv,
    go_test_argv,
    load_targeted_regression,
)
from .toolchain import check_toolchain, describe_mismatches

RESULT_PASSED = "passed"
RESULT_PENDING = "passed_pending_ledger"
RESULT_FAILED = "failed"
RESULTS = (RESULT_PASSED, RESULT_PENDING, RESULT_FAILED)
DEFAULT_REQUEST_RELATIVE = "tools/upstream_merge/request_template_v2.json"
PENDING_LEDGER_LABEL = "待台账闭合"
NOT_U4_NOTICE = (
    "本记录是试验区快线记录，不是 U-4 全量收据：正式 U-4 仍执行完整的 12 类门禁（含采集工具线与整套 go test），"
    "快线通过不减少 U-4 的任何覆盖项"
)
CACHE_NOTES = (
    "对比耗时的两次运行必须是同一候选、同一台机器；Go 编译缓存会影响先后两次的耗时（无法关闭），go test 一律 -count=1 不用结果缓存",
    "快线比原全量多出的耗时来自新增的定向回归（targeted-regression 线）与前置检查（toolchain、secret-scan），状态里分线列出",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 启动前检查与上下文
# ---------------------------------------------------------------------------


@dataclass
class FastContext:
    """一次快线运行的输入与事实。"""

    repository_root: Path
    codex_source_root: Path
    manifest: dict[str, Any]
    manifest_path: Path
    manifest_sha256: str
    request_path: Path
    request_sha256: str
    status_file: Path
    log_dir: Path
    commit: str
    tree: str
    compare_with: Path | None = None
    # 内联前置检查记下的环境事实（Go／lint 版本、密钥扫描门禁 id），写进状态文件。
    records: dict[str, Any] = field(default_factory=dict)


def prepare_fast_run(
    repository_root: Path,
    codex_source_root: Path,
    *,
    targeted: Path | None,
    request: Path | None,
    status_file: Path | None,
    log_dir: Path | None,
    compare_with: Path | None,
    check_frontend: bool = True,
) -> FastContext:
    """快线开跑前的检查：清单可信且绑定当前 HEAD、候选树干净、前端依赖到位、输出位置未被占用。

    任何一项不满足都抛 ``UpstreamMergeError``，不执行任何检查线（拒绝比跑出无效结论好）。
    """

    root = assert_git_repository(repository_root)
    if targeted is None:
        raise UpstreamMergeError(
            "快线需要定向回归清单：先运行 targeted-regression-plan 生成，再用 UPSTREAM_GATE_TARGETED=<清单.json> 或 --targeted 传入"
        )
    manifest_path = targeted if targeted.is_absolute() else (Path.cwd() / targeted)
    manifest = load_targeted_regression(manifest_path)
    commit = rev_parse(root, "HEAD^{commit}")
    planned = manifest["inputs"]["candidate_commit"]
    if planned != commit:
        raise UpstreamMergeError(
            f"定向回归清单针对的候选提交 {planned[:12]} 不是当前 HEAD {commit[:12]}：候选变化后须重新生成清单"
        )
    dirty = status_paths(root)
    if dirty:
        raise UpstreamMergeError(
            f"试验 worktree 有未提交的改动（{', '.join(dirty[:5])}{' 等' if len(dirty) > 5 else ''}）："
            "快线按提交核对清单，先提交或清理，再重新生成清单"
        )
    if check_frontend:
        from .workflow import _assert_frontend_dependencies

        _assert_frontend_dependencies(root)
    request_path = request if request is not None else root / DEFAULT_REQUEST_RELATIVE
    if not request_path.is_absolute():
        request_path = Path.cwd() / request_path
    if request_path.is_symlink() or not request_path.is_file():
        raise UpstreamMergeError(f"request 或标准模板不是可信普通文件：{request_path}")
    resolved_log_dir = log_dir if log_dir is not None else None
    if resolved_log_dir is not None:
        if not resolved_log_dir.is_absolute():
            resolved_log_dir = Path.cwd() / resolved_log_dir
        if resolved_log_dir.exists():
            raise UpstreamMergeError(f"快线日志目录已存在，禁止覆盖上一轮证据：{resolved_log_dir}")
    resolved_status = status_file
    if resolved_status is not None:
        if not resolved_status.is_absolute():
            resolved_status = Path.cwd() / resolved_status
        if resolved_status.exists() or resolved_status.is_symlink():
            raise UpstreamMergeError(f"快线状态文件已存在，禁止覆盖上一轮证据：{resolved_status}")
        if not resolved_status.parent.is_dir():
            # 状态文件在整轮结束后才写；目录不存在要在开跑前就拒绝，不能跑完才发现写不出结果。
            raise UpstreamMergeError(f"快线状态文件所在目录不存在：{resolved_status.parent}")
    if compare_with is not None and not compare_with.is_file():
        raise UpstreamMergeError(f"对比用的全量状态文件不存在：{compare_with}")
    # 全部校验通过之后才创建日志目录，拒绝启动不留垃圾。
    if resolved_log_dir is None:
        resolved_log_dir = Path(tempfile.mkdtemp(prefix="upstream-gate-fast-"))
    else:
        resolved_log_dir.mkdir(parents=True, mode=0o700)
    if resolved_status is None:
        resolved_status = resolved_log_dir / "fast-status.json"
    return FastContext(
        repository_root=root,
        codex_source_root=codex_source_root,
        manifest=manifest,
        manifest_path=manifest_path,
        manifest_sha256=file_binding(manifest_path)["sha256"],
        request_path=request_path,
        request_sha256=file_binding(request_path)["sha256"],
        status_file=resolved_status,
        log_dir=resolved_log_dir,
        commit=commit,
        tree=commit_tree(root, commit),
        compare_with=compare_with,
    )


# ---------------------------------------------------------------------------
# 检查线
# ---------------------------------------------------------------------------


def _toolchain_inline(context: FastContext) -> Callable[[], tuple[int, str]]:
    def run() -> tuple[int, str]:
        report = check_toolchain(context.repository_root, "candidate")
        context.records["toolchain"] = {
            "result": report["result"],
            "go": report["actual"]["go"],
            "golangci_lint": report["actual"]["golangci_lint"],
            "expected": report.get("expected"),
        }
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if report["result"] != "passed":
            text += "候选工具链版本核对未通过：" + "；".join(describe_mismatches(report)) + "\n"
            return 1, text
        return 0, text

    return run


def _secret_scan_inline(context: FastContext) -> Callable[[], tuple[int, str]]:
    def run() -> tuple[int, str]:
        from .gate_guard import candidate_secret_scan_from_request

        report = candidate_secret_scan_from_request(context.request_path, context.repository_root)
        context.records["secret_scan"] = {
            "result": report["result"],
            "gate_id": report["scan"].get("gate_id"),
            "request": str(context.request_path),
            "request_sha256": context.request_sha256,
        }
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if report["result"] != "passed":
            text += "候选密钥扫描未通过：" + "；".join(report["blockers"]) + "\n"
            return 1, text
        return 0, text

    return run


def render_checklist(manifest: dict[str, Any]) -> str:
    """把定向回归清单渲染成人读的检查清单：每个定向项与冲突／依赖的对应关系、不需要测试的文件、需要补足验证的文件。"""

    go = manifest["go"]
    facts = manifest["facts"]
    summary = manifest["summary"]
    lines = [
        "===== 定向回归清单 =====",
        f"候选提交 {manifest['inputs']['candidate_commit']}；fork {manifest['inputs']['fork_commit'][:12]}；"
        f"上游 {manifest['inputs']['upstream_ref']}（{manifest['inputs']['upstream_commit'][:12]}）；merge-base {manifest['inputs']['merge_base'][:12]}",
        f"冲突文件 {summary['conflict_file_count']} 个；实际修改 {summary['actual_modified_count']} 个；"
        f"仅上游改动且与上游相同 {facts['upstream_only_unchanged_count']} 个（Go 包仍按依赖规则纳入）",
    ]
    if go["fallback_full_go"]:
        lines.append("Go：退回全量 go test（默认、unit、integration 三组），理由：")
        lines.extend(f"  - {reason}" for reason in go["fallback_reasons"])
    else:
        lines.append(
            f"Go：定向 go test（估算 {go['estimated_seconds']} 秒，预算 {go['budget_seconds']} 秒；"
            f"integration 不在快线内：{go['integration']['reason']}）"
        )
        for label, data in go["tag_sets"].items():
            lines.append(f"  标签组 {label}：{len(data['packages'])} 个包" + ("" if data["packages"] else "（无）"))
            for covered in data["covered_by_egress_spec_lane"]:
                lines.append(f"    不重复执行 {covered}：出站规格线已按默认标签完整跑过")
        for entry in go["affected_packages"]:
            lines.append(f"  包 {entry['dir'] or '.'}：{'；'.join(entry['reasons'])}")
    frontend = manifest["frontend"]
    if frontend["spec_files"]:
        lines.append(f"前端：{len(frontend['changed_files'])} 个改动文件，按 import 依赖图选出 {len(frontend['spec_files'])} 个用例：")
        lines.extend(f"  - {spec}（{frontend['specs'][spec]['reason']}）" for spec in frontend["spec_files"])
    for spec, reason in frontend.get("excluded_specs", {}).items():
        lines.append(f"前端用例 {spec} 不跑：{reason}")
    for suite in manifest["python"]:
        lines.append(f"Python：{' '.join(suite['argv'])}（{suite['reason']}；涉及 {len(suite['files'])} 个文件）")
    for command in manifest["shell"]["commands"]:
        lines.append(f"部署脚本：{' '.join(command)}（{manifest['shell']['reason']}）")
    if any(manifest["shell"]["requires_docker"]):
        lines.append("  其中调用 docker 的命令在本机没有 Docker 时记为未执行，由 U-4／CI 补齐")
    if manifest["no_test_needed"]:
        lines.append(f"不需要测试的文件 {len(manifest['no_test_needed'])} 个：")
        lines.extend(f"  - {item['path']}：{item['reason']}" for item in manifest["no_test_needed"])
    if manifest["unresolved"]:
        lines.append(f"【须补足验证】归不了类的文件 {len(manifest['unresolved'])} 个，快线不能证明它们被测过：")
        lines.extend(f"  - {item['path']}：{item['reason']}" for item in manifest["unresolved"])
    return "\n".join(lines) + "\n"


def _checklist_inline(context: FastContext) -> Callable[[], tuple[int, str]]:
    def run() -> tuple[int, str]:
        return 0, render_checklist(context.manifest)

    return run


def _worktree_inline(context: FastContext) -> Callable[[], tuple[int, str]]:
    def run() -> tuple[int, str]:
        dirty = status_paths(context.repository_root)
        context.records["worktree_after_run"] = dirty
        if dirty:
            return 1, "检查结束后候选树被弄脏（与 U-4 同一条规则）：" + "、".join(dirty[:20]) + "\n"
        return 0, "检查结束后候选树仍然干净\n"

    return run


def _inline_step(name: str, label: str, inline: Callable[[], tuple[int, str]]) -> Step:
    return Step(name=name, argv=("(本进程内执行)", label), inline=inline)


def _targeted_steps(context: FastContext) -> list[Step]:
    manifest = context.manifest
    go = manifest["go"]
    steps = [_inline_step("targeted-checklist", "定向回归清单", _checklist_inline(context))]
    if go["fallback_full_go"]:
        for tag in BUILD_TAGS:
            base = _go_test_step(tag)
            # 快线不用 -json 事件流：归因直接读 go test 的文本输出。
            steps.append(replace(base, go_json_name=None, attribution=ATTRIBUTION_GO_TEST))
    else:
        for label, tags in GO_TAG_SETS:
            packages = go["tag_sets"][label]["packages"]
            if packages:
                steps.append(
                    Step(
                        name=f"go-test-{label}-targeted",
                        argv=tuple(go_test_argv(tags, packages)),
                        cwd="backend",
                        attribution=ATTRIBUTION_GO_TEST,
                    )
                )
    frontend = manifest["frontend"]
    if frontend["spec_files"]:
        steps.append(Step("frontend-vitest-specs", tuple(frontend_vitest_argv(frontend["spec_files"]))))
    for index, suite in enumerate(manifest["python"], start=1):
        steps.append(Step(f"python-tests-{index}", tuple(suite["argv"]), cwd=suite["cwd"]))
    for index, (command, needs_docker) in enumerate(zip(manifest["shell"]["commands"], manifest["shell"]["requires_docker"]), start=1):
        # 脚本真的调用 docker 的（simple-mode 环境检查）本机没有 Docker 时记为未执行，由 U-4／CI 补齐，与 integration 同一规则。
        steps.append(Step(f"deploy-script-{index}", tuple(command), requires_docker=needs_docker))
    return steps


def build_fast_lanes(context: FastContext) -> list[Lane]:
    """快线的检查线，顺序即执行顺序。"""

    return [
        Lane("toolchain", (_inline_step("toolchain-check", "toolchain-check --role candidate", _toolchain_inline(context)),)),
        Lane("secret-scan", (_inline_step("candidate-secret-scan", "candidate-secret-scan", _secret_scan_inline(context)),)),
        Lane("lint", tuple(_lint_step(tag) for tag in BUILD_TAGS)),
        Lane(
            "frontend",
            (
                Step("frontend-lint", ("pnpm", "--dir", "frontend", "run", "lint:check")),
                Step("frontend-typecheck", ("pnpm", "--dir", "frontend", "run", "typecheck")),
                Step("frontend-critical-vitest", ("make", "test-frontend-critical")),
            ),
        ),
        Lane("targeted-regression", tuple(_targeted_steps(context))),
        Lane(
            "egress-spec",
            (
                Step(
                    "check-egress-spec",
                    ("make", "-k", f"CODEX_0_149_1_SOURCE_ROOT={context.codex_source_root}", "check-egress-spec"),
                    unit_executor=True,
                    unit_summary_subdir="check-egress-spec",
                    attribution=ATTRIBUTION_EGRESS_SPEC,
                ),
            ),
        ),
        Lane("worktree", (_inline_step("clean-after-run", "候选树仍干净", _worktree_inline(context)),)),
    ]


# ---------------------------------------------------------------------------
# 结论与状态文件
# ---------------------------------------------------------------------------


def classification_of(result: StepResult) -> str | None:
    """失败步骤的归因；通过与未执行的步骤没有归因。没有明确证明的失败一律是功能失败。"""

    if result.status != "failed":
        return None
    return result.classification if result.classification in CLASSIFICATIONS else CLASS_FUNCTIONAL


def fast_result(results: Sequence[StepResult]) -> str:
    """failed：有功能失败（含归因不清的）；passed_pending_ledger：失败项只有冻结摘要类；否则 passed。"""

    classes = [classification_of(item) for item in results if item.status == "failed"]
    if any(item != CLASS_FROZEN_PENDING for item in classes):
        return RESULT_FAILED
    if classes:
        return RESULT_PENDING
    return RESULT_PASSED


def fast_exit_code(result: str) -> int:
    """passed 与 passed_pending_ledger 退出 0（待台账闭合不阻断），failed 非 0。"""

    return 1 if result == RESULT_FAILED else 0


def _lane_seconds(results: Sequence[StepResult]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for item in results:
        totals[item.lane] = round(totals.get(item.lane, 0.0) + item.duration_seconds, 1)
    return totals


def timing_comparison(results: Sequence[StepResult], elapsed: float, compare_with: Path | None) -> dict[str, Any]:
    """快线与原全量的耗时对比字段：全量的状态文件由 ``--compare-with`` 给出（同一候选、同一环境下另跑一次 upstream-gate-full）。"""

    fast_lanes = _lane_seconds(results)
    document: dict[str, Any] = {
        "full_status_file": str(compare_with) if compare_with else None,
        "fast_elapsed_seconds": elapsed,
        "full_elapsed_seconds": None,
        "saved_seconds": None,
        "lane_seconds": {"fast": fast_lanes, "full": None},
        "skipped_lanes": None,
        "added_lanes": None,
        "targeted_regression_seconds": fast_lanes.get("targeted-regression"),
        "cache_notes": list(CACHE_NOTES),
    }
    if compare_with is None:
        return document
    full = expect_object(load_json(compare_with, "对比用的全量状态文件"), "对比用的全量状态文件")
    if full.get("schema_version") != STATUS_SCHEMA or full.get("mode") != "full":
        raise UpstreamMergeError("--compare-with 必须是 upstream-gate-full 的状态文件（full 模式）")
    full_results = [
        StepResult(
            str(step["lane"]), str(step["step"]), str(step["status"]), step["exit_code"], float(step["duration_seconds"])
        )
        for step in full["steps"]
    ]
    full_lanes = _lane_seconds(full_results)
    full_elapsed = float(full["elapsed_seconds"])
    document.update(
        full_elapsed_seconds=full_elapsed,
        saved_seconds=round(full_elapsed - elapsed, 1),
        skipped_lanes=sorted(set(full_lanes) - set(fast_lanes)),
        added_lanes=sorted(set(fast_lanes) - set(full_lanes)),
    )
    document["lane_seconds"]["full"] = full_lanes
    return document


def fast_status_document(
    context: FastContext,
    results: Sequence[StepResult],
    *,
    started_at: str,
    finished_at: str,
    elapsed: float,
) -> dict[str, Any]:
    """快线的机器可读结果：实际执行项、逐项结论与归因、日志位置、耗时，以及绑定的清单摘要。"""

    verdict = fast_result(results)
    steps = []
    for item in results:
        steps.append(
            {
                "lane": item.lane,
                "step": item.step,
                "status": item.status,
                "exit_code": item.exit_code,
                "duration_seconds": item.duration_seconds,
                "reason": item.reason,
                "classification": classification_of(item),
                "classification_reason": item.classification_reason
                if item.status == "failed"
                else None,
                "log": {"path": item.log_path, "start": item.log_range[0], "end": item.log_range[1]}
                if item.log_path and item.log_range
                else None,
                "detail": item.detail,
            }
        )
    manifest = context.manifest
    return {
        "schema_version": FAST_STATUS_SCHEMA,
        "mode": "fast",
        "not_u4_receipt": True,
        "notice": NOT_U4_NOTICE,
        "repository": {"root": str(context.repository_root), "commit": context.commit, "tree": context.tree},
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "elapsed_seconds": elapsed,
        "jobs": 1,
        "log_dir": str(context.log_dir),
        "targeted": {
            "path": str(context.manifest_path),
            "sha256": context.manifest_sha256,
            "identity_sha256": manifest["identity_sha256"],
            "candidate_commit": manifest["inputs"]["candidate_commit"],
            "fallback_full_go": manifest["go"]["fallback_full_go"],
            "fallback_reasons": manifest["go"]["fallback_reasons"],
            "supplement_required": manifest["supplement_required"],
            "supplement_reasons": manifest["supplement_reasons"],
            "summary": manifest["summary"],
        },
        "request": {"path": str(context.request_path), "sha256": context.request_sha256},
        "environment": {
            "toolchain": context.records.get("toolchain"),
            "secret_scan": context.records.get("secret_scan"),
            "worktree_after_run": context.records.get("worktree_after_run"),
        },
        "steps": steps,
        "executed": [item.key for item in results if item.status in ("passed", "failed")],
        "failed_functional": [item.key for item in results if classification_of(item) == CLASS_FUNCTIONAL],
        "pending_ledger": [item.key for item in results if classification_of(item) == CLASS_FROZEN_PENDING],
        "not_executed": [item.key for item in results if item.status == "not_executed"],
        "lane_seconds": _lane_seconds(results),
        "timing_comparison": timing_comparison(results, elapsed, context.compare_with),
        "result": verdict,
    }


def write_fast_status(path: Path, document: dict[str, Any]) -> str:
    """写入状态文件（已存在即拒绝），返回文件的 sha256。"""

    raw = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with open(path, "xb") as handle:
        handle.write(raw)
    return file_binding(path)["sha256"]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _label(item: StepResult) -> str:
    cls = classification_of(item)
    if cls == CLASS_FROZEN_PENDING:
        return f"失败/{PENDING_LEDGER_LABEL}"
    if cls == CLASS_FUNCTIONAL:
        return "失败/功能"
    return item.status


def render_fast_summary(results: Sequence[StepResult], *, jobs: int, elapsed: float) -> str:
    lines = [
        "",
        f"===== 试验区快线汇总（总耗时 {elapsed} 秒）=====",
        f"{'检查线':<20}{'步骤':<30}{'结果':<22}{'耗时(秒)':>10}",
    ]
    for item in results:
        lines.append(f"{item.lane:<20}{item.step:<30}{_label(item):<22}{item.duration_seconds:>10}")
    functional = [item.key for item in results if classification_of(item) == CLASS_FUNCTIONAL]
    pending = [item.key for item in results if classification_of(item) == CLASS_FROZEN_PENDING]
    skipped = [item.key for item in results if item.status == "not_executed"]
    if functional:
        lines.append(f"结论：failed（功能失败 {len(functional)} 项，阻断，不得建正式 Plan）：{', '.join(functional)}")
    elif pending:
        lines.append(f"结论：passed_pending_ledger——{PENDING_LEDGER_LABEL}，不阻断快线")
    else:
        lines.append("结论：passed")
    if pending:
        lines.append(
            f"【{PENDING_LEDGER_LABEL}】冻结摘要类失败 {len(pending)} 项：{', '.join(pending)}；"
            "试验区还没生成台账，台账在正式 Plan 的最终 revision 由工具一次性生成，U-4 前再做定向冻结检查"
        )
    if skipped:
        lines.append(f"未执行 {len(skipped)} 项：{', '.join(skipped)}（须由 U-4／CI 补齐）")
    return "\n".join(lines) + "\n"


def render_fast_progress(lane: Lane, results: Sequence[StepResult], *, done: int, total: int, elapsed: float) -> str:
    functional = [item.step for item in results if classification_of(item) == CLASS_FUNCTIONAL]
    pending = [item.step for item in results if classification_of(item) == CLASS_FROZEN_PENDING]
    skipped = [item.step for item in results if item.status == "not_executed"]
    if functional:
        verdict = f"功能失败 {len(functional)} 项（{'、'.join(functional)}）"
    elif pending:
        verdict = f"{PENDING_LEDGER_LABEL} {len(pending)} 项（{'、'.join(pending)}），其余通过"
    elif skipped:
        verdict = f"已执行的通过，未执行 {len(skipped)} 项（{'、'.join(skipped)}）"
    else:
        verdict = "全部通过"
    duration = sum(item.duration_seconds for item in results)
    return f"[进度] 检查线 {lane.name} 结束：{verdict}，本线 {duration:.1f} 秒；已结束 {done}/{total} 条，累计 {elapsed:.0f} 秒\n"


def render_closing(context: FastContext, document: dict[str, Any], status_sha256: str) -> str:
    lines = [
        f"快线结论：{document['result']}" + (f"（{PENDING_LEDGER_LABEL}）" if document["result"] == RESULT_PENDING else ""),
        NOT_U4_NOTICE,
        f"状态文件：{context.status_file}（sha256 {status_sha256}）",
        f"日志目录：{context.log_dir}",
        f"定向回归清单：{context.manifest_path}（sha256 {context.manifest_sha256}）",
    ]
    if document["targeted"]["supplement_required"]:
        lines.append(
            "【须补足验证】定向回归清单里有归不了类的文件，快线不能证明它们被测过；补足验证并在汇报里写明之前，不要把本次快线当作建 Plan 的依据："
        )
        lines.extend(f"  - {item}" for item in document["targeted"]["supplement_reasons"])
    comparison = document["timing_comparison"]
    if comparison["full_elapsed_seconds"] is not None:
        lines.append(
            f"耗时对比：快线 {comparison['fast_elapsed_seconds']} 秒，原全量 {comparison['full_elapsed_seconds']} 秒，"
            f"节省 {comparison['saved_seconds']} 秒（定向回归线 {comparison['targeted_regression_seconds']} 秒）"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_fast(
    context: FastContext,
    *,
    lanes: Sequence[Lane] | None = None,
    stream=None,
    integration: tuple[bool, str | None] | None = None,
) -> tuple[int, dict[str, Any]]:
    """执行快线并写状态文件，返回 (退出码, 状态文档)。

    ``lanes`` 供测试注入合成检查线；``integration`` 供测试指定 integration 步骤是否执行。
    快线检查线一律串行（jobs=1）：满载并行会让计时敏感用例误判，与 full 模式同一原则。
    """

    stream = stream if stream is not None else sys.stdout.buffer
    if integration is None:
        integration = resolve_integration(os.environ.get("UPSTREAM_GATE_INTEGRATION", "auto").strip() or "auto")
    run_docker_steps, skip_reason = integration
    chosen = list(lanes) if lanes is not None else build_fast_lanes(context)
    started_at = _now()
    results, elapsed = run_lanes(
        chosen,
        jobs=1,
        repository_root=context.repository_root,
        log_dir=context.log_dir,
        stream=stream,
        run_docker_steps=run_docker_steps,
        docker_skip_reason=skip_reason,
        mode="fast",
        render=render_fast_summary,
        progress=render_fast_progress,
    )
    document = fast_status_document(context, results, started_at=started_at, finished_at=_now(), elapsed=elapsed)
    sha = write_fast_status(context.status_file, document)
    stream.write(render_closing(context, document, sha).encode("utf-8"))
    stream.flush()
    return fast_exit_code(document["result"]), document

