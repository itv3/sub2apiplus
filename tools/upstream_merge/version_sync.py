"""发版后 VERSION 回写与冻结承接（UM-8）。

release 工作流的回写 job 调用 ``release-version-sync``，一次完成此前每次发版都要人工补的两步：

1. 抓取远端受维护分支，在其最新 HEAD 上工作——这是回写前的实际主干 HEAD，不一定是发版提交；
2. 把 ``backend/cmd/server/VERSION`` 改成发版版本并提交；
3. 以回写前 HEAD 为 before、回写提交为 after，调用 ``freeze-successor-generate`` 的同一实现生成
   冻结承接收据，作为第二个提交；
4. 两个提交一次推送。推送因主干又前进被拒时，重新抓取并从新 HEAD 重做两个提交，首次推送之外最多重试 3 次。

失败关闭：非法版本号、浅克隆、工作树不干净、冻结链断裂、注册表要求人工动作、承接收据已存在，都在
推送前拒绝，远端不会留下只有回写、没有承接的半截提交。提交说明不得带任何跳过 CI 的字样——GitHub
在提交说明任意位置匹配到就会跳过整次推送的全部工作流。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .canonical import expect_safe_id
from .errors import UpstreamMergeError
from .freeze import MAINTENANCE_ROOT, generate_freeze_successor, plan_freeze_successor
from .gitops import assert_clean, assert_git_repository, git_output, rev_parse, run_git

VERSION_RELATIVE = "backend/cmd/server/VERSION"
# 与发版工具 .github/release-tools/release_matrix.py 的 VERSION_RE 同一口径：三段数字加可选后缀。
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
# GitHub 识别的全部跳过写法（不区分大小写）；bot 提交说明一律不得出现。
SKIP_CI_RE = re.compile(r"\[(?:skip ci|ci skip|no ci|skip actions|actions skip)\]", re.IGNORECASE)
# 首次推送之外最多重试 3 次（UM-8：推送被拒时从新 HEAD 重做，最多重试三次）。
DEFAULT_MAX_ATTEMPTS = 4
MAX_ATTEMPTS_LIMIT = 10
# 这些字样说明远端分支已前进（非快进），可以抓取后重做；其余推送失败一律按错误返回。
PUSH_REJECTED_MARKERS = ("[rejected]", "non-fast-forward", "fetch first", "failed to update ref")


def receipt_relative(version: str) -> str:
    """承接收据的仓库相对路径，与历次人工补登记的命名一致。"""

    return f"{MAINTENANCE_ROOT}/release-v{version}-version-sync-freeze-successor.json"


def _validate_version(version: Any) -> str:
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise UpstreamMergeError(f"发版版本号非法（应为不带 v 前缀的 X.Y.Z 或 X.Y.Z-后缀）：{version!r}")
    return version


def _commit(root: Path, subject: str, body: str, *paths: str) -> str:
    message = f"{subject}\n\n{body}"
    if SKIP_CI_RE.search(message):
        raise UpstreamMergeError("bot 提交说明不得包含跳过 CI 的字样")
    run_git(root, "add", "--", *paths)
    run_git(root, "commit", "--quiet", "-m", subject, "-m", body)
    return rev_parse(root, "HEAD^{commit}")


def _push(root: Path, remote: str, branch: str) -> bool:
    """推送 HEAD 到远端分支；主干已前进时返回 False，其余失败抛错。"""

    completed = run_git(root, "push", remote, f"HEAD:refs/heads/{branch}", check=False)
    if completed.returncode == 0:
        return True
    detail = f"{completed.stderr}\n{completed.stdout}"
    if any(marker in detail for marker in PUSH_REJECTED_MARKERS):
        return False
    raise UpstreamMergeError(f"推送受维护分支失败（exit={completed.returncode}）：{detail.strip()}")


def sync_released_version(
    repository_root: Path,
    version: str,
    *,
    remote: str = "origin",
    branch: str = "main",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    push: bool = True,
) -> dict[str, Any]:
    """回写 VERSION 并生成冻结承接收据，两个提交一次推送；返回执行结果摘要。

    ``push=False`` 只在本地生成两个提交（演练与离线夹具用），结果为 ``prepared``。
    """

    root = assert_git_repository(repository_root)
    version = _validate_version(version)
    remote = expect_safe_id(remote, "remote")
    branch = expect_safe_id(branch, "branch")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
        raise UpstreamMergeError(f"max_attempts 必须是 1～{MAX_ATTEMPTS_LIMIT} 的整数")
    if git_output(root, "rev-parse", "--is-shallow-repository") == "true":
        raise UpstreamMergeError("浅克隆无法判定冻结前序摘要；发版工作流须以 fetch-depth: 0 检出")
    assert_clean(root, "VERSION 回写")

    remote_ref = f"refs/remotes/{remote}/{branch}"
    version_path = root / VERSION_RELATIVE
    receipt_rel = receipt_relative(version)
    for attempt in range(1, max_attempts + 1):
        run_git(root, "fetch", "--quiet", "--no-tags", remote, f"+refs/heads/{branch}:{remote_ref}")
        before = rev_parse(root, f"{remote_ref}^{{commit}}")
        # 每一轮都从远端最新 HEAD 重新开始；上一轮被拒的两个提交留在游离历史里，不会被推送。
        run_git(root, "checkout", "--quiet", "--detach", before)
        current = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else None
        if current == version:
            return {
                "result": "already_synced",
                "version": version,
                "remote": remote,
                "branch": branch,
                "attempts": attempt,
                "head_commit": before,
            }
        if (root / receipt_rel).exists():
            raise UpstreamMergeError(f"承接收据已存在但 VERSION 未同步，需人工核对：{receipt_rel}")

        version_path.write_text(version + "\n", encoding="utf-8")
        version_commit = _commit(
            root,
            f"chore: sync VERSION to {version}",
            "发版后回写 backend/cmd/server/VERSION；冻结承接收据是下一个提交，"
            "两个提交由 release-version-sync 一次推送。",
            VERSION_RELATIVE,
        )

        plan = plan_freeze_successor(root, before, version_commit)
        if plan["broken_chain"]:
            listed = ", ".join(item["path"] for item in plan["broken_chain"])
            raise UpstreamMergeError(f"冻结链断裂，不能自动承接：{listed}")
        if plan["required_manual_actions"]:
            listed = ", ".join(sorted({action["rule_id"] for action in plan["required_manual_actions"]}))
            raise UpstreamMergeError(f"冻结注册表要求人工动作，不能自动承接：{listed}")

        receipt_commit: str | None = None
        if plan["frozen_hits"]:
            generate_freeze_successor(
                root,
                before,
                version_commit,
                root / receipt_rel,
                tag=f"release-v{version}-version-sync",
                reason=(
                    f"登记发版 v{version} 后发版 bot 回写 backend/cmd/server/VERSION 至 {version}"
                    f"（{version_commit[:9]}）的冻结承接边；前序为回写前主干 HEAD {before[:9]}；"
                    "不改画像、Persona、wire 或出站"
                ),
            )
            receipt_commit = _commit(
                root,
                f"chore(release): 登记 v{version} 发版后 VERSION 回写的冻结承接收据",
                f"发版 bot 在 {version_commit[:9]} 把 backend/cmd/server/VERSION 回写为 {version}。"
                f"该文件在冻结覆盖内，承接边以回写前主干 HEAD {before[:9]} 为前序，"
                "由 release-version-sync 自动生成，与回写提交一次推送。",
                receipt_rel,
            )

        summary = {
            "version": version,
            "remote": remote,
            "branch": branch,
            "attempts": attempt,
            "before_commit": before,
            "version_commit": version_commit,
            "receipt_commit": receipt_commit,
            "receipt": receipt_rel if receipt_commit else None,
            "head_commit": receipt_commit or version_commit,
        }
        if not push:
            return {"result": "prepared", **summary}
        if _push(root, remote, branch):
            return {"result": "synced", **summary}
    raise UpstreamMergeError(f"推送 {max_attempts} 次均因受维护分支前进被拒，已停止重试")
