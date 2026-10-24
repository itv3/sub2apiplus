"""U-4 启动前检查、失败项处置与结论读取。

v0.2.17 合并时，U-4 第一轮收据里 ``10-secret-scan`` 已失败，监视器只看进度行没读收据，又为修 CI lint 启动了
第二轮全量，白跑 47 分钟。本模块把“已知问题先处置、再开跑”做成工具约束，而不是靠人记得：

* ``run_gates_with_prechecks``：``gates-run`` 在创建 attempt 目录、启动任何执行组之前，先做三项检查并一次性列出
  全部阻断原因——上一 attempt 的失败项必须已有处置记录、候选工作树的工具链版本必须与它自己的声明一致、
  候选密钥扫描（与 U-4 同一份门禁）必须通过。任何一项不过都不建 attempt。
* ``record_failure_disposition``：逐项记录上一 attempt 每个失败项的原因、修复动作、处置类别和定向复验。定向复验
  由工具在当前候选 worktree 里真实执行并记录退出码、耗时、日志绑定，非 0 即处置不成立；不接受手填的“已通过”。
* ``attempt_status``／``u4_result_line``：监视逻辑读收据结论，不看进度行；``gates-run`` 结束与 ``gates-status``
  输出同一格式的固定一行，退出码与结论一致。

本模块依赖 ``workflow``（单向），``workflow`` 不引用本模块。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import (
    artifact_binding,
    expect_exact_fields,
    expect_object,
    expect_safe_id,
    expect_string,
    file_binding,
    load_json,
    open_once,
    resolve_within,
    safe_relative_path,
    validate_artifact_binding,
    validate_identity,
    validate_string_enum,
    write_json_once,
)
from .contracts import LoadedPlan, artifact_document, stage_binding
from .errors import UpstreamMergeError
from .gitops import assert_clean, rev_parse, run_git
from .toolchain import Probe, check_toolchain, describe_mismatches, run_probe
from .workflow import (
    ATTEMPT_RUN_MARKER,
    VERIFICATION_RECEIPT_SCHEMA,
    _gate_cwd,
    _gate_groups,
    _load_runner_status,
    _load_source_candidate,
    _stage_document,
    _validated_linked_worktree,
    load_verification_receipt,
    run_verification_gates,
    scan_candidate_secrets,
)

FAILURE_DISPOSITION_SCHEMA = "official-egress-upstream-failure-disposition/v1"
FAILURE_DISPOSITION_INPUT_SCHEMA = "official-egress-upstream-failure-disposition-input/v1"
# 处置类别：功能失败（含归因不清的）、冻结摘要类失败、环境／工具类失败。
DISPOSITION_CATEGORIES = ("environment", "frozen_digest", "functional")
DEFAULT_CATEGORY = "functional"
DISPOSITIONS_DIRECTORY = "dispositions"
RETEST_TAIL_BYTES = 2000
DEFAULT_RETEST_TIMEOUT_SECONDS = 3600
MAX_RETEST_TIMEOUT_SECONDS = 4 * 3600
MAINTENANCE_DIRECTORY = "docs/egress/maintenance"
# 冻结复验的判据：Go 冻结测试的 -run 选择器里含 Frozen，或跑 make 的出站规格检查目标。
FROZEN_RUN_MARKER = "Frozen"
FROZEN_MAKE_TARGETS = ("check-egress-spec", "check-egress-spec-ci")
WORKTREE_FAILURE_ID = "worktree-status"
U4_RESULT_PREFIX = "U4-RESULT"
# gates-status 与 gates-run 结论一致的状态集合；running／incomplete 只出现在没有收据时。
UNFINISHED_RESULTS = ("running", "incomplete")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# attempt 与收据的读取
# ---------------------------------------------------------------------------


def _attempts_root(plan: LoadedPlan) -> Path | None:
    """attempt 根目录；还没有任何 attempt 时不存在，返回 None。"""

    relative = plan.output_relative("gate_attempts_root")
    raw = plan.evidence_root / PurePosixPath(relative)
    if raw.is_symlink():
        raise UpstreamMergeError(f"attempt 根目录不得是符号链接：{raw}")
    if not raw.exists():
        return None
    return resolve_within(plan.evidence_root, relative, "gate attempts root")


def _dispositions_root(plan: LoadedPlan) -> Path:
    """处置记录目录：与 attempt 根目录同级的 ``dispositions``，不进入 plan 的 outputs 布局。"""

    parent = PurePosixPath(plan.output_relative("gate_attempts_root")).parent
    relative = (parent / DISPOSITIONS_DIRECTORY).as_posix()
    return resolve_within(plan.evidence_root, relative, "failure dispositions root")


def read_attempt_receipt(plan: LoadedPlan, receipt_path: Path) -> dict[str, Any]:
    """读取 attempt 收据并只做自摘要与身份校验。

    不做 ``load_verification_receipt`` 那样的“绑定当前 SourceCandidate”校验：源码 revision 推进之后，旧 attempt
    的收据按设计不再能被严格加载，但失败原因仍要能读出来供处置。
    """

    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise UpstreamMergeError(f"attempt 收据不是可信普通文件：{receipt_path}")
    document = expect_object(load_json(receipt_path, "VerificationReceipt"), "VerificationReceipt")
    if document.get("schema_version") != VERIFICATION_RECEIPT_SCHEMA:
        raise UpstreamMergeError("VerificationReceipt schema_version 非法")
    validate_identity(document, "VerificationReceipt")
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError("VerificationReceipt 计划身份不一致")
    if str(document.get("attempt_id")) != receipt_path.parent.name:
        raise UpstreamMergeError("VerificationReceipt attempt_id 与目录名不一致")
    return document


def _receipt_source_commit(plan: LoadedPlan, document: dict[str, Any]) -> str:
    """收据绑定的 SourceCandidate 提交：沿绑定读取当时的阶段制品（旧 revision 文件追加式保留）。"""

    binding = validate_artifact_binding(plan.evidence_root, document.get("source_candidate"), "收据 source_candidate")
    path = resolve_within(plan.evidence_root, binding["path"], "收据 source_candidate")
    candidate = expect_object(load_json(path, "SourceCandidate"), "SourceCandidate")
    validate_identity(candidate, "SourceCandidate")
    return expect_string(candidate.get("source_commit"), "SourceCandidate.source_commit")


def failure_items(plan: LoadedPlan, document: dict[str, Any]) -> list[dict[str, Any]]:
    """一份 attempt 收据里需要处置的失败项，按 id 排序。

    粒度：失败的执行组，若其 runner-status 记录了失败步骤（如 ``egress-spec/check-egress-spec``），细化到
    每个失败步骤，id 为 ``<执行组>/<检查线>/<步骤>``；没有步骤级信息（编排脚本崩溃、不用编排脚本的组）
    则整个执行组算一项，id 就是组名。执行后污染候选树（``worktree_status_paths``）另算一项。
    未执行（awaiting_ci）的项不是失败，不在此列。
    """

    by_id = {item["id"]: item for item in document.get("gates", []) if isinstance(item, dict) and "id" in item}
    items: list[dict[str, Any]] = []
    for group_name, members in _gate_groups(plan).items():
        results = [by_id[member["id"]] for member in members if member["id"] in by_id]
        if not any(result.get("status") != "passed" for result in results):
            continue
        gate_ids = sorted(member["id"] for member in members)
        leader = next((r for r in results if r.get("execution_status") == "executed"), results[0])
        steps: list[str] = []
        binding = leader.get("runner_status")
        if binding is not None:
            status = _load_runner_status(
                plan,
                binding,
                f"失败 attempt 的 {group_name} 状态文件",
                exit_code=leader["exit_code"],
            )
            steps = list(status["failed"])
        if steps:
            for key in steps:
                items.append(
                    {"id": f"{group_name}/{key}", "kind": "group_step", "gate_group": group_name, "gate_ids": gate_ids, "runner_step": key}
                )
        else:
            items.append({"id": group_name, "kind": "group", "gate_group": group_name, "gate_ids": gate_ids, "runner_step": None})
    dirty = document.get("worktree_status_paths") or []
    if dirty:
        items.append(
            {
                "id": WORKTREE_FAILURE_ID,
                "kind": "worktree",
                "gate_group": None,
                "gate_ids": [],
                "runner_step": None,
                "paths": list(dirty),
            }
        )
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise UpstreamMergeError(f"失败项 id 重复：{sorted(ids)}")
    return sorted(items, key=lambda item: item["id"])


def _attempt_directories(plan: LoadedPlan) -> list[Path]:
    root = _attempts_root(plan)
    if root is None:
        return []
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and not path.is_symlink()),
        key=lambda path: path.name,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_marker(attempt_directory: Path) -> dict[str, Any] | None:
    marker = attempt_directory / ATTEMPT_RUN_MARKER
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        document = load_json(marker, "attempt 运行标记")
    except UpstreamMergeError:
        return None
    return document if isinstance(document, dict) else None


def latest_receipt_attempt(plan: LoadedPlan) -> tuple[Path | None, list[Path]]:
    """最近一个有 receipt.json 的 attempt 目录（按收据文件的修改时间），以及没有收据的 attempt 目录清单。"""

    with_receipt: list[tuple[int, str, Path]] = []
    without: list[Path] = []
    for directory in _attempt_directories(plan):
        receipt = directory / "receipt.json"
        if receipt.is_file() and not receipt.is_symlink():
            with_receipt.append((receipt.stat().st_mtime_ns, directory.name, directory))
        else:
            without.append(directory)
    latest = max(with_receipt)[2] if with_receipt else None
    return latest, without


# ---------------------------------------------------------------------------
# 处置记录：读取与校验
# ---------------------------------------------------------------------------

_DISPOSITION_FIELDS = {
    "disposition_id",
    "failed_attempt",
    "source_candidate",
    "source_commit",
    "input",
    "items",
    "covered_failure_ids",
    "toolchain",
    "issued_at_utc",
    "result",
}
_DISPOSITION_ITEM_FIELDS = {
    "failure_id",
    "kind",
    "gate_group",
    "gate_ids",
    "runner_step",
    "category",
    "cause",
    "fix",
    "category_checks",
    "retests",
    "status",
}
_RETEST_FIELDS = {
    "argv",
    "cwd",
    "timeout_seconds",
    "exit_code",
    "timed_out",
    "duration_ms",
    "started_at_utc",
    "status",
    "stdout",
    "stderr",
    "stdout_tail",
    "stderr_tail",
}


def _disposition_files(plan: LoadedPlan, attempt_id: str) -> list[Path]:
    directory = _dispositions_root(plan) / attempt_id
    if not directory.is_dir() or directory.is_symlink():
        return []
    return sorted(
        path for path in directory.iterdir() if re.fullmatch(r"disposition-[0-9]{3}\.json", path.name) and path.is_file()
    )


def validate_disposition(plan: LoadedPlan, path: Path, *, expected_items: Sequence[dict[str, Any]], current_commit: str) -> dict[str, Any]:
    """复核一份处置记录：自摘要、计划与失败 attempt 的绑定、复验日志的绑定、覆盖范围、当前候选提交。

    任何不一致都抛 ``UpstreamMergeError``；通过时返回文档。
    """

    document = artifact_document(path, "FailureDisposition", FAILURE_DISPOSITION_SCHEMA, {"plan_id", "plan_identity_sha256"} | _DISPOSITION_FIELDS)
    if document.get("plan_id") != plan.plan_id or document.get("plan_identity_sha256") != plan.identity:
        raise UpstreamMergeError("处置记录的计划身份不一致")
    if document.get("result") != "closed":
        raise UpstreamMergeError("处置记录 result 不是 closed")
    failed = expect_object(document.get("failed_attempt"), "failed_attempt")
    expect_exact_fields(failed, {"attempt_id", "receipt", "result", "failed_gate_ids", "source_commit"}, "failed_attempt")
    receipt_binding = validate_artifact_binding(plan.evidence_root, failed.get("receipt"), "failed_attempt.receipt")
    receipt_path = resolve_within(plan.evidence_root, receipt_binding["path"], "failed_attempt.receipt")
    if receipt_path.parent.name != failed.get("attempt_id") or path.parent.name != failed.get("attempt_id"):
        raise UpstreamMergeError("处置记录的 attempt 标识与路径不一致")
    # 当前候选提交：处置只对写下它时的候选有效，候选再推进就要重新处置。
    if document.get("source_commit") != current_commit:
        raise UpstreamMergeError(f"处置记录绑定的候选提交 {document.get('source_commit')} 不是当前候选提交 {current_commit}")
    if document.get("source_candidate") != stage_binding(plan, "source_candidate"):
        raise UpstreamMergeError("处置记录的 SourceCandidate 绑定漂移")
    items = document.get("items")
    if not isinstance(items, list) or not items:
        raise UpstreamMergeError("处置记录 items 必须是非空数组")
    covered: list[str] = []
    for index, raw in enumerate(items):
        item = expect_object(raw, f"items[{index}]")
        expect_exact_fields(item, _DISPOSITION_ITEM_FIELDS, f"items[{index}]")
        covered.append(expect_string(item.get("failure_id"), f"items[{index}].failure_id"))
        category = validate_string_enum(item.get("category"), DISPOSITION_CATEGORIES, f"items[{index}].category")
        if category == "functional" and failed.get("source_commit") == document.get("source_commit"):
            raise UpstreamMergeError(f"功能失败项 {item['failure_id']} 的处置要求候选提交已在失败 attempt 之后推进")
        expect_string(item.get("cause"), f"items[{index}].cause")
        expect_string(item.get("fix"), f"items[{index}].fix")
        retests = item.get("retests")
        if not isinstance(retests, list) or not retests:
            raise UpstreamMergeError(f"失败项 {item['failure_id']} 没有定向复验")
        for number, retest_raw in enumerate(retests):
            retest = expect_object(retest_raw, f"items[{index}].retests[{number}]")
            expect_exact_fields(retest, _RETEST_FIELDS, f"items[{index}].retests[{number}]")
            if retest.get("exit_code") != 0 or retest.get("status") != "passed" or retest.get("timed_out"):
                raise UpstreamMergeError(f"失败项 {item['failure_id']} 的定向复验没有通过")
            validate_artifact_binding(plan.evidence_root, retest.get("stdout"), "retest.stdout")
            validate_artifact_binding(plan.evidence_root, retest.get("stderr"), "retest.stderr")
        if item.get("status") != "closed":
            raise UpstreamMergeError(f"失败项 {item['failure_id']} 的状态不是 closed")
    if covered != sorted(set(covered)) or document.get("covered_failure_ids") != covered:
        raise UpstreamMergeError("处置记录 covered_failure_ids 与逐项内容不一致")
    missing = sorted({item["id"] for item in expected_items} - set(covered))
    if missing:
        raise UpstreamMergeError(f"处置记录没有覆盖失败项：{missing}")
    return document


def check_failure_disposition(plan: LoadedPlan) -> dict[str, Any]:
    """上一轮权威收据的失败项是否都已处置：返回 ``blockers``（未处置的失败项逐项列出）与 ``warnings``。

    “上一轮”指最近一个有 ``receipt.json`` 的 attempt。它的 result 是 ``blocked`` 时，每个失败项都必须在一份
    绑定当前 SourceCandidate 提交、复验日志可复核的处置记录里；``passed`` 与 ``awaiting_ci`` 不需要处置
    （等 CI 补齐的未执行项和真实失败分开处理）。只有目录、没有收据的 attempt（被杀或崩溃）不算已知失败，
    仅在 ``warnings`` 里提示。
    """

    latest, without = latest_receipt_attempt(plan)
    warnings: list[str] = []
    for directory in without:
        marker = _read_marker(directory)
        pid = marker.get("pid") if marker else None
        if isinstance(pid, int) and _pid_alive(pid):
            warnings.append(f"attempt {directory.name} 没有 receipt.json，运行标记显示进程 {pid} 仍存活：请确认没有另一路在跑")
        else:
            warnings.append(
                f"attempt {directory.name} 只有目录、没有 receipt.json（运行被中断或崩溃），不算已知失败；"
                "它的部分输出保留供排查，重跑须换新的 attempt 标识"
            )
    report: dict[str, Any] = {
        "latest_attempt": latest.name if latest else None,
        "latest_result": None,
        "failure_ids": [],
        "blockers": [],
        "warnings": warnings,
    }
    if latest is None:
        return report
    document = read_attempt_receipt(plan, latest / "receipt.json")
    report["latest_result"] = document.get("result")
    if document.get("result") != "blocked":
        return report
    items = failure_items(plan, document)
    report["failure_ids"] = [item["id"] for item in items]
    current = _load_source_candidate(plan)["source_commit"]
    covered: set[str] = set()
    invalid: list[str] = []
    for path in _disposition_files(plan, latest.name):
        try:
            valid = validate_disposition(plan, path, expected_items=[], current_commit=current)
        except UpstreamMergeError as error:
            invalid.append(f"{path.name}：{error}")
            continue
        covered.update(valid["covered_failure_ids"])
    uncovered = [item for item in items if item["id"] not in covered]
    for item in uncovered:
        report["blockers"].append(
            f"上一轮 attempt {latest.name} 的失败项 {item['id']} 尚未处置（没有绑定当前候选提交 {current[:12]} 的处置记录）"
        )
    if uncovered:
        for entry in invalid:
            report["blockers"].append(f"已有处置记录无效，不能采信：{entry}")
        report["blockers"].append(
            "先用 failure-disposition 逐项记录失败原因、修复动作和定向复验（复验由工具在候选 worktree 真实执行），"
            f"再重新启动：failure-disposition --plan {plan.path} --attempt-id {latest.name} --input <处置输入.json>"
        )
    return report


# ---------------------------------------------------------------------------
# 处置记录：生成
# ---------------------------------------------------------------------------


def _parse_retests(raw: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise UpstreamMergeError(f"{label} 必须是非空数组：每个失败项至少一条定向复验命令")
    retests: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        item = expect_object(entry, f"{label}[{index}]")
        extra = set(item) - {"argv", "cwd", "timeout_seconds"}
        if extra or "argv" not in item:
            raise UpstreamMergeError(f"{label}[{index}] 只能含 argv、cwd、timeout_seconds（复验结果由工具执行并记录，不接受手填）")
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
            raise UpstreamMergeError(f"{label}[{index}].argv 必须是非空字符串数组")
        cwd = item.get("cwd", ".")
        if cwd != ".":
            safe_relative_path(cwd, f"{label}[{index}].cwd")
        timeout = item.get("timeout_seconds", DEFAULT_RETEST_TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_RETEST_TIMEOUT_SECONDS:
            raise UpstreamMergeError(f"{label}[{index}].timeout_seconds 必须是 1～{MAX_RETEST_TIMEOUT_SECONDS} 的整数")
        retests.append({"argv": list(argv), "cwd": cwd, "timeout_seconds": timeout})
    return retests


def _load_disposition_input(path: Path, expected_ids: Sequence[str]) -> list[dict[str, Any]]:
    document = expect_object(load_json(path, "处置输入"), "处置输入")
    expect_exact_fields(document, {"schema_version", "items"}, "处置输入")
    if document.get("schema_version") != FAILURE_DISPOSITION_INPUT_SCHEMA:
        raise UpstreamMergeError(f"处置输入 schema_version 必须是 {FAILURE_DISPOSITION_INPUT_SCHEMA}")
    raw_items = document.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise UpstreamMergeError("处置输入 items 必须是非空数组")
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        label = f"处置输入.items[{index}]"
        entry = expect_object(raw, label)
        extra = set(entry) - {"failure_id", "category", "cause", "fix", "retests"}
        required = {"failure_id", "cause", "fix", "retests"}
        if extra or required - set(entry):
            raise UpstreamMergeError(f"{label} 字段不闭合：缺失={sorted(required - set(entry))}，多余={sorted(extra)}")
        items.append(
            {
                "failure_id": expect_string(entry.get("failure_id"), f"{label}.failure_id"),
                # 归因不清的按功能失败处理：省略 category 即 functional。
                "category": validate_string_enum(entry.get("category", DEFAULT_CATEGORY), DISPOSITION_CATEGORIES, f"{label}.category"),
                "cause": expect_string(entry.get("cause"), f"{label}.cause"),
                "fix": expect_string(entry.get("fix"), f"{label}.fix"),
                "retests": _parse_retests(entry.get("retests"), f"{label}.retests"),
            }
        )
    ids = [item["failure_id"] for item in items]
    if len(ids) != len(set(ids)):
        raise UpstreamMergeError(f"处置输入的 failure_id 重复：{sorted(i for i in set(ids) if ids.count(i) > 1)}")
    missing = sorted(set(expected_ids) - set(ids))
    unknown = sorted(set(ids) - set(expected_ids))
    if missing or unknown:
        raise UpstreamMergeError(
            f"处置输入必须恰好覆盖失败 attempt 的全部失败项：缺失={missing}，不属于该 attempt={unknown}"
        )
    return sorted(items, key=lambda item: item["failure_id"])


def _is_frozen_retest(argv: Sequence[str]) -> bool:
    """冻结摘要类失败的定向复验：Go 冻结测试（-run 选择器含 Frozen），或 make 的出站规格检查目标。"""

    if argv and Path(argv[0]).name == "go" and "test" in argv:
        for index, part in enumerate(argv):
            if part in ("-run", "--run") and index + 1 < len(argv) and FROZEN_RUN_MARKER in argv[index + 1]:
                return True
            if part.startswith(("-run=", "--run=")) and FROZEN_RUN_MARKER in part:
                return True
    if argv and Path(argv[0]).name == "make":
        return any(part in FROZEN_MAKE_TARGETS for part in argv[1:])
    return False


def _maintenance_successors(worktree: Path, failed_commit: str, current_commit: str) -> list[str]:
    """失败 attempt 的候选提交到当前候选提交之间，新增或改动的 successor 台账收据（docs/egress/maintenance 下的 json）。"""

    listed = run_git(
        worktree, "diff", "--name-only", "--no-renames", f"{failed_commit}..{current_commit}", "--", MAINTENANCE_DIRECTORY
    ).stdout.split()
    found: list[str] = []
    for relative in listed:
        if not relative.endswith(".json"):
            continue
        shown = run_git(worktree, "show", f"{current_commit}:{relative}", check=False)
        if shown.returncode != 0:
            continue
        if re.search(r'"schema_version"\s*:\s*"[^"]*successor', shown.stdout):
            found.append(relative)
    return sorted(found)


def _run_retest(
    worktree: Path,
    retest: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    """在候选 worktree 里真实执行一条复验命令：禁用 shell，输出边跑边落盘，超时则杀掉整个进程组。"""

    cwd = _gate_cwd(worktree, retest["cwd"])
    started_at = _now_utc()
    started = time.monotonic()
    exit_code: int | None = None
    timed_out = False
    with open_once(stdout_path) as stdout, open_once(stderr_path) as stderr:
        try:
            process = subprocess.Popen(
                retest["argv"], cwd=cwd, stdout=stdout, stderr=stderr, env=env, start_new_session=True
            )
        except OSError as error:
            stderr.write(f"无法启动复验命令：{error}\n".encode("utf-8"))
        else:
            try:
                exit_code = process.wait(timeout=retest["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                stderr.write(f"复验命令超过 {retest['timeout_seconds']} 秒被终止\n".encode("utf-8"))
    duration_ms = int((time.monotonic() - started) * 1000)
    status = "passed" if exit_code == 0 and not timed_out else "failed"

    def tail(path: Path) -> str:
        return path.read_bytes()[-RETEST_TAIL_BYTES:].decode("utf-8", errors="replace")

    return {
        "argv": retest["argv"],
        "cwd": retest["cwd"],
        "timeout_seconds": retest["timeout_seconds"],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "started_at_utc": started_at,
        "status": status,
        "stdout_tail": tail(stdout_path),
        "stderr_tail": tail(stderr_path),
    }


def record_failure_disposition(
    plan: LoadedPlan,
    attempt_id: str,
    input_path: Path,
    *,
    probe: Probe = run_probe,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """为一个 blocked attempt 的全部失败项写处置记录（只写一次、不可覆盖），定向复验由工具真实执行。

    全部校验与复验通过才写 ``u4/dispositions/<attempt>/disposition-NNN.json``；任一复验非 0（或超时、无法启动）
    即抛错、不写记录，复验日志保留在同名目录供排查。处置类别的要求：

    * ``functional``（省略类别、归因不清的都按它处理）：当前 SourceCandidate 提交必须不同于失败 attempt 绑定的提交；
    * ``frozen_digest``：同上，且两提交之间新增或改动了 ``docs/egress/maintenance`` 下的 successor 台账收据，
      复验里至少一条是冻结测试（Go ``-run`` 含 Frozen）或 make 的出站规格检查；
    * ``environment``：当前候选 worktree 的工具链核对必须通过。
    """

    attempt = expect_safe_id(attempt_id, "attempt_id")
    attempts = _attempts_root(plan)
    if attempts is None:
        raise UpstreamMergeError("本 Plan 还没有任何 attempt")
    receipt_path = attempts / attempt / "receipt.json"
    document = read_attempt_receipt(plan, receipt_path)
    if document.get("result") != "blocked":
        raise UpstreamMergeError(f"attempt {attempt} 的结果是 {document.get('result')}，只有 blocked 的失败需要处置")
    expected = failure_items(plan, document)
    if not expected:
        raise UpstreamMergeError(f"attempt {attempt} 是 blocked 却没有可识别的失败项，收据不一致")
    entries = _load_disposition_input(input_path, [item["id"] for item in expected])
    expected_by_id = {item["id"]: item for item in expected}

    source = _load_source_candidate(plan)
    current_commit = source["source_commit"]
    failed_commit = _receipt_source_commit(plan, document)
    worktree = _validated_linked_worktree(plan, plan.worktree)
    if rev_parse(worktree, "HEAD^{commit}") != current_commit:
        raise UpstreamMergeError("候选 worktree 不在当前 SourceCandidate 提交上，不能执行定向复验")
    assert_clean(worktree, "候选 worktree")

    # 类别要求（复验之前先判，不满足就不浪费时间执行）
    toolchain_report: dict[str, Any] | None = None
    successors: list[str] | None = None
    for entry in entries:
        category, failure_id = entry["category"], entry["failure_id"]
        checks: dict[str, Any] = {"failed_source_commit": failed_commit, "current_source_commit": current_commit}
        if category in ("functional", "frozen_digest") and failed_commit == current_commit:
            raise UpstreamMergeError(
                f"失败项 {failure_id} 按{'冻结摘要' if category == 'frozen_digest' else '功能'}失败处置，"
                f"要求当前 SourceCandidate 提交已在 attempt {attempt} 之后推进（现仍是 {current_commit[:12]}）；"
                "先在同一 Plan 追加源码 revision 修复。若确属环境／工具问题而非代码问题，类别应填 environment"
            )
        if category == "frozen_digest":
            if successors is None:
                successors = _maintenance_successors(worktree, failed_commit, current_commit)
            if not successors:
                raise UpstreamMergeError(
                    f"失败项 {failure_id} 按冻结摘要类失败处置，要求当前候选已含台账 revision：{failed_commit[:12]}..{current_commit[:12]} "
                    f"之间没有新增或改动 {MAINTENANCE_DIRECTORY} 下的 successor 收据（先按现行流程生成并提交）"
                )
            if not any(_is_frozen_retest(retest["argv"]) for retest in entry["retests"]):
                raise UpstreamMergeError(
                    f"失败项 {failure_id} 按冻结摘要类失败处置，定向复验里必须有冻结测试"
                    "（go test -run 含 Frozen）或 make check-egress-spec／check-egress-spec-ci"
                )
            checks["successor_receipts"] = successors
        if category == "environment":
            if toolchain_report is None:
                toolchain_report = check_toolchain(worktree, "candidate", probe=probe, environ=environ)
            if toolchain_report["result"] != "passed":
                raise UpstreamMergeError(
                    f"失败项 {failure_id} 按环境／工具类失败处置，要求当前版本检查通过，但仍有不匹配："
                    + "；".join(describe_mismatches(toolchain_report))
                )
            checks["toolchain_result"] = toolchain_report["result"]
        entry["category_checks"] = checks

    # 分配序号并执行复验
    attempt_directory = _dispositions_root(plan) / attempt
    attempt_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if attempt_directory.is_symlink():
        raise UpstreamMergeError(f"处置目录不得是符号链接：{attempt_directory}")
    for private in (attempt_directory.parent, attempt_directory):
        private.chmod(0o700)
    numbers = [
        int(match.group(1))
        for child in attempt_directory.iterdir()
        if (match := re.fullmatch(r"disposition-([0-9]{3})(?:\.json)?", child.name))
    ]
    sequence = max(numbers, default=0) + 1
    disposition_id = f"disposition-{sequence:03d}"
    log_directory = attempt_directory / disposition_id
    log_directory.mkdir(mode=0o700)
    env = {**(os.environ if environ is None else environ), "PYTHONDONTWRITEBYTECODE": "1"}
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for item_index, entry in enumerate(entries, start=1):
        executed: list[dict[str, Any]] = []
        for retest_index, retest in enumerate(entry["retests"], start=1):
            stem = f"{item_index:02d}-{retest_index:02d}"
            outcome = _run_retest(worktree, retest, log_directory / f"{stem}.stdout.txt", log_directory / f"{stem}.stderr.txt", env)
            outcome["stdout"] = artifact_binding(plan.evidence_root, log_directory / f"{stem}.stdout.txt")
            outcome["stderr"] = artifact_binding(plan.evidence_root, log_directory / f"{stem}.stderr.txt")
            executed.append(outcome)
            if outcome["status"] != "passed":
                failures.append(
                    f"{entry['failure_id']}：{' '.join(retest['argv'])}（退出码 {outcome['exit_code']}"
                    f"{'，超时' if outcome['timed_out'] else ''}，日志 {log_directory / (stem + '.stderr.txt')}）"
                )
        results.append({**entry, "retests": executed})
    if failures:
        raise UpstreamMergeError(
            "定向复验未通过，处置不成立（没有写处置记录；日志保留在 "
            f"{log_directory}）：" + "；".join(failures)
        )
    assert_clean(worktree, "定向复验之后的候选 worktree")

    items_document = []
    for entry in results:
        planned = expected_by_id[entry["failure_id"]]
        items_document.append(
            {
                "failure_id": entry["failure_id"],
                "kind": planned["kind"],
                "gate_group": planned["gate_group"],
                "gate_ids": planned["gate_ids"],
                "runner_step": planned["runner_step"],
                "category": entry["category"],
                "cause": entry["cause"],
                "fix": entry["fix"],
                "category_checks": entry["category_checks"],
                "retests": entry["retests"],
                "status": "closed",
            }
        )
    disposition = _stage_document(
        plan,
        FAILURE_DISPOSITION_SCHEMA,
        {
            "disposition_id": disposition_id,
            "failed_attempt": {
                "attempt_id": attempt,
                "receipt": artifact_binding(plan.evidence_root, receipt_path),
                "result": "blocked",
                "failed_gate_ids": list(document.get("failed_gate_ids", [])),
                "source_commit": failed_commit,
            },
            "source_candidate": stage_binding(plan, "source_candidate"),
            "source_commit": current_commit,
            "input": file_binding(input_path),
            "items": items_document,
            "covered_failure_ids": [item["failure_id"] for item in items_document],
            "toolchain": toolchain_report,
            "issued_at_utc": _now_utc(),
            "result": "closed",
        },
    )
    output = attempt_directory / f"{disposition_id}.json"
    write_json_once(output, disposition)
    # 自检：新记录必须能被启动前检查接受。
    validate_disposition(plan, output, expected_items=expected, current_commit=current_commit)
    return {
        "result": "closed",
        "disposition": str(output),
        "disposition_id": disposition_id,
        "failed_attempt": attempt,
        "covered_failure_ids": disposition["covered_failure_ids"],
        "retest_count": sum(len(item["retests"]) for item in items_document),
        "source_commit": current_commit,
        "identity_sha256": disposition["identity_sha256"],
    }


# ---------------------------------------------------------------------------
# 启动前检查与 gates-run
# ---------------------------------------------------------------------------


def _stderr_notice(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run_gates_with_prechecks(
    plan: LoadedPlan,
    attempt_id: str,
    *,
    only: str | Sequence[str] | None = None,
    from_attempt: str | Path | None = None,
    probe: Probe = run_probe,
    environ: Mapping[str, str] | None = None,
    notify: Callable[[str], None] = _stderr_notice,
) -> dict[str, Any]:
    """``gates-run`` 的入口：先做启动前检查，全部通过才创建 attempt 并执行门禁。

    三项检查都在创建 attempt 目录、启动任何执行组之前完成，并把全部阻断原因一次列出（一次重跑发现全部问题）：

    1. 上一轮权威收据的失败项必须已有处置（含 ``--from-attempt`` 部分重跑）；
    2. 候选工作树的 Go 与 lint 版本必须与它自己的声明一致（``toolchain-check`` 的 candidate 角色）；
    3. 候选密钥扫描：与 U-4 同一份 ``secret_scan`` 门禁，对候选工作树先跑一遍。
    """

    attempt = expect_safe_id(attempt_id, "attempt_id")
    source = _load_source_candidate(plan)
    worktree = _validated_linked_worktree(plan, plan.worktree)
    if rev_parse(worktree, "HEAD^{commit}") != source["source_commit"]:
        raise UpstreamMergeError("U-4 门禁执行树与 SourceCandidate 不一致")
    assert_clean(worktree, "U-4 门禁执行树")
    blockers: list[str] = []

    disposition = check_failure_disposition(plan)
    for warning in disposition["warnings"]:
        notify(f"提示：{warning}")
    blockers.extend(disposition["blockers"])

    toolchain = check_toolchain(worktree, "candidate", probe=probe, environ=environ)
    if toolchain["result"] != "passed":
        blockers.append("候选工具链版本核对未通过：" + "；".join(describe_mismatches(toolchain)))

    scan = scan_candidate_secrets(worktree, plan.document["gates"])
    blockers.extend(scan["blockers"])

    if blockers:
        listed = "\n".join(f"  {index}. {text}" for index, text in enumerate(blockers, start=1))
        raise UpstreamMergeError(
            f"U-4 启动前检查未通过（共 {len(blockers)} 项，没有创建 attempt {attempt}，没有启动任何执行组）：\n{listed}"
        )
    return run_verification_gates(plan, attempt, only=only, from_attempt=from_attempt)


# ---------------------------------------------------------------------------
# 结论读取
# ---------------------------------------------------------------------------


def u4_result_line(attempt_id: str, result: str, failed: Sequence[str], receipt: Path | None) -> str:
    """固定格式的结论行；监视脚本据此判断，不看进度行。"""

    failed_text = ",".join(failed) if failed else "-"
    receipt_text = str(receipt) if receipt is not None else "-"
    return f"{U4_RESULT_PREFIX} attempt={attempt_id} result={result} failed={failed_text} receipt={receipt_text}"


def attempt_status(
    plan: LoadedPlan,
    attempt_id: str | None = None,
    *,
    alive: Callable[[int], bool] = _pid_alive,
) -> dict[str, Any]:
    """读取一个 attempt 的结论：有收据以收据为准，没有收据按运行标记区分 running 与 incomplete。

    不指定 attempt 时取目录最新的一个。``failed`` 是失败项 id（与 failure-disposition 用同一套 id）。
    """

    directories = _attempt_directories(plan)
    if attempt_id is not None:
        name = expect_safe_id(attempt_id, "attempt_id")
        directory = next((item for item in directories if item.name == name), None)
        if directory is None:
            raise UpstreamMergeError(f"找不到 attempt：{name}")
    else:
        if not directories:
            raise UpstreamMergeError("本 Plan 还没有任何 attempt")
        directory = max(directories, key=lambda item: (item.stat().st_mtime_ns, item.name))
    receipt = directory / "receipt.json"
    status: dict[str, Any] = {
        "attempt_id": directory.name,
        "receipt": None,
        "failed": [],
        "failed_gate_ids": [],
        "not_executed_checks": [],
        "verified": None,
        "verification_error": None,
        "pid": None,
    }
    if receipt.is_file() and not receipt.is_symlink() and receipt.stat().st_size > 0:
        document = read_attempt_receipt(plan, receipt)
        result = str(document.get("result"))
        if result not in ("passed", "blocked", "awaiting_ci"):
            raise UpstreamMergeError(f"收据 result 非法：{result}")
        status["receipt"] = str(receipt)
        status["failed_gate_ids"] = list(document.get("failed_gate_ids", []))
        status["not_executed_checks"] = list(document.get("not_executed_checks", []))
        if result == "blocked":
            status["failed"] = [item["id"] for item in failure_items(plan, document)]
        # 严格复核收据；旧 attempt 在源码 revision 推进后按设计不再能严格加载，那不改变它当时的结论。
        try:
            load_verification_receipt(plan, receipt, require_passed=False)
            status["verified"] = True
        except UpstreamMergeError as error:
            status["verified"] = False
            status["verification_error"] = str(error)
    else:
        marker = _read_marker(directory)
        pid = marker.get("pid") if marker else None
        status["pid"] = pid if isinstance(pid, int) else None
        result = "running" if isinstance(pid, int) and alive(pid) else "incomplete"
    status["result"] = result
    status["line"] = u4_result_line(directory.name, result, status["failed"], Path(status["receipt"]) if status["receipt"] else None)
    return status


def load_scan_gates(request_path: Path) -> list[dict[str, Any]]:
    """取出 request 里的门禁定义，不要求整份 request 已渲染完毕。

    ``request_path`` 可以是 ``request-render`` 生成的正式 request，也可以是标准模板
    ``request_template_v2.json``（门禁在其 ``request`` 字段里，仍含未渲染的 ``{source_repository}``）——试验
    worktree 在建正式 Plan 之前就要扫。这里只确认 gates 是对象数组；唯一的 ``secret_scan`` 门禁的形状、占位符与
    cwd 由扫描本身逐项校验，不合格即阻断。
    """

    document = expect_object(load_json(request_path, "request"), "request")
    gates = document.get("gates")
    if gates is None and isinstance(document.get("request"), dict):
        gates = document["request"].get("gates")
    if not isinstance(gates, list) or not gates or not all(isinstance(item, dict) for item in gates):
        raise UpstreamMergeError("request 缺少 gates 数组（或标准模板的 request.gates）")
    return gates


def candidate_secret_scan_from_request(request_path: Path, repository: Path) -> dict[str, Any]:
    """``candidate-secret-scan`` 的入口：门禁取自 request，对象是候选工作树。"""

    return scan_candidate_secrets(repository, load_scan_gates(request_path))
