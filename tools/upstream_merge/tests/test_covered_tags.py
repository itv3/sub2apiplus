"""一个 Plan 覆盖多个上游 tag（covered_tags）的复算测试：只用合成 Git 图。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge import contracts
from tools.upstream_merge.contracts import (
    _expect_upstream_fields,
    _validate_covered_tags_shape,
    resolve_covered_tags,
    upstream_range_tags,
)
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.workflow import _preflight_covered_tags


def git(root: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {argv!r} 失败：{completed.stderr}")
    return completed.stdout.strip()


def commit(root: Path, name: str) -> str:
    (root / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    git(root, "add", "--all")
    git(root, "commit", "-q", "-m", name)
    return git(root, "rev-parse", "HEAD")


class CoveredTagsFixture(unittest.TestCase):
    """base → 上游 v1.0.1 → v1.0.2 → v1.0.3；fork 在 main 上另有提交与自己的发版 tag。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repository"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Synthetic Test")
        git(self.root, "config", "user.email", "synthetic@example.invalid")
        self.base = commit(self.root, "base")
        git(self.root, "tag", "v1.0.0", self.base)
        git(self.root, "checkout", "-q", "-b", "upstream")
        self.tags: dict[str, str] = {}
        for tag in ("v1.0.1", "v1.0.2", "v1.0.3"):
            self.tags[tag] = commit(self.root, tag)
            git(self.root, "tag", tag, self.tags[tag])
        git(self.root, "checkout", "-q", "main")
        self.fork = commit(self.root, "fork")
        # fork 自己的发版 tag 不在上游区间内，不得被统计。
        git(self.root, "tag", "v1.0.0-1", self.fork)
        git(self.root, "remote", "add", "upstream", "https://example.invalid/upstream.git")

    def upstream(self, tag: str = "v1.0.3", covered: list[str] | None = None) -> dict:
        value = {"remote": "upstream", "url": "https://example.invalid/upstream.git", "tag": tag, "commit": self.tags[tag]}
        if covered is not None:
            value["covered_tags"] = [{"tag": item, "commit": self.tags[item]} for item in covered]
        return value

    def resolve(self, upstream: dict, *, require_explicit: bool = True):
        return resolve_covered_tags(self.root, upstream, self.base, label="upstream", require_explicit=require_explicit)


class CoveredTagsTest(CoveredTagsFixture):
    def test_range_lists_upstream_tags_in_ancestry_order_only(self) -> None:
        ranged = upstream_range_tags(self.root, self.base, self.tags["v1.0.3"])
        self.assertEqual([item["tag"] for item in ranged], ["v1.0.1", "v1.0.2", "v1.0.3"])
        self.assertEqual([item["commit"] for item in ranged], [self.tags[t] for t in ("v1.0.1", "v1.0.2", "v1.0.3")])

    def test_declared_range_matching_git_passes(self) -> None:
        declared = self.resolve(self.upstream(covered=["v1.0.1", "v1.0.2", "v1.0.3"]))
        self.assertEqual([item["tag"] for item in declared], ["v1.0.1", "v1.0.2", "v1.0.3"])

    def test_missing_wrong_order_or_wrong_commit_rejected(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "与 Git 复算不一致"):
            self.resolve(self.upstream(covered=["v1.0.1", "v1.0.3"]))
        with self.assertRaisesRegex(UpstreamMergeError, "与 Git 复算不一致"):
            self.resolve(self.upstream(covered=["v1.0.2", "v1.0.1", "v1.0.3"]))
        upstream = self.upstream(covered=["v1.0.1", "v1.0.2", "v1.0.3"])
        upstream["covered_tags"][0]["commit"] = self.tags["v1.0.2"]
        with self.assertRaisesRegex(UpstreamMergeError, "与 Git 复算不一致"):
            self.resolve(upstream)

    def test_last_entry_must_be_target(self) -> None:
        upstream = self.upstream(covered=["v1.0.1", "v1.0.2"])
        with self.assertRaisesRegex(UpstreamMergeError, "最后一项必须是目标"):
            self.resolve(upstream)

    def test_multi_tag_range_requires_explicit_registration_for_new_plans(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "必须在 upstream.covered_tags 逐个登记"):
            self.resolve(self.upstream())
        # 早期 Plan 没有 covered_tags，按旧语义加载，不做完整性检查。
        self.assertIsNone(self.resolve(self.upstream(), require_explicit=False))

    def test_single_tag_range_may_omit_covered_tags(self) -> None:
        self.assertIsNone(self.resolve(self.upstream(tag="v1.0.1")))
        declared = self.resolve(self.upstream(tag="v1.0.1", covered=["v1.0.1"]))
        self.assertEqual(declared, [{"tag": "v1.0.1", "commit": self.tags["v1.0.1"]}])

    def test_cross_minor_range_rejected(self) -> None:
        git(self.root, "tag", "v1.1.0", self.tags["v1.0.2"])
        with self.assertRaisesRegex(UpstreamMergeError, "跨 minor"):
            self.resolve(self.upstream(covered=["v1.0.1", "v1.0.2", "v1.0.3"]))
        upstream = self.upstream(covered=["v1.0.1", "v1.0.2", "v1.0.3"])
        upstream["covered_tags"][1]["tag"] = "v1.1.0"
        with self.assertRaisesRegex(UpstreamMergeError, "同一 minor"):
            _validate_covered_tags_shape(upstream, "upstream")

    def test_non_linear_upstream_history_rejected(self) -> None:
        # 两个 tag 分别在两条并行分支上，再由目标合并：区间不是线性历史，必须拒绝。
        git(self.root, "checkout", "-q", "-b", "side", self.base)
        side = commit(self.root, "side")
        git(self.root, "tag", "v1.0.9", side)
        git(self.root, "checkout", "-q", "upstream")
        git(self.root, "merge", "-q", "--no-edit", "side")
        merged = git(self.root, "rev-parse", "HEAD")
        git(self.root, "tag", "v1.0.10", merged)
        git(self.root, "checkout", "-q", "main")
        with self.assertRaisesRegex(UpstreamMergeError, "不在同一条祖先链上"):
            upstream_range_tags(self.root, self.base, merged)

    def test_shape_rejects_malformed_entries_and_unknown_upstream_fields(self) -> None:
        upstream = self.upstream(covered=["v1.0.1", "v1.0.2", "v1.0.3"])
        upstream["covered_tags"][0]["note"] = "extra"
        with self.assertRaises(UpstreamMergeError):
            _validate_covered_tags_shape(upstream, "upstream")
        upstream = self.upstream(covered=["v1.0.3", "v1.0.3"])
        with self.assertRaisesRegex(UpstreamMergeError, "重复"):
            _validate_covered_tags_shape(upstream, "upstream")
        upstream = self.upstream()
        upstream["covered_tags"] = []
        with self.assertRaisesRegex(UpstreamMergeError, "非空数组"):
            _validate_covered_tags_shape(upstream, "upstream")
        with self.assertRaisesRegex(UpstreamMergeError, "多余"):
            _expect_upstream_fields({**self.upstream(), "tags": []}, "upstream")
        with self.assertRaisesRegex(UpstreamMergeError, "缺失"):
            _expect_upstream_fields({"remote": "upstream", "url": "https://x", "tag": "v1.0.3"}, "upstream")

    def test_preflight_reports_counts_and_blocks_missing_registration(self) -> None:
        blockers: list[str] = []
        report = _preflight_covered_tags(self.root, self.upstream(), self.base, blockers)
        self.assertEqual(report["status"], "failed")
        self.assertEqual([row["tag"] for row in report["tags"]], ["v1.0.1", "v1.0.2", "v1.0.3"])
        self.assertEqual([row["commit_count"] for row in report["tags"]], [1, 1, 1])
        self.assertEqual([row["changed_file_count"] for row in report["tags"]], [1, 1, 1])
        self.assertTrue(any("覆盖区间检查失败" in item for item in blockers))
        blockers = []
        report = _preflight_covered_tags(
            self.root, self.upstream(covered=["v1.0.1", "v1.0.2", "v1.0.3"]), self.base, blockers
        )
        self.assertEqual((report["status"], report["findings"], blockers), ("passed", [], []))


class CreatePlanCoveredTagsTest(CoveredTagsFixture):
    """plan-create 在合成仓库上真实执行；只替换扫描器、路由快照、工具摘要等与区间无关的重依赖。"""

    def create(self, upstream: dict, *, plan_root: Path | None = None) -> tuple[Path, Exception | None]:
        outside = self.root.parent / "outside"
        outside.mkdir(exist_ok=True)
        files = {}
        for name in ("active-claude", "rollback-claude", "active-codex", "rollback-codex", "runtime", "recovery", "baseline"):
            path = outside / f"{name}.json"
            path.write_text("{}\n", encoding="utf-8")
            files[name] = str(path)
        workspace = plan_root or outside
        evidence = workspace / "evidence"
        request = {
            "schema_version": contracts.REQUEST_SCHEMA,
            "plan_id": "synthetic-covered-tags",
            "upstream": upstream,
            "repository": {"managed_ref": "refs/heads/main"},
            "workspace": {"worktree": str(workspace / "worktree"), "evidence_root": str(evidence)},
            "official_clients": {
                client: {
                    "persona": {"client": client},
                    "target_version": "1.0.0",
                    "active_path": files[f"active-{client}"],
                    "rollback_path": files[f"rollback-{client}"],
                }
                for client in contracts.CLIENT_KEYS
            },
            "baselines": {
                "production_ingress_inventory": {client: files["runtime"] for client in contracts.CLIENT_KEYS},
                "egress_disposition_inventory": {client: files["runtime"] for client in contracts.CLIENT_KEYS},
                "runtime_state_path": files["runtime"],
                "recovery_point_path": files["recovery"],
                "baseline_acceptance_path": files["baseline"],
            },
            "protected_repository_paths": [],
            "gates": [],
        }

        def fake_snapshot(_root: Path, path: Path, *_args, **_kwargs) -> None:
            path.write_text("{}\n", encoding="utf-8")

        patches = {
            "load_request": lambda _path: request,
            "_validate_baseline_binding": lambda *_args, **_kwargs: None,
            "route_snapshot": lambda *_args, **_kwargs: {"routes": []},
            "run_egress_snapshot": fake_snapshot,
            "command_environment": lambda: {},
            "protected_objects": lambda *_args, **_kwargs: [],
            "tool_bundle": lambda _root: {"bundle_sha256": "0" * 64, "files": []},
            "load_plan": lambda path, _root: path,
        }
        with mock.patch.multiple(contracts, **patches):
            try:
                contracts.create_plan(outside / "request.json", self.root)
            except Exception as error:  # 由调用方断言拒绝原因
                return evidence, error
        return evidence, None

    def test_multi_tag_plan_requires_registration_and_records_range(self) -> None:
        evidence, error = self.create(self.upstream())
        self.assertIsInstance(error, UpstreamMergeError)
        self.assertIn("必须在 upstream.covered_tags 逐个登记", str(error))
        # 拒绝发生在创建 evidence 与写任何制品之前。
        self.assertFalse(evidence.exists())
        evidence, error = self.create(self.upstream(covered=["v1.0.1", "v1.0.2", "v1.0.3"]))
        self.assertIsNone(error)
        plan = json.loads((evidence / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(
            plan["upstream"]["covered_tags"],
            [{"tag": tag, "commit": self.tags[tag]} for tag in ("v1.0.1", "v1.0.2", "v1.0.3")],
        )
        self.assertEqual(plan["repository"]["merge_base"], self.base)

    def test_single_tag_plan_records_target_as_range(self) -> None:
        evidence, error = self.create(self.upstream(tag="v1.0.1"))
        self.assertIsNone(error)
        plan = json.loads((evidence / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["upstream"]["covered_tags"], [{"tag": "v1.0.1", "commit": self.tags["v1.0.1"]}])

    def test_create_plan_in_designated_private_root(self) -> None:
        (self.root / ".gitignore").write_text("/local-analysis/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-q", "-m", "忽略私有分析资料")
        plan_root = self.root / "local-analysis/upstream/v1.0.1-20261006-001"
        evidence, error = self.create(self.upstream(tag="v1.0.1"), plan_root=plan_root)
        self.assertIsNone(error)
        plan = json.loads((evidence / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(plan["workspace"]["evidence_root"]), evidence.resolve())
        self.assertEqual(Path(plan["workspace"]["worktree"]), (plan_root / "worktree").resolve())
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_invalid_internal_root_is_rejected_without_creating_evidence(self) -> None:
        evidence, error = self.create(self.upstream(tag="v1.0.1"), plan_root=self.root / "other")
        self.assertIsInstance(error, UpstreamMergeError)
        self.assertIn("主仓库之外", str(error))
        self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
