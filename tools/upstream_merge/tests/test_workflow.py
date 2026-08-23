"""完整计划、隔离合并和失败关闭边界的合成测试。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge import gitops as upstream_gitops
from tools.upstream_merge.canonical import (
    bind_identity,
    resolve_within,
    sha256_file,
    write_json_once,
)
from tools.upstream_merge.contracts import (
    LoadedPlan,
    PLAN_PURPOSE,
    PLAN_SCHEMA,
    REQUIRED_GATE_CATEGORIES,
    _output_layout,
    _validate_gates,
)
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.gitops import (
    EGRESS_MIGRATION_RECEIPT_PATHS,
    commit_tree,
    merge_base,
    protected_objects,
    rev_parse,
    route_snapshot,
    run_egress_snapshot,
    tool_bundle,
    validate_tool_bundle,
)
from tools.upstream_merge.workflow import (
    CONFLICT_INPUT_SCHEMA,
    _apply_client_impact,
    _recompute_merge_start,
    _surface_delta_rows,
    seal_merge,
    start_merge,
)


SOURCE_ROOT = Path(__file__).resolve().parents[3]


def run(repository: Path, *argv: str) -> str:
    completed = subprocess.run(
        list(argv),
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"命令失败 {argv!r}: {completed.stderr or completed.stdout}"
        )
    return completed.stdout.strip()


class SyntheticRepository:
    """只在临时目录构造双分支 Git 图，不接触真实仓库。"""

    def __init__(self, root: Path, *, conflict: bool) -> None:
        self.root = root / "repository"
        self.evidence = root / "evidence"
        self.worktree = root / "isolated-worktree"
        self.root.mkdir()
        self.evidence.mkdir(mode=0o700)
        self.evidence.chmod(0o700)
        run(self.root, "git", "init", "-b", "main")
        run(self.root, "git", "config", "user.name", "Synthetic Test")
        run(self.root, "git", "config", "user.email", "synthetic@example.invalid")
        self._copy_tool_bundle()
        (self.root / "protected.txt").write_text("protected\n", encoding="utf-8")
        (self.root / "conflict.txt").write_text("base\n", encoding="utf-8")
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-m", "base")
        self.base = rev_parse(self.root, "HEAD^{commit}")

        run(self.root, "git", "checkout", "-b", "upstream")
        if conflict:
            (self.root / "conflict.txt").write_text("upstream\n", encoding="utf-8")
        else:
            (self.root / "upstream.txt").write_text("upstream\n", encoding="utf-8")
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-m", "upstream")
        self.upstream = rev_parse(self.root, "HEAD^{commit}")
        run(self.root, "git", "tag", "v1.0.1", self.upstream)

        run(self.root, "git", "checkout", "main")
        if conflict:
            (self.root / "conflict.txt").write_text("fork\n", encoding="utf-8")
        else:
            (self.root / "fork.txt").write_text("fork\n", encoding="utf-8")
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-m", "fork")
        self.fork = rev_parse(self.root, "HEAD^{commit}")
        self.plan = self._plan()

    def _copy_tool_bundle(self) -> None:
        (self.root / "tools").mkdir()
        shutil.copytree(SOURCE_ROOT / "tools/upstream_merge", self.root / "tools/upstream_merge")
        for name in (
            "check_ledger_completeness.py",
            "upstream_merge_plan.schema.json",
            "upstream_merge_request.schema.json",
            "upstream_merge_artifacts.schema.json",
        ):
            shutil.copy2(SOURCE_ROOT / "tools" / name, self.root / "tools" / name)
        shutil.copy2(SOURCE_ROOT / "Makefile", self.root / "Makefile")
        scanner = self.root / "backend/cmd/egressscan"
        scanner.mkdir(parents=True)
        (scanner / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")

    def _plan(self) -> LoadedPlan:
        outputs = _output_layout("v1.0.1")
        document = bind_identity(
            {
                "schema_version": PLAN_SCHEMA,
                "plan_id": "synthetic-v1.0.1",
                "purpose": PLAN_PURPOSE,
                "upstream": {
                    "remote": "upstream",
                    "url": "https://example.invalid/upstream.git",
                    "tag": "v1.0.1",
                    "commit": self.upstream,
                },
                "repository": {
                    "managed_ref": "refs/heads/main",
                    "fork_head": self.fork,
                    "fork_tree": commit_tree(self.root, self.fork),
                    "merge_base": merge_base(self.root, self.fork, self.upstream),
                    "protected_objects": protected_objects(
                        self.root, self.fork, ["protected.txt"]
                    ),
                },
                "workspace": {
                    "worktree": str(self.worktree),
                    "evidence_root": str(self.evidence),
                },
                "official_clients": {},
                "baselines": {},
                "discovery_baseline": {},
                "tool_bundle": tool_bundle(self.root),
                "environment": {},
                "gates": [],
                "outputs": outputs,
            }
        )
        plan_path = self.evidence / "plan.json"
        write_json_once(plan_path, document)
        return LoadedPlan(
            document=document,
            path=plan_path,
            repository_root=self.root,
            evidence_root=self.evidence,
            worktree=self.worktree,
        )

    def cleanup_worktree(self) -> None:
        if self.worktree.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(self.worktree)],
                cwd=self.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


class UpstreamMergeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name)
        self.fixture: SyntheticRepository | None = None

    def tearDown(self) -> None:
        if self.fixture is not None:
            self.fixture.cleanup_worktree()
        self.temporary.cleanup()

    def test_clean_merge_is_sealed_with_exact_parents(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        start = start_merge(self.fixture.plan)
        self.assertEqual(start["status"], "ready_to_seal")
        replayed = _recompute_merge_start(self.fixture.plan, start)
        self.assertEqual(replayed["conflict_paths"], [])
        candidate = seal_merge(self.fixture.plan, None)
        self.assertEqual(candidate["parents"], [self.fixture.fork, self.fixture.upstream])
        self.assertEqual(
            candidate["candidate_tree"],
            commit_tree(self.fixture.root, candidate["merge_commit"]),
        )

    def test_route_snapshot_distinguishes_local_receivers_across_functions(self) -> None:
        repository = self.temp_root / "route-repository"
        routes = repository / "backend/internal/server/routes"
        server = repository / "backend/cmd/server"
        routes.mkdir(parents=True)
        server.mkdir(parents=True)
        (routes / "admin.go").write_text(
            "package routes\n\n"
            "func registerUsers() {\n"
            "\tusers.GET(\"\", first)\n"
            "}\n\n"
            "func registerAffiliates() {\n"
            "\tusers.GET(\"\", second)\n"
            "}\n",
            encoding="utf-8",
        )

        snapshot = route_snapshot(repository, "a" * 40, "b" * 40)

        self.assertEqual(snapshot["entry_count"], 2)
        self.assertEqual(
            sorted(entry["function"] for entry in snapshot["entries"]),
            ["registerAffiliates", "registerUsers"],
        )
        self.assertEqual(
            len({entry["route_fingerprint"] for entry in snapshot["entries"]}),
            2,
        )

    def test_conflict_requires_exact_decision_and_records_resolved_blob(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=True)
        start = start_merge(self.fixture.plan)
        self.assertEqual(start["conflict_paths"], ["conflict.txt"])
        replayed = _recompute_merge_start(self.fixture.plan, start)
        self.assertEqual(replayed["conflict_stages"], start["conflict_stages"])
        forged = {**start, "conflict_paths": []}
        with self.assertRaisesRegex(UpstreamMergeError, "conflict_paths.*独立复算"):
            _recompute_merge_start(self.fixture.plan, forged)
        (self.fixture.worktree / "conflict.txt").write_text("manual\n", encoding="utf-8")
        run(self.fixture.worktree, "git", "add", "conflict.txt")
        with self.assertRaisesRegex(UpstreamMergeError, "必须提供 ConflictResolutionInput"):
            seal_merge(self.fixture.plan, None)
        decision = bind_identity(
            {
                "schema_version": CONFLICT_INPUT_SCHEMA,
                "plan_id": self.fixture.plan.plan_id,
                "plan_identity_sha256": self.fixture.plan.identity,
                "merge_start_sha256": sha256_file(
                    self.fixture.plan.output_path("merge_start")
                ),
                "resolutions": [
                    {
                        "path": "conflict.txt",
                        "resolution": "manual",
                        "rationale": "保留 fork 控制边界并合入上游业务修复",
                    }
                ],
            }
        )
        decision_path = self.temp_root / "conflict-decision.json"
        write_json_once(decision_path, decision)
        candidate = seal_merge(self.fixture.plan, decision_path)
        self.assertEqual(candidate["parents"], [self.fixture.fork, self.fixture.upstream])
        ledger = json.loads(
            self.fixture.plan.output_path("conflict_ledger").read_text(encoding="utf-8")
        )
        self.assertEqual(ledger["conflict_count"], 1)
        self.assertEqual(ledger["resolutions"][0]["resolved_state"]["existence"], "present")

    def test_tool_bundle_drift_fails_closed(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        bundle = self.fixture.plan.document["tool_bundle"]
        (self.fixture.root / "tools/upstream_merge/errors.py").write_text(
            "class UpstreamMergeError(Exception):\n    pass\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(UpstreamMergeError, "工具文件漂移"):
            validate_tool_bundle(self.fixture.root, bundle)

    def test_egress_snapshot_wraps_reviewed_scanner_with_current_git_identity(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        output = self.fixture.evidence / "snapshot/source-to-sink.json"
        original_run_process = upstream_gitops.run_process

        def fake_scan(argv, *, cwd, check):
            if argv[0] != "go":
                return original_run_process(argv, cwd=cwd, check=check)
            self.assertEqual(argv[3:5], ("-mode", "snapshot"))
            receipt_flag = argv.index("-migration-receipts")
            self.assertEqual(
                argv[receipt_flag + 1],
                ",".join(
                    str(self.fixture.root / relative)
                    for relative in EGRESS_MIGRATION_RECEIPT_PATHS
                ),
            )
            out_flag = argv.index("-out")
            raw_output = Path(argv[out_flag + 1])
            raw_output.write_text(
                json.dumps(
                    {
                        "bootstrap_commit": "0" * 40,
                        "scan_pattern": "example.invalid/...",
                        "build_contexts": ["linux/amd64"],
                        "packages_loaded": 1,
                        "sinks": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")

        with mock.patch(
            "tools.upstream_merge.gitops.run_process",
            side_effect=fake_scan,
        ):
            run_egress_snapshot(self.fixture.root, output)
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["source_commit"], self.fixture.fork)
        self.assertEqual(
            snapshot["source_tree"],
            commit_tree(self.fixture.root, self.fixture.fork),
        )
        self.assertEqual(snapshot["schema_version"], "official-egress-upstream-source-to-sink-snapshot/v1")

    def test_path_escape_is_rejected(self) -> None:
        root = self.temp_root / "private"
        root.mkdir(mode=0o700)
        with self.assertRaisesRegex(UpstreamMergeError, "根内规范相对路径"):
            resolve_within(root, "../escape.json", "escape")

    def test_gate_categories_must_be_exact_and_unique(self) -> None:
        gates = [
            {
                "id": f"gate-{index:02d}",
                "category": category,
                "mode": "command",
                "cwd": ".",
                "argv": ["true"],
            }
            for index, category in enumerate(REQUIRED_GATE_CATEGORIES)
        ]
        _validate_gates(gates)
        gates[-1]["category"] = gates[0]["category"]
        with self.assertRaisesRegex(UpstreamMergeError, "恰好覆盖"):
            _validate_gates(gates)

    def test_line_hint_does_not_create_fake_surface_delta(self) -> None:
        before = {
            "sink": {
                "scan_candidate_id": "sink",
                "persona": "codex",
                "runtime_sink_id": "codex.responses",
                "line_hint": 10,
            }
        }
        after = {
            "sink": {
                "scan_candidate_id": "sink",
                "persona": "codex",
                "runtime_sink_id": "codex.responses",
                "line_hint": 99,
            }
        }
        self.assertEqual(_surface_delta_rows(before, after, "egress"), [])

    def test_protocol_semantics_change_requires_both_successor_campaigns(self) -> None:
        impacts = {"claude": False, "codex": False}
        campaigns = {"claude": False, "codex": False}
        shared = _apply_client_impact(
            {"protocol_adapter"},
            True,
            impacts,
            campaigns,
        )
        self.assertFalse(shared)
        self.assertEqual(impacts, {"claude": True, "codex": True})
        self.assertEqual(campaigns, {"claude": True, "codex": True})

    def test_plan_schema_keeps_v1_and_v2(self) -> None:
        schema = json.loads(
            (SOURCE_ROOT / "tools/upstream_merge_plan.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["$defs"]["v1"]["properties"]["schema_version"]["const"],
            "official-egress-upstream-merge-plan/v1",
        )
        self.assertEqual(
            schema["$defs"]["v2"]["properties"]["schema_version"]["const"],
            PLAN_SCHEMA,
        )


if __name__ == "__main__":
    unittest.main()
