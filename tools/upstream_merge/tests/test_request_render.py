"""request-render（UM-18）的合成仓库测试：catalog、发布图、上游 tag 链与前序 Plan 证据都在临时目录里构造。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.preflight_report import (
    OFFICIAL_EGRESS_ROOT,
    REQUEST_TEMPLATE_RELATIVE,
    load_request_template,
    template_validity,
)
from tools.upstream_merge.request_render import INVENTORY_FILES, FINALIZE_RECEIPT, render_request

SOURCE_ROOT = Path(__file__).resolve().parents[3]
SOURCE_TREE = "local-analysis/sources/codex-cli-0.149.1"


def git(root: Path, *argv: str) -> str:
    completed = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git {argv!r} 失败：{completed.stderr}")
    return completed.stdout.strip()


def write_json(path: Path, document: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    path.write_text(raw, encoding="utf-8")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RequestRenderTest(unittest.TestCase):
    """main 上有 fork 提交；上游线 base → v1.0.1 → v1.0.2 → v1.1.0；前序 Plan 已走完 U-6。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "repository"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Synthetic Test")
        git(self.root, "config", "user.email", "synthetic@example.invalid")
        (self.root / ".gitignore").write_text("/local-analysis/\n", encoding="utf-8")
        (self.root / SOURCE_TREE).mkdir(parents=True)
        template = self.root / REQUEST_TEMPLATE_RELATIVE
        template.parent.mkdir(parents=True)
        shutil.copy2(SOURCE_ROOT / REQUEST_TEMPLATE_RELATIVE, template)
        self._write_catalogs(active_version="1.2.3", previous_version="1.2.2")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", "-b", "upstream")
        self.tags: dict[str, str] = {}
        for tag in ("v1.0.1", "v1.0.2", "v1.1.0"):
            (self.root / f"{tag}.txt").write_text(tag, encoding="utf-8")
            git(self.root, "add", "--all")
            git(self.root, "commit", "-q", "-m", tag)
            self.tags[tag] = git(self.root, "rev-parse", "HEAD")
            git(self.root, "tag", tag)
        git(self.root, "checkout", "-q", "main")
        (self.root / "fork.txt").write_text("fork", encoding="utf-8")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "fork")
        git(self.root, "tag", "v1.0.0-1")
        self.plans = self.tmp / "plans"
        self.previous = self._finished_plan("v1.0.0-20260901-001")
        self.baseline = self.plans / "baseline-20261004-0000" / "evidence" / "BaselineAcceptance.json"
        write_json(self.baseline, {"result": "accepted"})
        self.plan_root = self.plans / "v1.0.2-20261004-001"

    def _write_catalogs(self, *, active_version: str, previous_version: str) -> None:
        egress = self.root / OFFICIAL_EGRESS_ROOT
        claude_profile = "catalogdata/claude/profiles/9.9.9/profile.json"
        claude_sha = write_json(egress / claude_profile, {"profile": "claude"})
        rollback = "docs/egress/maintenance/claude-rollback-receipt.json"
        rollback_sha = write_json(self.root / rollback, {"receipt": "rollback"})
        write_json(egress / "catalogdata/claude/release-catalog.json", {
            "releases": [{"version": "9.9.9", "release_sha256": "c" * 64, "profile": {"path": claude_profile, "sha256": claude_sha}}],
            "selectors": {
                "production_active": {"kind": "release", "release_sha256": "c" * 64},
                "production_rollback": {"kind": "operational-deployment", "deployment": {"receipt": {"path": rollback, "sha256": rollback_sha}}},
            },
        })
        runtime = egress / "catalogdata/runtime"
        snapshots = []
        self.codex_digests: dict[str, str] = {}
        for version in (previous_version, active_version):
            file = f"profiles/{version}/profile.json"
            digest = write_json(runtime / file, {"profile": f"codex-{version}"})
            self.codex_digests[version] = digest
            snapshots.append({"version": version, "digest": digest, "blob_sha256": digest, "file": file})
        catalog_sha = write_json(runtime / "snapshot-catalogs/catalog.json", {"schema_version": 1, "snapshots": snapshots})
        nodes = [
            {"mode": mode, "purpose": purpose, "snapshot": {"version": version, "digest": self.codex_digests[version]}}
            for purpose in ("openai_oauth_responses_http", "openai_oauth_responses_ws")
            for mode, version in (("active", active_version), ("previous", previous_version))
        ]
        graph_sha = write_json(runtime / "release-graphs/graph.json", {"schema_version": 1, "nodes": nodes})
        write_json(runtime / "release-catalog.json", {
            "schema_version": 1,
            "release_graph": {"path": "catalogdata/runtime/release-graphs/graph.json", "sha256": graph_sha},
            "snapshot_catalog": {"path": "catalogdata/runtime/snapshot-catalogs/catalog.json", "sha256": catalog_sha},
        })

    def _finished_plan(self, name: str) -> Path:
        evidence = self.plans / name / "evidence"
        for relative in INVENTORY_FILES:
            write_json(evidence / relative, {"inventory": relative})
        write_json(evidence / FINALIZE_RECEIPT, {"result": "finalized"})
        return evidence

    def _render(self, **overrides):
        arguments = {
            "plan_root": self.plan_root,
            "upstream_tag": "v1.0.2",
            "baseline_acceptance": self.baseline,
        }
        arguments.update(overrides)
        return render_request(self.root, **arguments)

    def _request(self) -> dict:
        return json.loads((self.plan_root / "inputs" / "request.json").read_text(encoding="utf-8"))

    def test_renders_request_runtime_state_and_recovery_point(self) -> None:
        result = self._render()
        head = git(self.root, "rev-parse", "HEAD")
        self.assertEqual(result["plan_id"], "sub2api-v1.0.2-merge-20261004-001")
        request = self._request()
        self.assertEqual(request["upstream"]["tag"], "v1.0.2")
        self.assertEqual(request["upstream"]["commit"], self.tags["v1.0.2"])
        # 区间只含 merge-base 之后到目标的版本 tag：fork 自己的发版 tag 与目标之后的 v1.1.0 都不进来。
        self.assertEqual([item["tag"] for item in request["upstream"]["covered_tags"]], ["v1.0.1", "v1.0.2"])
        egress = self.root / OFFICIAL_EGRESS_ROOT
        clients = request["official_clients"]
        self.assertEqual(clients["codex"]["target_version"], "1.2.3")
        self.assertEqual(clients["codex"]["active_path"], str(egress / "catalogdata/runtime/profiles/1.2.3/profile.json"))
        self.assertEqual(clients["codex"]["rollback_path"], str(egress / "catalogdata/runtime/profiles/1.2.2/profile.json"))
        self.assertEqual(clients["claude"]["target_version"], "9.9.9")
        self.assertEqual(request["baselines"]["production_ingress_inventory"]["codex"], str(self.previous / INVENTORY_FILES[1]))
        self.assertEqual(request["baselines"]["baseline_acceptance_path"], str(self.baseline))
        self.assertEqual(request["workspace"]["worktree"], str(self.plan_root / "worktree"))
        # 只读源码根在生成时渲染成主仓库；扫描候选的 {repository} 留给 gates-run。
        argv = [item for gate in request["gates"] for item in gate["argv"]]
        self.assertIn(f"CODEX_0_149_1_SOURCE_ROOT={self.root}/{SOURCE_TREE}", argv)
        self.assertIn("{repository}", argv)
        self.assertNotIn("{source_repository}", json.dumps(request))
        runtime = json.loads((self.plan_root / "inputs" / "runtime-state.json").read_text(encoding="utf-8"))
        recovery = json.loads((self.plan_root / "inputs" / "recovery-point.json").read_text(encoding="utf-8"))
        self.assertEqual((runtime["captured_commit"], recovery["captured_commit"]), (head, head))
        self.assertEqual(runtime["codex"]["active_profile_sha256"], self.codex_digests["1.2.3"])
        self.assertEqual((recovery["codex"]["previous_version"], recovery["codex"]["previous_profile_sha256"]),
                         ("1.2.2", self.codex_digests["1.2.2"]))
        for name in ("request.json", "runtime-state.json", "recovery-point.json"):
            self.assertEqual(stat.S_IMODE((self.plan_root / "inputs" / name).stat().st_mode), 0o600)
        for directory in (self.plan_root, self.plan_root / "inputs", self.plan_root / "evidence"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        template = load_request_template(self.root)
        self.assertEqual(template_validity(self.root, request, template)["findings"], [])

    def test_rerender_follows_head_until_plan_create(self) -> None:
        self._render()
        (self.root / "fork-2.txt").write_text("fork-2", encoding="utf-8")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "fork-2")
        self._render()
        runtime = json.loads((self.plan_root / "inputs" / "runtime-state.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["captured_commit"], git(self.root, "rev-parse", "HEAD"))
        write_json(self.plan_root / "evidence" / "plan.json", {"plan": "created"})
        with self.assertRaisesRegex(UpstreamMergeError, "不得重渲染"):
            self._render()

    def test_rejects_dirty_tree_mismatched_name_and_missing_baseline(self) -> None:
        (self.root / "untracked.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "干净工作树"):
            self._render()
        (self.root / "untracked.txt").unlink()
        with self.assertRaisesRegex(UpstreamMergeError, "tag 与 --upstream-tag 不一致"):
            self._render(plan_root=self.plans / "v1.0.1-20261004-001")
        with self.assertRaisesRegex(UpstreamMergeError, "命名为"):
            self._render(plan_root=self.plans / "merge-v1.0.2")
        with self.assertRaisesRegex(UpstreamMergeError, "基线收据不存在"):
            self._render(baseline_acceptance=self.tmp / "missing.json")

    def test_codex_roles_come_from_release_graph(self) -> None:
        # 快照目录里 1.2.3 排在后面，但发布图把 1.2.2 标成 active 时以发布图为准。
        self._write_catalogs(active_version="1.2.2", previous_version="1.2.3")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "swap roles")
        self._render()
        clients = self._request()["official_clients"]
        self.assertEqual(clients["codex"]["target_version"], "1.2.2")
        self.assertTrue(clients["codex"]["rollback_path"].endswith("profiles/1.2.3/profile.json"))

    def test_previous_evidence_defaults_to_latest_finished_plan(self) -> None:
        unfinished = self.plans / "v1.0.1-20260920-001" / "evidence"
        write_json(unfinished / INVENTORY_FILES[0], {"inventory": "partial"})
        newer = self._finished_plan("v1.0.1-20260925-002")
        older_receipt = self.previous / FINALIZE_RECEIPT
        os.utime(older_receipt, (time.time() - 3600, time.time() - 3600))
        result = self._render()
        self.assertEqual(result["previous_evidence"], str(newer))
        with self.assertRaisesRegex(UpstreamMergeError, "缺少 Inventory 基线"):
            self._render(previous_evidence=unfinished)

    def test_missing_source_root_is_reported(self) -> None:
        shutil.rmtree(self.root / "local-analysis")
        with self.assertRaisesRegex(UpstreamMergeError, "源码根不存在"):
            self._render()


if __name__ == "__main__":
    unittest.main()
