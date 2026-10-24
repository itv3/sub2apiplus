"""开工检查：主干 HEAD 的功能 CI 结果必须是绿的，并绑定到当前 HEAD；基线工具链必须与主干声明一致。

v0.2.17 合并时，主干原有的 CI 红与工具链不一致在合并过程中才暴露，约 1 小时花在“这是合并引入的还是原本就红”
上。开工检查在 ``plan-create`` 之前、基线封存之前做，回答三件事：

1. 主仓库是干净的、在受维护分支上、HEAD 已经在 origin 上（HEAD 没推上去，CI 结果就无从谈起）；
2. 该提交上 ``backend-ci.yml`` 的最新一次运行（push 或 workflow_dispatch 事件）已完成且成功，必需作业齐全且逐个
   成功（非必需作业 success／skipped／neutral 可接受，failure／cancelled／timed_out 阻断）；状态缺失或仍在运行一律
   算未通过，不是“暂时没问题”；Security Scan 只作信息记录，不作硬前置；
3. 主干声明的 Go 与 lint 版本与本机真正会用到的版本一致（基线取主干声明的版本，不得误用候选的版本）。

联网查询（GitHub Actions、远端分支）只放在本检查里；现有预检保持离线。查询全部只读，不 fetch、不 push。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import bind_identity, write_json_once
from .errors import UpstreamMergeError
from .gitops import assert_git_repository, assert_private_path, commit_tree, current_branch_ref, rev_parse, run_git, status_paths
from .toolchain import CI_WORKFLOW_PATH, Probe, check_toolchain, describe_mismatches, run_probe
from .workflow import CI_REQUIRED_JOBS, GITHUB_SLUG_RE, GitHubActionsApi, _origin_slug

START_CHECK_SCHEMA = "official-egress-upstream-start-check/v1"
# 接受作为“主干 HEAD 功能 CI 结果”的事件：push，以及发版回写 VERSION 后由 release 工作流显式调起的 workflow_dispatch
# （回写提交由 GITHUB_TOKEN 推送，不会触发 push 事件，主干 HEAD 恰是这个提交时只有后者）。pull_request 的运行
# 绑定的是 PR 头提交而非合并结果，不算。
ACCEPTED_CI_EVENTS = ("push", "workflow_dispatch")
SECURITY_SCAN_WORKFLOW_SUFFIX = "security-scan.yml"
# 必需作业（workflow.CI_REQUIRED_JOBS）必须逐个 success；其余作业 success／skipped／neutral 可以接受
# （例如按条件跳过的可选作业），failure／cancelled／timed_out 等一律阻断。报告里照常列出全部作业的结论。
ACCEPTABLE_OPTIONAL_CONCLUSIONS = ("success", "skipped", "neutral")


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "run_attempt": run.get("run_attempt"),
        "event": run.get("event"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def _latest(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """最新的一次运行：按创建时间，其次 run id。"""

    return max(runs, key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0))) if runs else None


def _remote_head(root: Path, remote: str, managed_ref: str) -> tuple[str | None, str | None]:
    """远端受维护分支的当前提交（只读查询）；返回 (提交, 失败原因)。"""

    completed = run_git(root, "ls-remote", remote, managed_ref, check=False)
    if completed.returncode != 0:
        return None, (completed.stderr.strip() or completed.stdout.strip() or f"退出码 {completed.returncode}")[-300:]
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == managed_ref:
            return fields[0], None
    return None, f"{remote} 上没有 {managed_ref}"


def _ancestry(root: Path, ancestor: str, descendant: str) -> bool | None:
    """ancestor 是否为 descendant 的祖先；其中一个提交不在本地对象库里返回 None。"""

    completed = run_git(root, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def start_check(
    repository: Path,
    *,
    output: Path | None = None,
    managed_ref: str = "refs/heads/main",
    remote: str = "origin",
    repository_slug: str | None = None,
    api: Any | None = None,
    probe: Probe = run_probe,
    environ: Mapping[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """开工检查。返回报告；``result`` 为 ``ready`` 才可以开始基线封存与后续步骤，否则 ``blockers`` 逐条写明原因。

    报告绑定当前 HEAD 的提交与 tree；CI 结果是在该提交上查到的，``ci.bound_to_head`` 为真才是有效证据。
    ``output`` 给出时必须在主仓库之外（或 Git 忽略的 local-analysis/upstream），不覆盖既有文件。
    """

    root = assert_git_repository(repository)
    if output is not None:
        if not output.is_absolute():
            raise UpstreamMergeError("start-check 输出必须是绝对路径")
        assert_private_path(root, output, "start-check 报告")
        if output.exists() or output.is_symlink():
            raise UpstreamMergeError(f"start-check 报告输出已存在，禁止覆盖：{output}")
    clock = now if now is not None else (lambda: datetime.now(timezone.utc))
    blockers: list[str] = []

    dirty = status_paths(root)
    if dirty:
        blockers.append(f"主仓库工作树不是干净状态（{len(dirty)} 个路径有改动，例如 {dirty[0]}）")
    try:
        branch = current_branch_ref(root)
    except UpstreamMergeError:
        branch = None
    if branch != managed_ref:
        blockers.append(f"当前不在受维护分支 {managed_ref}（实际 {branch or '游离 HEAD'}）")
    head = rev_parse(root, "HEAD^{commit}")
    tree = commit_tree(root, head)

    # HEAD 必须已经在远端受维护分支上，且就是它的当前提交：本地领先、落后或分叉都得不到有效的 CI 结果。
    origin_commit, origin_error = _remote_head(root, remote, managed_ref)
    origin_matches = origin_commit == head
    if origin_error is not None:
        blockers.append(f"无法确认 HEAD 是否已在 {remote} 上：{origin_error}")
    elif not origin_matches:
        relation = ""
        assert origin_commit is not None
        if _ancestry(root, origin_commit, head):
            relation = "（本地领先，尚未推送）"
        elif _ancestry(root, head, origin_commit):
            relation = "（本地落后于远端）"
        blockers.append(
            f"HEAD {head[:12]} 不等于 {remote} 的 {managed_ref} {origin_commit[:12]}{relation}；"
            "CI 结果只对已推送的提交有意义，先让本地与远端一致"
        )

    ci: dict[str, Any] = {
        "workflow": CI_WORKFLOW_PATH,
        "events_accepted": list(ACCEPTED_CI_EVENTS),
        "status": "skipped",
        "run": None,
        "jobs": [],
        "required_jobs_missing": [],
        "required_jobs_failed": [],
        "optional_jobs_blocking": [],
        "required_jobs_success": False,
        "all_jobs_success": False,
        "bound_to_head": False,
    }
    security: dict[str, Any] = {"informational": True, "run": None}
    slug: str | None = repository_slug
    if origin_error is None and origin_matches:
        try:
            slug = repository_slug or _origin_slug(root, remote)
            if not GITHUB_SLUG_RE.fullmatch(slug):
                raise UpstreamMergeError(f"GitHub 仓库标识非法：{slug}")
            actions = api if api is not None else GitHubActionsApi(root)
            runs = actions.runs_for_commit(slug, head)
            ci_runs = [
                item
                for item in runs
                if item.get("path") == CI_WORKFLOW_PATH and item.get("event") in ACCEPTED_CI_EVENTS and item.get("head_sha") == head
            ]
            latest = _latest(ci_runs)
            scan_run = _latest(
                [item for item in runs if str(item.get("path", "")).endswith(SECURITY_SCAN_WORKFLOW_SUFFIX) and item.get("event") == "push"]
            )
            if scan_run is not None:
                security["run"] = _run_summary(scan_run)
            if latest is None:
                ci["status"] = "blocked"
                blockers.append(
                    f"找不到 HEAD {head[:12]} 上 {CI_WORKFLOW_PATH} 的 {'／'.join(ACCEPTED_CI_EVENTS)} 运行（状态缺失不算通过）"
                )
            else:
                ci["run"] = _run_summary(latest)
                ci["bound_to_head"] = latest.get("head_sha") == head
                if latest.get("status") != "completed":
                    ci["status"] = "blocked"
                    blockers.append(
                        f"HEAD 的最新 CI 运行 {latest.get('id')} 尚未结束（status={latest.get('status')}），等它完成后再开工"
                    )
                elif latest.get("conclusion") != "success":
                    ci["status"] = "blocked"
                    blockers.append(
                        f"HEAD 的最新 CI 运行 {latest.get('id')} 没有成功（conclusion={latest.get('conclusion')}），主干原有的红须先处置"
                    )
                else:
                    jobs = actions.jobs(slug, int(latest["id"]))
                    ci["jobs"] = [
                        {
                            "name": job.get("name"),
                            "id": job.get("id"),
                            "status": job.get("status"),
                            "conclusion": job.get("conclusion"),
                            "started_at": job.get("started_at"),
                            "completed_at": job.get("completed_at"),
                        }
                        for job in sorted(jobs, key=lambda item: str(item.get("name")))
                    ]
                    required = set(CI_REQUIRED_JOBS)
                    names = {job.get("name") for job in jobs}
                    ci["required_jobs_missing"] = sorted(required - names)
                    required_failed = sorted(
                        str(job.get("name")) for job in jobs if job.get("name") in required and job.get("conclusion") != "success"
                    )
                    optional_blocking = sorted(
                        str(job.get("name"))
                        for job in jobs
                        if job.get("name") not in required and job.get("conclusion") not in ACCEPTABLE_OPTIONAL_CONCLUSIONS
                    )
                    ci["required_jobs_failed"] = required_failed
                    ci["optional_jobs_blocking"] = optional_blocking
                    # all_jobs_success 仅作信息：每个作业（含非必需）都是 success；是否阻断看必需作业与非必需作业的阻断项
                    ci["all_jobs_success"] = all(job.get("conclusion") == "success" for job in jobs)
                    ci["required_jobs_success"] = not ci["required_jobs_missing"] and not required_failed
                    ci["status"] = "passed" if ci["required_jobs_success"] and not optional_blocking else "blocked"
                    if ci["required_jobs_missing"]:
                        blockers.append(f"HEAD 的 CI 运行缺少必需作业：{ci['required_jobs_missing']}")
                    if required_failed:
                        blockers.append(f"HEAD 的 CI 运行有必需作业未成功：{required_failed}")
                    if optional_blocking:
                        blockers.append(
                            f"HEAD 的 CI 运行有非必需作业失败或被取消（只有 {'／'.join(ACCEPTABLE_OPTIONAL_CONCLUSIONS)} 可接受）：{optional_blocking}"
                        )
        except UpstreamMergeError as error:
            ci["status"] = "blocked"
            blockers.append(f"查询 HEAD 的 CI 结果失败：{error}")
    else:
        ci["reason"] = "HEAD 不在远端受维护分支上，没有查询 CI"

    toolchain = check_toolchain(root, "baseline", probe=probe, environ=environ)
    if toolchain["result"] != "passed":
        blockers.append("基线工具链核对未通过：" + "；".join(describe_mismatches(toolchain)))

    report = bind_identity(
        {
            "schema_version": START_CHECK_SCHEMA,
            "non_authoritative": True,
            "repository": str(root),
            "managed_ref": managed_ref,
            "remote": remote,
            "repository_slug": slug,
            "head": {
                "commit": head,
                "tree": tree,
                "branch": branch,
                "clean": not dirty,
                "origin_commit": origin_commit,
                "origin_matches": origin_matches,
            },
            "ci": ci,
            "security_scan": security,
            "toolchain": toolchain,
            "blockers": blockers,
            "checked_at_utc": clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "result": "ready" if not blockers else "blocked",
        }
    )
    if output is not None:
        write_json_once(output, report)
    return report
