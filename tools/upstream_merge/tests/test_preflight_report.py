"""预检五项报告的纯函数测试。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.canonical import bind_identity
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.freeze import FREEZE_REGISTRY_RELATIVE, FREEZE_REGISTRY_SCHEMA, MAINTENANCE_ROOT
from tools.upstream_merge.preflight_report import (
    OFFICIAL_EGRESS_ROOT,
    REQUEST_TEMPLATE_RELATIVE,
    conflict_closure,
    freeze_coverage,
    load_request_template,
    scanner_coverage,
    template_validity,
    tool_bundle_disturbance,
)
from tools.upstream_merge.tests.test_workflow import SyntheticRepository, run

SOURCE_ROOT = Path(__file__).resolve().parents[3]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, document: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    path.write_text(raw, encoding="utf-8")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TemplateValidityTest(unittest.TestCase):
    """在临时目录里合成 release catalog，验证模板比对与 catalog 绑定检查。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        shutil.copy2(SOURCE_ROOT / REQUEST_TEMPLATE_RELATIVE, self._ensure(self.root / REQUEST_TEMPLATE_RELATIVE))
        self.template = load_request_template(self.root)
        egress = self.root / OFFICIAL_EGRESS_ROOT
        # Claude：production_active 指向 release，release.profile 指向 profile 文件。
        profile_relative = "catalogdata/claude/profiles/9.9.9/profile.json"
        profile_sha = write_json(egress / profile_relative, {"profile": "claude"})
        rollback_relative = f"{MAINTENANCE_ROOT}/claude-rollback-receipt.json"
        rollback_sha = write_json(self.root / rollback_relative, {"receipt": "rollback"})
        write_json(
            egress / "catalogdata/claude/release-catalog.json",
            {
                "releases": [{"version": "9.9.9", "release_sha256": "r" * 64, "profile": {"path": profile_relative, "sha256": profile_sha}}],
                "selectors": {
                    "production_active": {"kind": "release", "release_sha256": "r" * 64},
                    "production_rollback": {"kind": "operational-deployment", "deployment": {"receipt": {"path": rollback_relative, "sha256": rollback_sha}}},
                },
            },
        )
        # Codex：runtime release catalog 绑定 snapshot catalog，snapshot 指向 profile 文件。
        active_file = "profiles/1.2.3/active.json"
        rollback_file = "profiles/1.2.2/rollback.json"
        active_sha = write_json(egress / "catalogdata/runtime" / active_file, {"profile": "codex-active"})
        rollback_sha_codex = write_json(egress / "catalogdata/runtime" / rollback_file, {"profile": "codex-rollback"})
        catalog_relative = "catalogdata/runtime/snapshot-catalogs/catalog.json"
        catalog_sha = write_json(
            egress / catalog_relative,
            {"snapshots": [
                {"version": "1.2.3", "digest": active_sha, "file": active_file},
                {"version": "1.2.2", "digest": "x" * 64, "blob_sha256": rollback_sha_codex, "file": rollback_file},
            ]},
        )
        write_json(egress / "catalogdata/runtime/release-catalog.json", {"snapshot_catalog": {"path": catalog_relative, "sha256": catalog_sha}})
        self.request = json.loads(json.dumps(self.template["request"]))
        clients = self.request["official_clients"]
        clients["claude"]["target_version"] = "9.9.9"
        clients["claude"]["active_path"] = str(egress / profile_relative)
        clients["claude"]["rollback_path"] = str(self.root / rollback_relative)
        clients["codex"]["target_version"] = "1.2.3"
        clients["codex"]["active_path"] = str(egress / "catalogdata/runtime" / active_file)
        clients["codex"]["rollback_path"] = str(egress / "catalogdata/runtime" / rollback_file)

    @staticmethod
    def _ensure(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_request_generated_from_template_passes(self) -> None:
        report = template_validity(self.root, self.request, self.template)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["execution_group_count"], 3)
        self.assertEqual(report["gate_count"], 12)

    def test_rendered_repository_placeholder_matches(self) -> None:
        for gate in self.request["gates"]:
            gate["argv"] = [item.replace("{repository}", str(self.root)) for item in gate["argv"]]
        self.assertEqual(template_validity(self.root, self.request, self.template)["status"], "passed")

    def test_stale_template_and_catalog_drift_are_reported(self) -> None:
        self.request["gates"][0]["argv"] = ["go", "test", "./..."]
        self.request["gates"][8]["mode"] = "receipt_replay"
        self.request["gates"][8]["execution_group"] = None
        self.request["official_clients"]["claude"]["target_version"] = "9.9.8"
        self.request["official_clients"]["codex"]["active_path"] = str(self.root / "missing.json")
        self.request["official_clients"]["codex"]["persona"]["provider"] = "someone-else"
        findings = template_validity(self.root, self.request, self.template)["findings"]
        joined = "\n".join(findings)
        self.assertIn("argv 与模板不一致", joined)
        self.assertIn("receipt_replay", joined)
        self.assertIn("执行组数量", joined)
        self.assertIn("Claude target_version", joined)
        self.assertIn("Codex active profile 不在当前 snapshot catalog", joined)
        self.assertIn("codex persona 与模板不一致", joined)

    def test_missing_template_fails_closed(self) -> None:
        (self.root / REQUEST_TEMPLATE_RELATIVE).unlink()
        with self.assertRaises(UpstreamMergeError):
            load_request_template(self.root)


class RepositoryReportTest(unittest.TestCase):
    """用合成双分支仓库验证闭集受扰与冻结覆盖。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.synthetic = SyntheticRepository(Path(self.temporary.name), conflict=False)
        self.addCleanup(self.synthetic.cleanup_worktree)
        self.root = self.synthetic.root

    def _upstream_commit(self, mutate) -> str:
        run(self.root, "git", "checkout", "-q", "upstream")
        mutate()
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-q", "-m", "upstream change")
        commit = run(self.root, "git", "rev-parse", "HEAD")
        run(self.root, "git", "checkout", "-q", "main")
        return commit

    def test_tool_bundle_disturbance_lists_upstream_touched_bundle_files(self) -> None:
        clean = tool_bundle_disturbance(self.root, self.synthetic.base, self.synthetic.upstream)
        self.assertEqual(clean["status"], "clean")
        self.assertEqual(clean["changed_bundle_paths"], [])
        upstream = self._upstream_commit(lambda: (self.root / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8"))
        disturbed = tool_bundle_disturbance(self.root, self.synthetic.base, upstream)
        self.assertEqual(disturbed["status"], "disturbed")
        self.assertEqual(disturbed["changed_bundle_paths"], ["Makefile"])

    def test_freeze_coverage_reports_hits_conflicts_and_rule_matches(self) -> None:
        maintenance = self.root / MAINTENANCE_ROOT
        frozen_digest = sha256_text("fork\n")
        write_json(
            maintenance / "example-successor.json",
            {"schema_version": "official-egress-example-successor/v1", "transitions": [
                {"path": "fork.txt", "predecessor_sha256s": ["0" * 64], "to_sha256": frozen_digest, "reason": "登记 fork.txt"},
                {"path": "backend/cmd/egressscan/main.go", "predecessor_sha256s": ["1" * 64], "to_sha256": sha256_text("package main\nfunc main() {}\n"), "reason": "登记扫描器"},
            ]},
        )
        write_json(
            self.root / FREEZE_REGISTRY_RELATIVE,
            bind_identity({
                "schema_version": FREEZE_REGISTRY_SCHEMA,
                "issued_at_utc": "2026-09-10T12:00:00Z",
                "scope": "freeze-registry",
                "rules": [{
                    "id": "scanner",
                    "description": "扫描器算法",
                    "match": {"prefixes": ["backend/cmd/egressscan/"], "exclude_suffixes": ["_test.go"]},
                    "action": {"kind": "single_hop_file", "file": "docs/x.json", "instruction": "改写 to"},
                    "verification": ["make scan"],
                }],
            }),
        )
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-q", "-m", "register frozen paths")
        base = run(self.root, "git", "rev-parse", "HEAD")
        upstream = self._upstream_commit(lambda: (
            (self.root / "fork.txt").write_text("fork changed by upstream\n", encoding="utf-8"),
            (self.root / "backend/cmd/egressscan/main.go").write_text("package main\nfunc main() { println() }\n", encoding="utf-8"),
            (self.root / "free.txt").write_text("free\n", encoding="utf-8"),
        ))
        report = freeze_coverage(self.root, base, upstream, conflict_paths=["fork.txt"])
        self.assertEqual(report["status"], "computed")
        self.assertEqual(report["frozen_hit_paths"], ["backend/cmd/egressscan/main.go", "fork.txt"])
        self.assertEqual(report["conflicting_frozen_paths"], ["fork.txt"])
        self.assertEqual(report["registry_rule_hits"], {"scanner": ["backend/cmd/egressscan/main.go"]})
        self.assertEqual(report["frozen_path_count"], 2)
        self.assertGreaterEqual(report["upstream_changed_path_count"], 3)

    def test_freeze_coverage_requires_registry(self) -> None:
        (self.root / MAINTENANCE_ROOT).mkdir(parents=True, exist_ok=True)
        with self.assertRaises(UpstreamMergeError):
            freeze_coverage(self.root, self.synthetic.base, self.synthetic.upstream, conflict_paths=[])


class PureReportTest(unittest.TestCase):
    def test_scanner_coverage_diffs_sink_ids(self) -> None:
        fork = {"sinks": [{"scan_candidate_id": "a", "file": "x.go", "sink_kind": "http"}, {"scan_candidate_id": "b"}]}
        candidate = {"sinks": [{"scan_candidate_id": "b"}, {"scan_candidate_id": "c", "file": "new.go", "func": "Do", "sink_kind": "ws", "protocol": "wss", "sink_type": "terminal", "package": "p"}]}
        report = scanner_coverage(fork, candidate)
        self.assertEqual(report["status"], "computed")
        self.assertEqual(report["added_sink_count"], 1)
        self.assertEqual(report["removed_sink_count"], 1)
        self.assertEqual(report["added_sinks"][0]["scan_candidate_id"], "c")
        self.assertEqual(report["added_sinks"][0]["sink_kind"], "ws")
        self.assertEqual(report["removed_sinks"][0]["scan_candidate_id"], "a")
        self.assertEqual(scanner_coverage(None, candidate, deferred_reason="冲突")["status"], "deferred")
        self.assertEqual(scanner_coverage(None, candidate)["status"], "failed")

    def test_conflict_closure(self) -> None:
        self.assertEqual(
            conflict_closure(["b.txt", "a.txt"], 7),
            {"conflict_count": 2, "conflict_paths": ["b.txt", "a.txt"], "upstream_changed_path_count": 7},
        )


if __name__ == "__main__":
    unittest.main()
