"""UM-9 第 1、2 项：试验区定型树重放与冲突决策重放的合成 Git 图测试。

场景：前序 Plan A 在 fork F1 上解决冲突（并在非冲突文件上做了适配）后封存 U-1；主干随后新增提交
得到 F2，后继 Plan B 以 F2 为 fork HEAD。Plan B 用 ``plan-replay`` 合成 A 的定型树与主干新增，
再用 ``merge-seal --replay-from`` 复用 A 的冲突决定。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.canonical import bind_identity, sha256_file, write_json_once
from tools.upstream_merge.contracts import PLAN_PURPOSE, PLAN_SCHEMA, LoadedPlan, _output_layout
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.gitops import commit_tree, merge_base, protected_objects, rev_parse, tool_bundle
from tools.upstream_merge.plan_inputs import AWAITING_MANUAL_INPUT
from tools.upstream_merge.plan_replay import (
    _replay_rationale,
    replay_trial_tree,
    seal_merge_with_replay,
)
from tools.upstream_merge.tests.test_workflow import SyntheticRepository, run
from tools.upstream_merge.workflow import CONFLICT_INPUT_SCHEMA, seal_merge, start_merge

RATIONALE_A = "保留 fork 控制边界并合入上游业务修复（Plan A 人工审定）"


def make_plan(fixture: SyntheticRepository, *, fork: str, plan_root: Path, plan_id: str) -> LoadedPlan:
    """在同一合成仓库上为另一个 fork HEAD 建 Plan（标准布局 <计划目录>/evidence）。"""

    evidence = plan_root / "evidence"
    evidence.mkdir(parents=True, mode=0o700)
    evidence.chmod(0o700)
    worktree = plan_root / "worktree"
    document = bind_identity(
        {
            "schema_version": PLAN_SCHEMA,
            "plan_id": plan_id,
            "purpose": PLAN_PURPOSE,
            "upstream": {
                "remote": "upstream",
                "url": "https://example.invalid/upstream.git",
                "tag": "v1.0.1",
                "commit": fixture.upstream,
            },
            "repository": {
                "managed_ref": "refs/heads/main",
                "fork_head": fork,
                "fork_tree": commit_tree(fixture.root, fork),
                "merge_base": merge_base(fixture.root, fork, fixture.upstream),
                "protected_objects": protected_objects(fixture.root, fork, ["protected.txt"]),
            },
            "workspace": {"worktree": str(worktree), "evidence_root": str(evidence)},
            "official_clients": {},
            "baselines": {},
            "discovery_baseline": {},
            "tool_bundle": tool_bundle(fixture.root),
            "environment": {},
            "gates": [],
            "outputs": _output_layout("v1.0.1"),
        }
    )
    plan_path = evidence / "plan.json"
    write_json_once(plan_path, document)
    return LoadedPlan(
        document=document,
        path=plan_path,
        repository_root=fixture.root,
        evidence_root=evidence.resolve(),
        worktree=worktree,
    )


def conflict_input(plan: LoadedPlan, path: Path, resolutions: list[dict[str, str]]) -> Path:
    write_json_once(
        path,
        bind_identity(
            {
                "schema_version": CONFLICT_INPUT_SCHEMA,
                "plan_id": plan.plan_id,
                "plan_identity_sha256": plan.identity,
                "merge_start_sha256": sha256_file(plan.output_path("merge_start")),
                "resolutions": resolutions,
            }
        ),
    )
    return path


class PlanReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name).resolve()
        self.fixture = SyntheticRepository(
            self.temp_root,
            conflict=True,
            extra_fork_files={"fork-only.txt": "fork only\n"},
        )
        self.worktrees: list[Path] = [self.fixture.worktree]
        # Plan A：解决冲突并在非冲突文件上做适配，然后封存 U-1。
        self.plan_a = self.fixture.plan
        start_merge(self.plan_a)
        worktree_a = self.fixture.worktree
        (worktree_a / "conflict.txt").write_text("fork+upstream\n", encoding="utf-8")
        (worktree_a / "fork-only.txt").write_text("fork only (adapted in trial)\n", encoding="utf-8")
        run(worktree_a, "git", "add", "--all")
        decision = conflict_input(
            self.plan_a,
            self.temp_root / "conflict-a.json",
            [{"path": "conflict.txt", "resolution": "manual", "rationale": RATIONALE_A}],
        )
        self.candidate_a = seal_merge(self.plan_a, decision)

    def tearDown(self) -> None:
        for worktree in self.worktrees:
            if worktree.exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=self.fixture.root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        self.temporary.cleanup()

    def advance_main(self, files: dict[str, str], message: str) -> str:
        for relative, content in files.items():
            (self.fixture.root / relative).write_text(content, encoding="utf-8")
        run(self.fixture.root, "git", "add", "--all")
        run(self.fixture.root, "git", "commit", "-m", message)
        return rev_parse(self.fixture.root, "HEAD^{commit}")

    def plan_b(self, fork: str) -> LoadedPlan:
        plan = make_plan(self.fixture, fork=fork, plan_root=self.temp_root / "plan-b", plan_id="synthetic-v1.0.1-b")
        self.worktrees.append(plan.worktree)
        start_merge(plan)
        return plan

    def test_replay_trial_tree_and_conflict_decisions(self) -> None:
        fork_b = self.advance_main({"mainline.txt": "mainline\n"}, "主干新增")
        plan_b = self.plan_b(fork_b)

        report = replay_trial_tree(
            plan_b,
            self.candidate_a["candidate_tree"],
            self.fixture.fork,
            self.plan_a.evidence_root,
        )
        self.assertEqual(report["mainline_delta_paths"], ["mainline.txt"])
        self.assertEqual(report["non_conflict_adjustments"], ["fork-only.txt"])
        self.assertEqual(report["previous_plan"]["diff_paths"], ["mainline.txt"])
        self.assertTrue(report["previous_plan"]["all_within_mainline_delta"])

        sealed = seal_merge_with_replay(plan_b, self.plan_a.evidence_root, None)
        self.assertEqual(sealed["candidate_tree"], report["candidate_tree"])
        self.assertEqual(sealed["conflict_replay"]["replayed_count"], 1)
        self.assertEqual(sealed["conflict_replay"]["manual_count"], 0)
        ledger = json.loads(plan_b.output_path("conflict_ledger").read_text(encoding="utf-8"))
        entry = ledger["resolutions"][0]
        self.assertEqual(entry["resolution"], "manual")
        self.assertTrue(entry["rationale"].startswith(RATIONALE_A))
        self.assertIn("〔重放自 synthetic-v1.0.1：", entry["rationale"])
        self.assertTrue((self.temp_root / "plan-b/inputs/conflict-decisions-replayed.json").is_file())

    def test_replay_note_is_replaced_not_stacked(self) -> None:
        once = _replay_rationale(RATIONALE_A, "plan-a")
        twice = _replay_rationale(once, "plan-b")
        self.assertEqual(twice.count("〔重放自"), 1)
        self.assertIn("plan-b", twice)
        self.assertTrue(twice.startswith(RATIONALE_A))

    def test_changed_stage_is_handed_to_human_with_draft(self) -> None:
        # 主干再次改动冲突文件：stage 2 与前序不同，这条冲突不重放；上游同样改过它，树重放也拒绝。
        fork_b = self.advance_main({"conflict.txt": "fork v2\n"}, "主干改动冲突文件")
        plan_b = self.plan_b(fork_b)
        with self.assertRaisesRegex(UpstreamMergeError, "上游也改动了主干新增路径"):
            replay_trial_tree(plan_b, self.candidate_a["candidate_tree"], self.fixture.fork)

        worktree_b = plan_b.worktree
        (worktree_b / "conflict.txt").write_text("fork v2+upstream\n", encoding="utf-8")
        run(worktree_b, "git", "add", "conflict.txt")
        waiting = seal_merge_with_replay(plan_b, self.plan_a.evidence_root, None)
        self.assertEqual(waiting["result"], AWAITING_MANUAL_INPUT)
        self.assertEqual([item["path"] for item in waiting["remaining"]], ["conflict.txt"])
        self.assertIn("stage 2", waiting["remaining"][0]["reason"])
        self.assertFalse(plan_b.output_path("conflict_ledger").exists())
        draft = json.loads(Path(waiting["draft"]).read_text(encoding="utf-8"))
        self.assertEqual(draft["resolutions"], [{"path": "conflict.txt", "resolution": "manual", "rationale": ""}])

        manual = conflict_input(
            plan_b,
            self.temp_root / "plan-b/inputs/conflict-decisions-remaining.json",
            [{"path": "conflict.txt", "resolution": "manual", "rationale": "主干第二版与上游修复逐段合并，人工审定"}],
        )
        sealed = seal_merge_with_replay(plan_b, self.plan_a.evidence_root, manual)
        self.assertEqual(sealed["conflict_replay"]["replayed_count"], 0)
        self.assertEqual(sealed["conflict_replay"]["manual_count"], 1)

    def test_trial_change_on_mainline_path_is_refused(self) -> None:
        # 试验区适配过 fork-only.txt，主干随后也改了它：机械重放会丢掉试验区改动。
        fork_b = self.advance_main({"fork-only.txt": "fork only v2\n"}, "主干改动试验区适配过的文件")
        plan_b = self.plan_b(fork_b)
        with self.assertRaisesRegex(UpstreamMergeError, "试验区改动了主干新增路径"):
            replay_trial_tree(plan_b, self.candidate_a["candidate_tree"], self.fixture.fork)

    def test_replay_rejects_self_and_non_ancestor_base(self) -> None:
        fork_b = self.advance_main({"mainline.txt": "mainline\n"}, "主干新增")
        plan_b = self.plan_b(fork_b)
        with self.assertRaisesRegex(UpstreamMergeError, "不能指向本 Plan"):
            seal_merge_with_replay(plan_b, plan_b.evidence_root, None)
        with self.assertRaisesRegex(UpstreamMergeError, "不是本 Plan fork HEAD 的祖先"):
            replay_trial_tree(plan_b, self.candidate_a["candidate_tree"], self.fixture.upstream)


if __name__ == "__main__":
    unittest.main()
