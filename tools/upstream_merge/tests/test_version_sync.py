"""发版后 VERSION 回写与冻结承接（UM-8）的离线夹具。

每个用例都在临时目录里搭一个"远端裸仓库 + 发版工作流检出副本 + 另一位开发者的副本"：
回写命令在检出副本里执行并真实推送到裸仓库，另一副本用来模拟主干在回写前或推送前又前进。
承接收据是否被接受，用冻结台账抽边的同一实现（load_frozen_edges）在推送后的远端 HEAD 上复核：
VERSION 的当前摘要必须已是登记过的后继摘要，这正是 worktree successor 门禁检查的条件。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge import version_sync
from tools.upstream_merge.canonical import bind_identity, sha256_bytes
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.freeze import (
    FREEZE_REGISTRY_RELATIVE,
    FREEZE_REGISTRY_SCHEMA,
    MAINTENANCE_ROOT,
    gate_compatible_identity,
    load_frozen_edges,
)
from tools.upstream_merge.version_sync import SKIP_CI_RE, VERSION_RELATIVE, receipt_relative, sync_released_version

SOURCE_ROOT = Path(__file__).resolve().parents[3]
OLD_VERSION = "0.2.9"
NEW_VERSION = "0.2.10"


def git(repository: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {argv!r} 失败：{completed.stderr or completed.stdout}")
    return completed.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, document: dict) -> None:
    write(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def registry(rules: list[dict]) -> dict:
    return bind_identity(
        {
            "schema_version": FREEZE_REGISTRY_SCHEMA,
            "issued_at_utc": "2026-09-30T00:00:00Z",
            "scope": "freeze-registry",
            "policy": {"generic_graph": "maintenance/*.json"},
            "rules": rules,
        }
    )


BENIGN_RULE = {
    "id": "scanner",
    "description": "与 VERSION 无关的扫描器单跳规则",
    "match": {"prefixes": ["backend/cmd/scan/"]},
    "action": {"kind": "single_hop_file", "file": "docs/x.json", "instruction": "改写 to"},
    "verification": ["make scan"],
}


class ReleaseVersionSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        seed = base / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        self._identity(seed)
        write(seed / VERSION_RELATIVE, OLD_VERSION + "\n")
        write(seed / "backend/app.go", "package app\n")
        write_json(seed / FREEZE_REGISTRY_RELATIVE, registry([BENIGN_RULE]))
        # VERSION 由一份历史承接收据登记为冻结路径，前序是任意历史摘要。
        write_json(
            seed / MAINTENANCE_ROOT / "seed-version-successor.json",
            {
                "schema_version": "official-egress-example-successor/v1",
                "transitions": [
                    {
                        "path": VERSION_RELATIVE,
                        "predecessor_sha256s": ["0" * 64],
                        "to_sha256": sha256_bytes(f"{OLD_VERSION}\n".encode()),
                        "reason": "夹具：历史发版登记的 VERSION 摘要",
                    }
                ],
            },
        )
        git(seed, "add", "--all")
        git(seed, "commit", "-q", "-m", "seed")
        self.release_commit = git(seed, "rev-parse", "HEAD")
        self.remote = base / "remote.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(seed, "push", "-q", str(self.remote), "main")
        self.runner = self._clone(base / "runner")
        self.other = self._clone(base / "other")

    def _identity(self, repository: Path) -> None:
        git(repository, "config", "user.name", "Release Bot")
        git(repository, "config", "user.email", "bot@example.invalid")
        git(repository, "config", "commit.gpgsign", "false")

    def _clone(self, destination: Path) -> Path:
        git(destination.parent, "clone", "-q", str(self.remote), str(destination))
        self._identity(destination)
        return destination

    def _advance_main(self, label: str) -> str:
        """另一位开发者往主干推一个与 VERSION 无关的提交，返回新的主干 HEAD。"""

        git(self.other, "pull", "-q", "--ff-only", "origin", "main")
        write(self.other / "backend" / f"{label}.go", f"package {label}\n")
        git(self.other, "add", "--all")
        git(self.other, "commit", "-q", "-m", f"feat: {label}")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        return git(self.other, "rev-parse", "HEAD")

    def _remote_head(self) -> str:
        return git(self.remote, "rev-parse", "main")

    def _checkout_remote_head(self) -> Path:
        verify = Path(self.temporary.name) / f"verify-{self._remote_head()[:12]}"
        return self._clone(verify)

    def _assert_accepted_by_freeze_graph(self, checkout: Path) -> None:
        """推送后的远端 HEAD：VERSION 当前摘要必须已是登记过的后继摘要（worktree successor 门禁条件）。"""

        edges, known, receipts = load_frozen_edges(checkout)
        current = sha256_bytes((checkout / VERSION_RELATIVE).read_bytes())
        self.assertIn(current, known[VERSION_RELATIVE])
        self.assertIn(receipt_relative(NEW_VERSION), receipts[VERSION_RELATIVE])
        # 与 Go 侧 loadAuditedSourceSuccessorEdges 同一抽边口径：旧摘要 → 新摘要这条边必须存在。
        previous = sha256_bytes(f"{OLD_VERSION}\n".encode())
        self.assertIn(
            (VERSION_RELATIVE, previous, current),
            {(edge.path, edge.from_sha256, edge.to_sha256) for edge in edges},
        )

    def _assert_sync_commits(self, result: dict, before: str) -> None:
        self.assertEqual("synced", result["result"])
        self.assertEqual(before, result["before_commit"])
        self.assertEqual(result["receipt_commit"], self._remote_head())
        self.assertEqual(before, git(self.remote, "rev-parse", f"{result['version_commit']}^"))
        self.assertEqual(result["version_commit"], git(self.remote, "rev-parse", f"{result['receipt_commit']}^"))
        for commit in (result["version_commit"], result["receipt_commit"]):
            self.assertIsNone(SKIP_CI_RE.search(git(self.remote, "log", "-1", "--format=%B", commit)))
        checkout = self._checkout_remote_head()
        receipt = json.loads((checkout / receipt_relative(NEW_VERSION)).read_text(encoding="utf-8"))
        self.assertEqual(before, receipt["base_commit"])
        self.assertEqual(result["version_commit"], receipt["current_commit"])
        self.assertEqual("commit", receipt["mode"])
        self.assertEqual(gate_compatible_identity(receipt), receipt["identity_sha256"])
        [transition] = receipt["transitions"]
        self.assertEqual(VERSION_RELATIVE, transition["path"])
        self.assertEqual([sha256_bytes(f"{OLD_VERSION}\n".encode())], transition["predecessor_sha256s"])
        self.assertEqual(sha256_bytes(f"{NEW_VERSION}\n".encode()), transition["to_sha256"])
        self.assertEqual(NEW_VERSION + "\n", (checkout / VERSION_RELATIVE).read_text(encoding="utf-8"))
        self._assert_accepted_by_freeze_graph(checkout)

    def test_release_commit_is_main_head(self) -> None:
        result = sync_released_version(self.runner, NEW_VERSION)
        self.assertEqual(1, result["attempts"])
        self._assert_sync_commits(result, self.release_commit)

    def test_main_advanced_before_write_back_uses_actual_head(self) -> None:
        # 发版提交之后主干又前进一个提交：before 必须是回写前的实际主干 HEAD，而不是发版提交。
        advanced = self._advance_main("after_release")
        self.assertNotEqual(self.release_commit, advanced)
        result = sync_released_version(self.runner, NEW_VERSION)
        self._assert_sync_commits(result, advanced)

    def test_push_race_rebuilds_both_commits_on_new_head(self) -> None:
        real_push = version_sync._push
        competing: list[str] = []

        def racing_push(root: Path, remote: str, branch: str) -> bool:
            if not competing:
                competing.append(self._advance_main("race"))
            return real_push(root, remote, branch)

        with mock.patch.object(version_sync, "_push", side_effect=racing_push):
            result = sync_released_version(self.runner, NEW_VERSION)
        self.assertEqual(2, result["attempts"])
        self._assert_sync_commits(result, competing[0])

    def test_gives_up_after_max_attempts_without_partial_push(self) -> None:
        real_push = version_sync._push
        counter = [0]

        def always_racing(root: Path, remote: str, branch: str) -> bool:
            counter[0] += 1
            self._advance_main(f"race{counter[0]}")
            return real_push(root, remote, branch)

        with mock.patch.object(version_sync, "_push", side_effect=always_racing):
            with self.assertRaisesRegex(UpstreamMergeError, "推送 4 次均因受维护分支前进被拒"):
                sync_released_version(self.runner, NEW_VERSION)
        # 首次推送之外最多重试 3 次，共推送 4 次。
        self.assertEqual(4, counter[0])
        checkout = self._checkout_remote_head()
        self.assertEqual(OLD_VERSION + "\n", (checkout / VERSION_RELATIVE).read_text(encoding="utf-8"))
        self.assertFalse((checkout / receipt_relative(NEW_VERSION)).exists())

    def test_already_synced_version_is_noop(self) -> None:
        before = self._remote_head()
        result = sync_released_version(self.runner, OLD_VERSION)
        self.assertEqual("already_synced", result["result"])
        self.assertEqual(before, self._remote_head())

    def test_no_push_prepares_two_local_commits_only(self) -> None:
        before = self._remote_head()
        result = sync_released_version(self.runner, NEW_VERSION, push=False)
        self.assertEqual("prepared", result["result"])
        self.assertEqual(before, self._remote_head())
        self.assertEqual(result["receipt_commit"], git(self.runner, "rev-parse", "HEAD"))
        self.assertEqual(result["version_commit"], git(self.runner, "rev-parse", "HEAD^"))

    def test_rejects_invalid_version_and_arguments(self) -> None:
        for value in ("v0.2.10", "0.2", "0.2.10;rm -rf /", "0.2.10\n", ""):
            with self.subTest(value=value), self.assertRaisesRegex(UpstreamMergeError, "版本号非法"):
                sync_released_version(self.runner, value)
        with self.assertRaisesRegex(UpstreamMergeError, "max_attempts"):
            sync_released_version(self.runner, NEW_VERSION, max_attempts=0)
        with self.assertRaises(UpstreamMergeError):
            sync_released_version(self.runner, NEW_VERSION, branch="main;echo")

    def test_rejects_shallow_clone(self) -> None:
        shallow = Path(self.temporary.name) / "shallow"
        git(shallow.parent, "clone", "-q", "--depth", "1", f"file://{self.remote}", str(shallow))
        self._identity(shallow)
        with self.assertRaisesRegex(UpstreamMergeError, "浅克隆"):
            sync_released_version(shallow, NEW_VERSION)

    def test_rejects_dirty_worktree(self) -> None:
        write(self.runner / "untracked.txt", "x\n")
        with self.assertRaisesRegex(UpstreamMergeError, "干净工作树"):
            sync_released_version(self.runner, NEW_VERSION)

    def test_registry_manual_action_fails_closed_before_push(self) -> None:
        git(self.other, "pull", "-q", "--ff-only", "origin", "main")
        rule = {
            "id": "version-manual",
            "description": "夹具：VERSION 命中需要人工的注册表规则",
            "match": {"paths": [VERSION_RELATIVE]},
            "action": {"kind": "single_hop_file", "file": "docs/y.json", "instruction": "人工改写"},
            "verification": ["make y"],
        }
        write_json(self.other / FREEZE_REGISTRY_RELATIVE, registry([BENIGN_RULE, rule]))
        git(self.other, "commit", "-q", "-am", "chore: 注册表新增人工规则")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        before = self._remote_head()
        with self.assertRaisesRegex(UpstreamMergeError, "version-manual"):
            sync_released_version(self.runner, NEW_VERSION)
        self.assertEqual(before, self._remote_head())

    def test_commit_message_guard_rejects_every_skip_ci_spelling(self) -> None:
        for spelling in ("[skip ci]", "[CI SKIP]", "[no ci]", "[skip actions]", "[actions skip]"):
            with self.subTest(spelling=spelling), self.assertRaisesRegex(UpstreamMergeError, "跳过 CI"):
                version_sync._commit(self.runner, "chore: x", f"说明里引用 {spelling} 也不行", VERSION_RELATIVE)

    def test_release_workflow_uses_version_sync_and_dispatches_ci(self) -> None:
        # 本机没有 PyYAML，按文本核对工作流接线；CI 的 release-helpers 作业另有 YAML 级测试。
        release = (SOURCE_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        job = release[release.index("  sync-version-file:"):]
        self.assertIn("fetch-depth: 0", job)
        self.assertIn("python3 -m tools.upstream_merge release-version-sync", job)
        self.assertIn("actions: write", job)
        self.assertIn("gh workflow run backend-ci.yml", job)
        self.assertIsNone(SKIP_CI_RE.search(release))
        self.assertNotIn("git push", job)
        ci = (SOURCE_ROOT / ".github/workflows/backend-ci.yml").read_text(encoding="utf-8")
        head = ci[: ci.index("\njobs:")]
        self.assertIn("workflow_dispatch:", head)


if __name__ == "__main__":
    unittest.main()
