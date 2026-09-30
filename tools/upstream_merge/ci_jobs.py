"""上游 CI 作业差异（UM-14）。

v0.2.10 合并时，上游在 ``.github/workflows/backend-ci.yml`` 新增了 ``release-helpers`` 作业（发布矩阵
测试）。本机 U-4 的 ``upstream-gate-full`` 不含它：首轮本机门禁全绿，候选 CI 却红在这个作业，多走
两个 revision、一轮本机门禁与一轮 CI。这里在预检阶段就回答两个问题：

1. 上游在 merge-base..目标 tag 之间新增、删除或改动了哪些 CI 作业，各自执行什么命令；
2. 这些作业在本机门禁里由哪条检查线覆盖，还是只在 CI 跑——以受管登记表
   ``tools/upstream_merge/ci_job_coverage.json`` 为准。候选 CI 会运行、却没有登记的作业一律阻断，
   要求建 Plan 前决定纳入本机门禁还是登记为只在 CI 跑；只在 CI 跑的作业列出命令，在试验
   worktree 先执行一次。

本机没有 PyYAML，这里按 GitHub 工作流的缩进结构只取作业原文块与 ``run`` 命令，不做通用 YAML 解析。
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from pathlib import Path
from typing import Any

from .canonical import expect_object, load_json
from .errors import UpstreamMergeError
from .gate_runner import build_lanes
from .gitops import run_git

WORKFLOW_DIR = ".github/workflows"
CI_JOB_COVERAGE_RELATIVE = "tools/upstream_merge/ci_job_coverage.json"
CI_JOB_COVERAGE_SCHEMA = "official-egress-upstream-ci-job-coverage/v1"
COVERAGE_KINDS = ("local", "ci_only")
JOBS_KEY_RE = re.compile(r"^jobs:\s*(?:#.*)?$")
JOB_KEY_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$")
RUN_KEY_RE = re.compile(r"^(\s*)(- )?run:\s*(.*)$")
CI_JOB_METHOD = (
    "解析 merge-base 与目标 tag 两棵上游树的 .github/workflows/*.yml，按作业原文块比对新增、删除与改动；"
    "候选 CI 会运行的工作流逐个对照 ci_job_coverage.json 登记的本机检查线或只在 CI 跑"
)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _run_commands(block: list[str]) -> list[str]:
    """取出作业中每个 step 的 run 命令：单行标量或 ``|``／``>`` 块。"""

    commands: list[str] = []
    index = 0
    while index < len(block):
        match = RUN_KEY_RE.match(block[index])
        if match is None:
            index += 1
            continue
        key_column = len(match.group(1)) + (2 if match.group(2) else 0)
        value = match.group(3).strip()
        index += 1
        if value[:1] in ("|", ">"):
            body: list[str] = []
            while index < len(block) and (not block[index].strip() or _indent(block[index]) > key_column):
                body.append(block[index])
                index += 1
            commands.append(textwrap.dedent("\n".join(body)).strip())
        else:
            # 只有整条命令被同一种引号包住时才去引号；命令内部或结尾的引号属于命令本身。
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            commands.append(value)
    return commands


def parse_workflow_jobs(text: str) -> dict[str, dict[str, Any]]:
    """按缩进结构取出顶层 ``jobs:`` 下每个作业的原文摘要与 run 命令。"""

    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if JOBS_KEY_RE.match(line)), None)
    if start is None:
        return {}
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "#")):
            break
        match = JOB_KEY_RE.match(line)
        if match:
            current = match.group(1)
            if current in blocks:
                raise UpstreamMergeError(f"工作流作业重复定义：{current}")
            blocks[current] = [line]
            continue
        if current is not None:
            blocks[current].append(line)
    jobs: dict[str, dict[str, Any]] = {}
    for name, block in blocks.items():
        normalized = "\n".join(line.rstrip() for line in block).rstrip() + "\n"
        jobs[name] = {
            "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "runs": _run_commands(block),
        }
    return jobs


def _workflow_files(root: Path, commit: str) -> dict[str, str]:
    listed = run_git(root, "ls-tree", "--name-only", commit, f"{WORKFLOW_DIR}/").stdout.split()
    files: dict[str, str] = {}
    for path in listed:
        if path.endswith((".yml", ".yaml")):
            files[path] = run_git(root, "show", f"{commit}:{path}").stdout
    return files


def ci_job_delta(root: Path, merge_base: str, upstream_commit: str) -> list[dict[str, Any]]:
    """上游 merge-base..目标提交之间每个工作流的作业增删改（只列有变化的工作流）。"""

    before = _workflow_files(root, merge_base)
    after = _workflow_files(root, upstream_commit)
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old = parse_workflow_jobs(before.get(path, ""))
        new = parse_workflow_jobs(after.get(path, ""))
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(name for name in set(old) & set(new) if old[name]["sha256"] != new[name]["sha256"])
        if not (added or removed or changed or (path in before) != (path in after)):
            continue
        changes.append(
            {
                "workflow": path,
                "status": "added" if path not in before else "removed" if path not in after else "modified",
                "jobs": [
                    *({"job": name, "change": "added", "runs": new[name]["runs"]} for name in added),
                    *({"job": name, "change": "changed", "runs": new[name]["runs"]} for name in changed),
                    *({"job": name, "change": "removed", "runs": old[name]["runs"]} for name in removed),
                ],
            }
        )
    return changes


def load_ci_job_coverage(root: Path) -> dict[str, Any]:
    """读取并校验 CI 作业覆盖登记表；结构不闭合即 fail-close。"""

    document = expect_object(load_json(root / CI_JOB_COVERAGE_RELATIVE, "CI 作业覆盖登记表"), "CI 作业覆盖登记表")
    if document.get("schema_version") != CI_JOB_COVERAGE_SCHEMA:
        raise UpstreamMergeError("CI 作业覆盖登记表 schema_version 非法")
    workflows = expect_object(document.get("workflows"), "CI 作业覆盖登记表.workflows")
    lanes = {lane.name for lane in build_lanes("full", root)}
    for path, raw in workflows.items():
        label = f"CI 作业覆盖登记表.workflows[{path}]"
        entry = expect_object(raw, label)
        if not isinstance(entry.get("candidate_ci"), bool):
            raise UpstreamMergeError(f"{label}.candidate_ci 必须是布尔值")
        if not entry["candidate_ci"]:
            if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
                raise UpstreamMergeError(f"{label} 不在候选 CI 运行时必须写明 reason")
            continue
        for name, job_raw in expect_object(entry.get("jobs"), f"{label}.jobs").items():
            job = expect_object(job_raw, f"{label}.jobs[{name}]")
            coverage = job.get("coverage")
            if coverage not in COVERAGE_KINDS:
                raise UpstreamMergeError(f"{label}.jobs[{name}].coverage 必须是 local 或 ci_only")
            if coverage == "local":
                declared = job.get("lanes")
                if not isinstance(declared, list) or not declared or not set(declared) <= lanes:
                    raise UpstreamMergeError(f"{label}.jobs[{name}].lanes 必须是本机门禁检查线：{sorted(lanes)}")
            elif not isinstance(job.get("reason"), str) or not job["reason"].strip():
                raise UpstreamMergeError(f"{label}.jobs[{name}] 只在 CI 跑时必须写明 reason")
    return document


def _coverage_of(registry: dict[str, Any], workflow: str, job: str) -> dict[str, Any]:
    entry = registry["workflows"].get(workflow)
    if entry is None:
        return {"coverage": "unregistered", "reason": "工作流未登记"}
    if not entry["candidate_ci"]:
        return {"coverage": "not_candidate", "reason": entry["reason"]}
    registered = entry["jobs"].get(job)
    if registered is None:
        return {"coverage": "unregistered", "reason": "候选 CI 会运行，但作业未登记"}
    return dict(registered)


def fork_unregistered_jobs(root: Path, registry: dict[str, Any], commit: str = "HEAD") -> list[str]:
    """fork 当前候选 CI 会运行的作业里，未在登记表登记的（登记表须覆盖 fork 自身的作业）。"""

    missing: list[str] = []
    for path, text in sorted(_workflow_files(root, commit).items()):
        entry = registry["workflows"].get(path)
        if entry is None:
            missing.append(path)
            continue
        if not entry["candidate_ci"]:
            continue
        missing.extend(f"{path}#{name}" for name in sorted(parse_workflow_jobs(text)) if name not in entry["jobs"])
    return missing


def ci_job_coverage(root: Path, merge_base: str, upstream_commit: str, fork_head: str = "HEAD") -> dict[str, Any]:
    """预检第六项：上游 CI 作业差异及本机覆盖情况，有未登记的候选 CI 作业即阻断。"""

    try:
        registry = load_ci_job_coverage(root)
        delta = ci_job_delta(root, merge_base, upstream_commit)
        fork_missing = fork_unregistered_jobs(root, registry, fork_head)
    except (OSError, UpstreamMergeError) as error:
        return {"status": "failed", "method": CI_JOB_METHOD, "error": str(error)}
    jobs: list[dict[str, Any]] = []
    for workflow in delta:
        for job in workflow["jobs"]:
            coverage = _coverage_of(registry, workflow["workflow"], job["job"])
            jobs.append({"workflow": workflow["workflow"], **job, **coverage})
    unregistered = [
        f"{item['workflow']}#{item['job']}"
        for item in jobs
        if item["coverage"] == "unregistered" and item["change"] != "removed"
    ]
    trial_commands = [
        {"workflow": item["workflow"], "job": item["job"], "runs": item["runs"]}
        for item in jobs
        if item["change"] != "removed" and item["coverage"] in ("ci_only", "unregistered")
    ]
    return {
        "status": "blocked" if unregistered or fork_missing else "passed",
        "method": CI_JOB_METHOD,
        "changed_workflows": [item["workflow"] for item in delta],
        "jobs": jobs,
        "unregistered_jobs": unregistered,
        "fork_unregistered_jobs": fork_missing,
        "trial_commands": trial_commands,
    }
