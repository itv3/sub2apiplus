"""完整计划、隔离合并和失败关闭边界的合成测试。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge import gitops as upstream_gitops
from tools.upstream_merge.canonical import (
    artifact_binding,
    bind_identity,
    file_binding,
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
    next_stage_path,
    revision_number,
    stage_paths,
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
    CHANGE_DECISION_INPUT_SCHEMA,
    CHANGE_DECISION_RECEIPT_SCHEMA,
    CLIENT_GATE_RECEIPT_SCHEMA,
    SOURCE_CANDIDATE_SCHEMA,
    SURFACE_DELTA_SCHEMA,
    _auto_classification,
    _apply_client_impact,
    _component_ownership,
    _infer_conflict_resolution,
    _install_frontend_dependencies_offline,
    _suggest_categories,
    _gate_groups,
    _load_surface_delta,
    _recompute_merge_start,
    _surface_delta_rows,
    generate_source_transition,
    load_verification_receipt,
    run_verification_gates,
    scan_surfaces,
    seal_merge,
    start_merge,
    validate_source_transition,
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

    def __init__(self, root: Path, *, conflict: bool, extra_fork_files: dict[str, str] | None = None) -> None:
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
        for relative, content in (extra_fork_files or {}).items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
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


def build_verification_plan(
    fixture: SyntheticRepository,
    marker: Path,
    *,
    grouped: bool = True,
) -> LoadedPlan:
    """为 U-4 测试构造最小但可完整复核的计划和前置制品。"""

    evidence = fixture.evidence
    overlay = evidence / "u2" / "overlay.json"
    write_json_once(overlay, {"overlay": "stable"})
    source_document = bind_identity(
        {
            "schema_version": SOURCE_CANDIDATE_SCHEMA,
            "plan_id": fixture.plan.plan_id,
            "plan_identity_sha256": fixture.plan.identity,
            "merge_candidate": {"path": "u1/merge-candidate.json", "sha256": "0" * 64, "bytes": 0},
            "source_commit": fixture.fork,
            "source_tree": commit_tree(fixture.root, fixture.fork),
            "changed_paths": [],
            "source_change_input": None,
            "codex_overlay_ledger": {
                "path": "u2/overlay.json",
                "sha256": sha256_file(overlay),
                "bytes": overlay.stat().st_size,
            },
        }
    )
    write_json_once(evidence / "u2" / "source-candidate.json", source_document)
    impact_matrix_path = evidence / "u3" / "impact-matrix.json"
    write_json_once(impact_matrix_path, {"matrix": "stable"})
    decision_path = fixture.evidence.parent / "change-decision.json"
    write_json_once(decision_path, {"schema_version": CHANGE_DECISION_INPUT_SCHEMA})
    impact_document = bind_identity(
        {
            "schema_version": CHANGE_DECISION_RECEIPT_SCHEMA,
            "plan_id": fixture.plan.plan_id,
            "plan_identity_sha256": fixture.plan.identity,
            "impact_matrix": artifact_binding(evidence, impact_matrix_path),
            "change_decision": file_binding(decision_path),
            "file_decision_count": 1,
            "surface_decision_count": 0,
            "client_impacts": {"claude": False, "codex": False},
            "successor_campaign_required": {"claude": False, "codex": False},
            "shared_contract_required": False,
            "unclassified_count": 0,
            "official_client_identity_change_count": 0,
            "result": "closed",
        }
    )
    write_json_once(evidence / "u3" / "change-decision-receipt.json", impact_document)

    failing_argv = [
        "python3",
        "-c",
        f"import pathlib,sys; sys.exit(1 if pathlib.Path({str(marker)!r}).exists() else 0)",
    ]
    gates: list[dict[str, object]] = []
    for index, category in enumerate(REQUIRED_GATE_CATEGORIES):
        argv = failing_argv if index == 0 else ["python3", "-c", "pass"]
        gate: dict[str, object] = {
            "id": f"gate-{index:02d}",
            "category": category,
            "mode": "command",
            "cwd": ".",
            "argv": argv,
        }
        if grouped and index in (0, 1):
            gate["execution_group"] = "client-wire-group"
            gate["argv"] = failing_argv
        gates.append(gate)
    document = dict(fixture.plan.document)
    document["gates"] = gates
    return LoadedPlan(
        document=document,
        path=fixture.plan.path,
        repository_root=fixture.root,
        evidence_root=fixture.evidence,
        worktree=fixture.root,
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

    def test_gates_first_run_groups_commands_and_writes_client_receipts(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        marker = self.temp_root / "gate-failure.marker"
        marker.write_text("fail\n", encoding="utf-8")
        plan = build_verification_plan(self.fixture, marker)

        receipt = run_verification_gates(plan, "attempt-001")

        self.assertEqual(receipt["result"], "blocked")
        self.assertEqual(receipt["failed_gate_ids"], ["gate-00", "gate-01"])
        self.assertEqual(receipt["execution_group_count"], 11)
        self.assertEqual(receipt["executed_gate_count"], 11)
        self.assertEqual(set(receipt["client_receipts"]), {
            "claude_active_wire",
            "claude_ingress_matrix",
            "claude_rollback_wire",
            "codex_active_wire",
            "codex_ingress_matrix",
            "codex_rollback_wire",
        })
        loaded = load_verification_receipt(
            plan,
            self.fixture.evidence / "u4/attempts/attempt-001/receipt.json",
            require_passed=False,
        )
        self.assertEqual(loaded["result"], "blocked")

    def test_gates_retry_only_failed_group_and_reuses_passed_receipts(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        marker = self.temp_root / "gate-failure.marker"
        marker.write_text("fail\n", encoding="utf-8")
        plan = build_verification_plan(self.fixture, marker)
        run_verification_gates(plan, "attempt-001")
        marker.unlink()

        receipt = run_verification_gates(
            plan,
            "attempt-002",
            from_attempt="attempt-001",
        )

        self.assertEqual(receipt["result"], "passed")
        self.assertEqual(receipt["failed_gate_ids"], [])
        self.assertEqual(receipt["execution_group_count"], 1)
        self.assertEqual(receipt["executed_gate_count"], 1)
        self.assertEqual(receipt["reused_gate_count"], 10)
        loaded = load_verification_receipt(
            plan,
            self.fixture.evidence / "u4/attempts/attempt-002/receipt.json",
            require_passed=True,
        )
        self.assertEqual(loaded["result"], "passed")

    def test_gates_require_frontend_dependencies_matching_lockfile(self) -> None:
        # UM-16：候选树带前端锁文件时，依赖缺失或与锁文件不一致都在开跑前拒绝，不留 attempt 目录。
        lockfile = "lockfileVersion: '9.0'\n"
        self.fixture = SyntheticRepository(
            self.temp_root,
            conflict=False,
            extra_fork_files={"frontend/pnpm-lock.yaml": lockfile, "frontend/.gitignore": "node_modules/\n"},
        )
        marker = self.temp_root / "gate-failure.marker"
        marker.write_text("fail\n", encoding="utf-8")
        plan = build_verification_plan(self.fixture, marker)
        attempt = self.fixture.evidence / "u4/attempts/attempt-001"

        with self.assertRaisesRegex(UpstreamMergeError, "未安装前端依赖"):
            run_verification_gates(plan, "attempt-001")
        self.assertFalse(attempt.exists())

        current = self.fixture.root / "frontend/node_modules/.pnpm/lock.yaml"
        current.parent.mkdir(parents=True)
        current.write_text("lockfileVersion: '8.0'\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "不一致"):
            run_verification_gates(plan, "attempt-001")
        self.assertFalse(attempt.exists())

        current.write_text(lockfile, encoding="utf-8")
        receipt = run_verification_gates(plan, "attempt-001")
        self.assertEqual(receipt["failed_gate_ids"], ["gate-00", "gate-01"])
        self.assertTrue((attempt / "receipt.json").is_file())

    def test_rerun_worktree_installs_frontend_dependencies_offline(self) -> None:
        # replay --rerun-gates 的临时执行树：有锁文件才离线安装，失败时给出原因，不联网。
        tree = self.temp_root / "rerun-tree"
        tree.mkdir()
        with mock.patch("tools.upstream_merge.workflow.run_process") as process:
            _install_frontend_dependencies_offline(tree)
            process.assert_not_called()
        (tree / "frontend").mkdir()
        (tree / "frontend/pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        with mock.patch("tools.upstream_merge.workflow.run_process") as process:
            process.return_value = subprocess.CompletedProcess([], 0, "", "")
            _install_frontend_dependencies_offline(tree)
            argv = process.call_args.args[0]
            self.assertEqual(argv[-3:], ("install", "--frozen-lockfile", "--offline"))
        with mock.patch("tools.upstream_merge.workflow.run_process") as process:
            process.return_value = subprocess.CompletedProcess([], 1, "", "ERR_PNPM_NO_OFFLINE_TARBALL")
            with self.assertRaisesRegex(UpstreamMergeError, "离线安装前端依赖"):
                _install_frontend_dependencies_offline(tree)
        with mock.patch("tools.upstream_merge.workflow.run_process", side_effect=FileNotFoundError("pnpm")):
            with self.assertRaisesRegex(UpstreamMergeError, "离线安装前端依赖"):
                _install_frontend_dependencies_offline(tree)

    def test_gates_reject_incomplete_retry_without_leaving_attempt_directory(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        marker = self.temp_root / "gate-failure.marker"
        marker.write_text("fail\n", encoding="utf-8")
        plan = build_verification_plan(self.fixture, marker)
        run_verification_gates(plan, "attempt-001")

        with self.assertRaisesRegex(UpstreamMergeError, "未覆盖上一 attempt"):
            run_verification_gates(
                plan,
                "attempt-002",
                only="gate-02",
                from_attempt="attempt-001",
            )
        self.assertFalse((self.fixture.evidence / "u4/attempts/attempt-002").exists())

    def test_gate_execution_group_definition_must_match_exactly(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        marker = self.temp_root / "gate-failure.marker"
        plan = build_verification_plan(self.fixture, marker)
        plan.document["gates"][1]["argv"] = ["python3", "-c", "different"]

        with self.assertRaisesRegex(UpstreamMergeError, "门禁定义不一致"):
            _gate_groups(plan)

    def test_new_receipt_without_client_receipts_is_rejected(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        marker = self.temp_root / "gate-failure.marker"
        plan = build_verification_plan(self.fixture, marker)
        run_verification_gates(plan, "attempt-001")
        receipt_path = self.fixture.evidence / "u4/attempts/attempt-001/receipt.json"
        document = json.loads(receipt_path.read_text(encoding="utf-8"))
        document.pop("client_receipts")
        receipt_path.write_text(
            json.dumps(bind_identity(document), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(UpstreamMergeError, "六类 client_receipts"):
            load_verification_receipt(plan, receipt_path, require_passed=False)

    def test_legacy_receipt_can_be_read_after_old_relative_executable_is_removed(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        marker = self.temp_root / "gate-failure.marker"
        plan = build_verification_plan(self.fixture, marker)
        executable = self.temp_root / "legacy-gate"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        for index in (0, 1):
            plan.document["gates"][index]["argv"] = [str(executable)]
        run_verification_gates(plan, "attempt-001")
        receipt_path = self.fixture.evidence / "u4/attempts/attempt-001/receipt.json"
        document = json.loads(receipt_path.read_text(encoding="utf-8"))
        for field in (
            "client_receipts",
            "executed_gate_count",
            "reused_gate_count",
            "execution_group_count",
            "from_attempt",
            "selected_gate_ids",
        ):
            document.pop(field, None)
        for gate in document["gates"]:
            for field in ("execution_group", "execution_leader_id", "execution_status", "reused_from"):
                gate.pop(field, None)
        receipt_path.write_text(
            json.dumps(bind_identity(document), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        executable.unlink()
        loaded = load_verification_receipt(plan, receipt_path, require_passed=True)
        self.assertEqual(loaded["result"], "passed")

    def test_attempt_reuse_must_bind_every_gate_to_top_level_attempt(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        marker = self.temp_root / "gate-failure.marker"
        marker.write_text("fail\n", encoding="utf-8")
        plan = build_verification_plan(self.fixture, marker)
        run_verification_gates(plan, "attempt-001")
        marker.unlink()
        run_verification_gates(plan, "attempt-002", from_attempt="attempt-001")
        source_receipt = self.fixture.evidence / "u4/attempts/attempt-001/receipt.json"
        alternate = self.fixture.evidence / "u4/attempts/attempt-003"
        alternate.mkdir(parents=True)
        alternate_receipt = alternate / "receipt.json"
        alternate_receipt.write_bytes(source_receipt.read_bytes())
        receipt_path = self.fixture.evidence / "u4/attempts/attempt-002/receipt.json"
        document = json.loads(receipt_path.read_text(encoding="utf-8"))
        alternate_binding = artifact_binding(self.fixture.evidence, alternate_receipt)
        for gate in document["gates"]:
            if gate.get("execution_status") == "attempt_reused":
                gate["reused_from"] = alternate_binding
                break
        receipt_path.write_text(
            json.dumps(bind_identity(document), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(UpstreamMergeError, "top-level from_attempt"):
            load_verification_receipt(plan, receipt_path, require_passed=True)

    def test_revision_chain_rejects_gap_and_keeps_legacy_compatibility(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        plan = self.fixture.plan
        legacy = plan.output_path("source_candidate")
        write_json_once(legacy, {"revision": 1})
        self.assertEqual(stage_paths(plan, "source_candidate"), [legacy])
        number, next_path = next_stage_path(plan, "source_candidate")
        self.assertEqual(number, 2)
        write_json_once(next_path, {"revision": 2})
        self.assertEqual(revision_number(next_path, plan, "source_candidate"), 2)
        gap_path = plan.evidence_root / "u2/source-candidates/source-candidate-004.json"
        write_json_once(gap_path, {"revision": 4})
        with self.assertRaisesRegex(UpstreamMergeError, "链存在缺口"):
            stage_paths(plan, "source_candidate")

    def test_revision_two_rejects_legacy_dependent_surface_artifact(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        plan = build_verification_plan(self.fixture, self.temp_root / "marker")
        source_path = plan.output_path("source_candidate")
        source = json.loads(source_path.read_text(encoding="utf-8"))
        _, revision_path = next_stage_path(plan, "source_candidate")
        source["revision"] = 2
        source["predecessor"] = artifact_binding(plan.evidence_root, source_path)
        revision_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_once(revision_path, bind_identity(source))
        delta_path = plan.output_path("surface_delta")
        delta = bind_identity(
            {
                "schema_version": SURFACE_DELTA_SCHEMA,
                "plan_id": plan.plan_id,
                "plan_identity_sha256": plan.identity,
                "source_candidate": {"path": "u2/source-candidate.json", "sha256": "0" * 64, "bytes": 0},
                "baseline_route_snapshot": {"path": "u0/route-snapshot.json", "sha256": "0" * 64, "bytes": 0},
                "candidate_route_snapshot": {"path": "u2/route-snapshot.json", "sha256": "0" * 64, "bytes": 0},
                "baseline_source_to_sink_snapshot": {"path": "u0/source-to-sink-snapshot.json", "sha256": "0" * 64, "bytes": 0},
                "candidate_source_to_sink_snapshot": {"path": "u2/source-to-sink-snapshot.json", "sha256": "0" * 64, "bytes": 0},
                "route_delta_count": 0,
                "egress_delta_count": 0,
                "deltas": [],
            }
        )
        write_json_once(delta_path, delta)
        with self.assertRaisesRegex(UpstreamMergeError, "缺少 revision/predecessor metadata"):
            _load_surface_delta(plan)

    def test_component_mapping_only_allows_known_low_risk_paths_to_auto_classify(self) -> None:
        known = _component_ownership("frontend/src/example.ts")
        unknown = _component_ownership("new-area/example.go")
        self.assertEqual(known["status"], "known")
        self.assertEqual(unknown["status"], "unknown")
        self.assertTrue(
            _auto_classification(
                {
                    "component_ownership": known,
                    "risk_hints": [],
                    "suggested_categories": ["repository_support"],
                }
            )["eligible"]
        )
        self.assertFalse(
            _auto_classification(
                {
                    "component_ownership": unknown,
                    "risk_hints": [],
                    "suggested_categories": ["out_of_scope_product"],
                }
            )["eligible"]
        )

    def test_source_transition_generates_and_validates_append_only_chain(self) -> None:
        repository = self.temp_root / "transition-repository"
        repository.mkdir()
        run(repository, "git", "init", "-b", "main")
        run(repository, "git", "config", "user.name", "Transition Test")
        run(repository, "git", "config", "user.email", "transition@example.invalid")
        (repository / "old.txt").write_text("old\n", encoding="utf-8")
        run(repository, "git", "add", "old.txt")
        run(repository, "git", "commit", "-m", "base")
        base = rev_parse(repository, "HEAD^{commit}")
        (repository / "old.txt").rename(repository / "renamed.txt")
        run(repository, "git", "add", "-A")
        run(repository, "git", "commit", "-m", "rename")
        renamed = rev_parse(repository, "HEAD^{commit}")
        first_path = self.temp_root / "transition-001.json"

        first = generate_source_transition(repository, base, renamed, first_path)
        self.assertEqual(first["chain_sequence"], 1)
        self.assertEqual(first["entries"][0]["status"], "R")
        self.assertEqual(validate_source_transition(repository, first_path)["result"], "valid")

        (repository / "renamed.txt").unlink()
        run(repository, "git", "add", "-A")
        run(repository, "git", "commit", "-m", "delete")
        deleted = rev_parse(repository, "HEAD^{commit}")
        second_path = self.temp_root / "transition-002.json"
        second = generate_source_transition(
            repository,
            renamed,
            deleted,
            second_path,
            predecessor_register=first_path,
        )
        self.assertEqual(second["chain_sequence"], 2)
        self.assertEqual(second["entries"][0]["status"], "D")
        self.assertEqual(validate_source_transition(repository, second_path)["result"], "valid")

    def test_source_transition_v3_issued_at_and_v2_read_only(self) -> None:
        # UM-17：新生成的收据是 v3 且写明签发时间；v2 历史收据不带签发时间，照常只读校验。
        repository = self.temp_root / "transition-issued-repository"
        repository.mkdir()
        run(repository, "git", "init", "-b", "main")
        run(repository, "git", "config", "user.name", "Transition Test")
        run(repository, "git", "config", "user.email", "transition@example.invalid")
        (repository / "value.txt").write_text("one\n", encoding="utf-8")
        run(repository, "git", "add", "value.txt")
        run(repository, "git", "commit", "-m", "base")
        base = rev_parse(repository, "HEAD^{commit}")
        (repository / "value.txt").write_text("two\n", encoding="utf-8")
        run(repository, "git", "commit", "-am", "change")
        changed = rev_parse(repository, "HEAD^{commit}")
        generated = generate_source_transition(repository, base, changed)
        self.assertEqual(generated["schema_version"], "official-egress-upstream-source-transition/v3")
        self.assertRegex(generated["issued_at_utc"], r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00Z$")

        def write(name: str, document: dict) -> Path:
            path = self.temp_root / name
            body = {key: value for key, value in document.items() if key != "identity_sha256"}
            path.write_text(json.dumps(bind_identity(body), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return path

        self.assertEqual(validate_source_transition(repository, write("v3.json", generated))["result"], "valid")
        missing = {key: value for key, value in generated.items() if key != "issued_at_utc"}
        with self.assertRaisesRegex(UpstreamMergeError, "字段"):
            validate_source_transition(repository, write("v3-missing.json", missing))
        with self.assertRaisesRegex(UpstreamMergeError, "晚于当前时刻"):
            validate_source_transition(repository, write("v3-future.json", {**generated, "issued_at_utc": "2999-01-01T00:00:00Z"}))
        with self.assertRaisesRegex(UpstreamMergeError, "UTC 时间"):
            validate_source_transition(repository, write("v3-format.json", {**generated, "issued_at_utc": "2026-10-01 00:00:00"}))
        legacy = {**missing, "schema_version": "official-egress-upstream-source-transition/v2"}
        self.assertEqual(validate_source_transition(repository, write("v2.json", legacy))["result"], "valid")
        with self.assertRaisesRegex(UpstreamMergeError, "字段"):
            validate_source_transition(repository, write("v2-issued.json", {**legacy, "issued_at_utc": generated["issued_at_utc"]}))

    def test_source_transition_rejects_symlink_and_impossible_status_sha(self) -> None:
        repository = self.temp_root / "transition-symlink-repository"
        repository.mkdir()
        run(repository, "git", "init", "-b", "main")
        run(repository, "git", "config", "user.name", "Transition Test")
        run(repository, "git", "config", "user.email", "transition@example.invalid")
        (repository / "value.txt").write_text("one\n", encoding="utf-8")
        run(repository, "git", "add", "value.txt")
        run(repository, "git", "commit", "-m", "base")
        base = rev_parse(repository, "HEAD^{commit}")
        (repository / "value.txt").write_text("two\n", encoding="utf-8")
        run(repository, "git", "add", "value.txt")
        run(repository, "git", "commit", "-m", "change")
        changed = rev_parse(repository, "HEAD^{commit}")
        first_path = self.temp_root / "transition-safe-001.json"
        generate_source_transition(repository, base, changed, first_path)
        link_path = self.temp_root / "transition-link.json"
        os.symlink(first_path, link_path)
        with self.assertRaisesRegex(UpstreamMergeError, "可信普通文件"):
            validate_source_transition(repository, link_path)
        (repository / "value.txt").write_text("three\n", encoding="utf-8")
        run(repository, "git", "add", "value.txt")
        run(repository, "git", "commit", "-m", "change-again")
        changed_again = rev_parse(repository, "HEAD^{commit}")
        second_path = self.temp_root / "transition-safe-002.json"
        with self.assertRaisesRegex(UpstreamMergeError, "普通文件"):
            generate_source_transition(
                repository,
                changed,
                changed_again,
                second_path,
                predecessor_register=link_path,
            )

        forged = json.loads(first_path.read_text(encoding="utf-8"))
        forged["entries"][0]["status"] = "A"
        first_path.write_text(
            json.dumps(bind_identity(forged), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(UpstreamMergeError, "新增文件必须仅有 current_sha256"):
            validate_source_transition(repository, first_path)

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

    def test_surface_scan_failure_does_not_leave_partial_revision_snapshots(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        plan = build_verification_plan(self.fixture, self.temp_root / "marker")
        routes = self.fixture.root / "backend/internal/server/routes"
        server = self.fixture.root / "backend/cmd/server"
        routes.mkdir(parents=True)
        server.mkdir(parents=True)
        route_baseline = route_snapshot(
            self.fixture.root,
            self.fixture.fork,
            commit_tree(self.fixture.root, self.fixture.fork),
        )
        egress_baseline = {
            "schema_version": "official-egress-upstream-source-to-sink-snapshot/v1",
            "source_commit": self.fixture.fork,
            "source_tree": commit_tree(self.fixture.root, self.fixture.fork),
            "scan_pattern": "example.invalid/...",
            "build_contexts": [],
            "packages_loaded": 0,
            "sink_count": 0,
            "sinks": [],
        }
        route_path = plan.evidence_root / "u0/route-snapshot.json"
        egress_path = plan.evidence_root / "u0/source-to-sink-snapshot.json"
        write_json_once(route_path, route_baseline)
        write_json_once(egress_path, egress_baseline)
        plan.document["discovery_baseline"] = {
            "route_snapshot": artifact_binding(plan.evidence_root, route_path),
            "source_to_sink_snapshot": artifact_binding(plan.evidence_root, egress_path),
        }
        with mock.patch(
            "tools.upstream_merge.workflow.run_egress_snapshot",
            side_effect=UpstreamMergeError("模拟 scanner 失败"),
        ):
            with self.assertRaisesRegex(UpstreamMergeError, "模拟 scanner 失败"):
                scan_surfaces(plan)
        self.assertFalse(plan.output_path("surface_route_snapshot").exists())
        self.assertFalse(plan.output_path("surface_egress_snapshot").exists())
        self.assertFalse(plan.output_path("surface_delta").exists())

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

    def test_final_symlink_is_rejected_before_resolution(self) -> None:
        root = self.temp_root / "private-symlink"
        root.mkdir(mode=0o700)
        target = root / "target.json"
        target.write_text("{}\n", encoding="utf-8")
        os.symlink(target, root / "alias.json")
        with self.assertRaisesRegex(UpstreamMergeError, "路径包含符号链接"):
            resolve_within(root, "alias.json", "alias")

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

    def test_artifact_schema_local_refs_are_closed(self) -> None:
        schema = json.loads(
            (SOURCE_ROOT / "tools/upstream_merge_artifacts.schema.json").read_text(
                encoding="utf-8"
            )
        )
        definitions = schema.get("$defs", {})
        missing: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                reference = value.get("$ref")
                if (
                    isinstance(reference, str)
                    and reference.startswith("#/$defs/")
                    and reference.removeprefix("#/$defs/") not in definitions
                ):
                    missing.add(reference)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(schema)
        self.assertEqual(missing, set())



class SuggestionSelfConsistencyTests(unittest.TestCase):
    """建议端产出必须能通过校验端的同一组要求。"""

    CARRIERS = {"claude_persona", "codex_persona", "protocol_adapter", "shared_control"}

    def test_wire_selector_hints_always_get_a_carrier(self) -> None:
        # ChangeDecision 校验要求 wire/selector 提示被四类之一承接；
        # 建议端若不产生，工具生成的草稿在自身校验下天然不合规。
        paths = [
            "backend/ent/client.go",
            "backend/ent/proxy/where.go",
            "backend/go.mod",
            "frontend/src/api/admin/accounts.ts",
            "deploy/docker-compose.yml",
            "backend/internal/handler/gateway_handler.go",
            "backend/internal/service/account.go",
            "backend/internal/server/routes/admin.go",
            "docs/egress/maintenance/example.json",
        ]
        for path in paths:
            for hint in ("wire", "selector"):
                with self.subTest(path=path, hint=hint):
                    categories = set(_suggest_categories(path, [hint]))
                    self.assertTrue(
                        categories & self.CARRIERS,
                        f"{path} 带 {hint} 提示却没有承接类别：{sorted(categories)}",
                    )

    def test_key_group_hints_keep_their_carrier(self) -> None:
        for hint in ("account", "billing", "group", "key", "quota_usage", "route"):
            with self.subTest(hint=hint):
                self.assertIn(
                    "key_group_routing_billing",
                    _suggest_categories("backend/internal/repository/proxy_repo.go", [hint]),
                )

    def test_no_hint_keeps_previous_classification(self) -> None:
        # 无 wire/selector 提示时不得凭空引入承接类别。
        self.assertEqual(
            _suggest_categories("README_CN.md", []),
            ["out_of_scope_product"],
        )


class ConflictResolutionInferenceTests(unittest.TestCase):
    """merge-seal 已能从 index 事实推断处置类型，拒绝时必须说出来。"""

    def test_infers_fork_upstream_and_manual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = SyntheticRepository(Path(directory), conflict=True)
            start = start_merge(repo.plan)
            worktree = repo.worktree
            stages = start["conflict_stages"]
            path = "conflict.txt"

            # 冲突一旦 add 就没有 stage 2/3 可 checkout，直接按两侧内容写入 index。
            for content, expected in (
                ("fork\n", "fork"),
                ("upstream\n", "upstream"),
                ("hand merged\n", "manual"),
            ):
                with self.subTest(expected=expected):
                    (worktree / path).write_text(content, encoding="utf-8")
                    run(worktree, "git", "add", "--", path)
                    self.assertEqual(
                        _infer_conflict_resolution(worktree, stages, path), expected
                    )


if __name__ == "__main__":
    unittest.main()
