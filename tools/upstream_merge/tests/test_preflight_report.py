"""预检五项报告的纯函数测试。"""

from __future__ import annotations

import hashlib
import json
import os
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
    candidate_sink_diff,
    conflict_closure,
    freeze_coverage,
    load_request_template,
    parse_scanner_check_output,
    scanner_coverage,
    template_validity,
    tool_bundle_disturbance,
)
from tools.upstream_merge.tests.test_workflow import SyntheticRepository, run
from tools.upstream_merge.workflow import (
    SCAN_ONLY_RULES_RELATIVE,
    SCANNER_SOURCE_RELATIVE,
    SCANNER_SUCCESSOR_RELATIVE,
    _preflight_upstream_sink_scan,
    _scanner_algorithm_digest,
    _write_scan_only_rules,
)

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
        # request-render 把只读源码根渲染成主仓库路径（UM-18），源码树只在主仓库存在。
        self.source_tree = self.root / "local-analysis/sources/codex-cli-0.149.1"
        self.source_tree.mkdir(parents=True)
        for gate in self.request["gates"]:
            gate["argv"] = [item.replace("{source_repository}", str(self.root)) for item in gate["argv"]]

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

    def test_source_root_must_be_rendered_into_main_repository(self) -> None:
        """v0.2.13 合并的缺陷：源码根写成执行期的 {repository} 会被渲染到候选工作树，每个问题只报一次。"""

        def findings_for(value: str) -> list[str]:
            request = json.loads(json.dumps(self.request))
            for gate in request["gates"]:
                gate["argv"] = [
                    value if item.startswith("CODEX_0_149_1_SOURCE_ROOT=") else item for item in gate["argv"]
                ]
            report = template_validity(self.root, request, self.template)
            self.assertEqual(report["status"], "failed")
            return report["findings"]

        unrendered = findings_for("CODEX_0_149_1_SOURCE_ROOT={source_repository}/local-analysis/sources/codex-cli-0.149.1")
        self.assertEqual(sum("{source_repository} 未渲染" in item for item in unrendered), 1)
        worktree = findings_for("CODEX_0_149_1_SOURCE_ROOT={repository}/local-analysis/sources/codex-cli-0.149.1")
        self.assertEqual(sum("执行时的 {repository}" in item for item in worktree), 1)
        self.source_tree.rmdir()
        missing = template_validity(self.root, self.request, self.template)["findings"]
        self.assertEqual(sum("源码根不存在" in item for item in missing), 1)

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


DRIFT_OUTPUT = """❌ 发送面相对基线发生漂移：
  [新增] example.com/svc.*S.NewName@backend/svc/a.go#facade_http_upstream_do#1  (example.com/x.Do @ backend/svc/a.go:12)
  [新增] example.com/typesafe.Evaluate@backend/pkg/typesafe/client.go#net_http_client_do#1  (net/http.(*Client).Do @ backend/pkg/typesafe/client.go:40)
  [变更] example.com/svc.*S.Keep@backend/svc/b.go#net_http_client_do#1  persona: "out-of-scope" → "codex-cli"
  [消失] example.com/svc.*S.OldName@backend/svc/a.go#facade_http_upstream_do#1  (example.com/x.Do)
  [变更] [基线元数据] packages_loaded: "900" → "912"
"""
SCAN_FAILED_STDERR = """扫描失败：2 条 sink 未匹配任何分类规则：
  - example.com/repo.*R.QueryEligibility@backend/repo/r.go#reqv3_get#1  ((*req.Request).Get @ backend/repo/r.go:30)
  - example.com/repo.*R.SendInvite@backend/repo/r.go#reqv3_post#1  ((*req.Request).Post @ backend/repo/r.go:44)

新增发送点必须在 cmd/egressscan/classify.go 显式登记。

1 条分类结果不完整：
  - example.com/svc.*Q.QueryUsage@backend/svc/q.go#reqv3_get#1：endpoint_evidence 非法: ""
"""


class PureReportTest(unittest.TestCase):
    def test_candidate_sink_diff_diffs_sink_ids(self) -> None:
        fork = {"sinks": [{"scan_candidate_id": "a", "file": "x.go", "sink_kind": "http"}, {"scan_candidate_id": "b"}]}
        candidate = {"sinks": [{"scan_candidate_id": "b"}, {"scan_candidate_id": "c", "file": "new.go", "func": "Do", "sink_kind": "ws", "protocol": "wss", "sink_type": "terminal", "package": "p"}]}
        report = candidate_sink_diff(fork, candidate)
        self.assertEqual(report["status"], "computed")
        self.assertEqual(report["added_sink_count"], 1)
        self.assertEqual(report["removed_sink_count"], 1)
        self.assertEqual(report["added_sinks"][0]["scan_candidate_id"], "c")
        self.assertEqual(report["added_sinks"][0]["sink_kind"], "ws")
        self.assertEqual(report["removed_sinks"][0]["scan_candidate_id"], "a")
        self.assertEqual(candidate_sink_diff(None, candidate, deferred_reason="冲突")["status"], "deferred")
        self.assertEqual(candidate_sink_diff(None, candidate)["status"], "failed")

    def test_parse_scanner_check_output_drift(self) -> None:
        parsed = parse_scanner_check_output(DRIFT_OUTPUT, "", 1)
        self.assertEqual(parsed["outcome"], "drift")
        self.assertEqual(
            [item["scan_candidate_id"] for item in parsed["added"]],
            [
                "example.com/svc.*S.NewName@backend/svc/a.go#facade_http_upstream_do#1",
                "example.com/typesafe.Evaluate@backend/pkg/typesafe/client.go#net_http_client_do#1",
            ],
        )
        self.assertEqual(len(parsed["changed"]), 1)
        self.assertEqual(len(parsed["removed"]), 1)
        self.assertEqual(parsed["unclassified"], [])
        # 基线元数据变化单独归类，不当成发送点。
        self.assertEqual(len(parsed["metadata_changes"]), 1)
        metadata_only = parse_scanner_check_output("  [变更] [基线元数据] packages_loaded: \"1\" → \"2\"\n", "", 1)
        self.assertEqual((metadata_only["outcome"], metadata_only["changed"]), ("drift", []))
        self.assertEqual(scanner_coverage(metadata_only, {})["status"], "passed")

    def test_parse_scanner_check_output_scan_failure(self) -> None:
        parsed = parse_scanner_check_output("", SCAN_FAILED_STDERR, 1)
        self.assertEqual(parsed["outcome"], "scan_failed")
        self.assertEqual(
            [item["scan_candidate_id"] for item in parsed["unclassified"]],
            [
                "example.com/repo.*R.QueryEligibility@backend/repo/r.go#reqv3_get#1",
                "example.com/repo.*R.SendInvite@backend/repo/r.go#reqv3_post#1",
            ],
        )
        self.assertEqual(
            [item["scan_candidate_id"] for item in parsed["classification_problems"]],
            ["example.com/svc.*Q.QueryUsage@backend/svc/q.go#reqv3_get#1"],
        )
        self.assertEqual(parse_scanner_check_output("✅ 当前发送面通过\n", "", 0)["outcome"], "passed")
        broken = parse_scanner_check_output("", "验证 bootstrap inventory lock 失败：摘要不符\n", 1)
        self.assertEqual(broken["outcome"], "error")
        self.assertIn("inventory lock", broken["error"])

    def test_scanner_coverage_blocks_unregistered_additions(self) -> None:
        candidate_merge = {"status": "deferred", "reason": "冲突"}
        drift = scanner_coverage(parse_scanner_check_output(DRIFT_OUTPUT, "", 1), candidate_merge)
        self.assertEqual(drift["status"], "blocked")
        self.assertEqual(drift["unregistered_added_count"], 2)
        self.assertIn("reviewedPostBootstrapSinkAdditions", drift["registration_hint"])
        self.assertEqual(
            drift["rename_candidates"],
            [
                {
                    "before": "example.com/svc.*S.OldName@backend/svc/a.go#facade_http_upstream_do#1",
                    "after": "example.com/svc.*S.NewName@backend/svc/a.go#facade_http_upstream_do#1",
                }
            ],
        )
        self.assertEqual(drift["candidate_merge"], candidate_merge)
        failed = scanner_coverage(parse_scanner_check_output("", SCAN_FAILED_STDERR, 1), candidate_merge)
        self.assertEqual((failed["status"], failed["unregistered_added_count"]), ("blocked", 2))

    def test_scanner_coverage_never_passes_without_a_verdict(self) -> None:
        # 只有"分类不完整"、没有未分类项时，扫描器不会进入基线比较，看不到[新增]，不能判为通过。
        only_problems = (
            "扫描失败：\n1 条分类结果不完整：\n"
            '  - example.com/svc.*Q.QueryUsage@backend/svc/q.go#reqv3_get#1：endpoint_evidence 非法: ""\n'
        )
        parsed = parse_scanner_check_output("", only_problems, 1)
        self.assertEqual((parsed["outcome"], parsed["unclassified"]), ("scan_failed", []))
        self.assertEqual(scanner_coverage(parsed, {})["status"], "failed")
        self.assertEqual(scanner_coverage(parse_scanner_check_output("", "make: *** No rule\n", 2), {})["status"], "failed")
        self.assertEqual(scanner_coverage(None, {})["status"], "failed")
        self.assertEqual(scanner_coverage(parse_scanner_check_output("ok\n", "", 0), {})["status"], "passed")

    def test_conflict_closure(self) -> None:
        self.assertEqual(
            conflict_closure(["b.txt", "a.txt"], 7),
            {"conflict_count": 2, "conflict_paths": ["b.txt", "a.txt"], "upstream_changed_path_count": 7},
        )


# 假扫描器：backend/*.go 首行 "// SINK <ID>" 视为一个发送点，registered.txt 逐行列出已登记的 ID。
# 带 "// UNCLASSIFIED" 标记且试扫描临时分类里没有它的 ID 时，按扫描器格式在基线比较前失败；
# 其余输出格式与 egressscan -mode check 的漂移段一致，未登记即 "[新增]" 并以非零退出。
FAKE_SCANNER = """#!/bin/sh
rules=backend/cmd/egressscan/zz_preflight_scan_only_rules.go
missing=""
for f in backend/*.go; do
  id=$(sed -n 's|^// SINK ||p' "$f")
  [ -z "$id" ] && continue
  if grep -q '^// UNCLASSIFIED' "$f"; then
    if [ ! -f "$rules" ] || ! grep -qF "\\"$id\\"" "$rules"; then
      missing="$missing $id"
    fi
  fi
done
if [ -n "$missing" ]; then
  echo "扫描失败：$(echo $missing | wc -w | tr -d ' ') 条 sink 未匹配任何分类规则：" >&2
  for id in $missing; do echo "  - $id  (fake.Get @ backend:1)" >&2; done
  exit 1
fi
status=0
for f in backend/*.go; do
  id=$(sed -n 's|^// SINK ||p' "$f")
  [ -z "$id" ] && continue
  if ! grep -qxF "$id" registered.txt; then
    [ $status -eq 0 ] && echo "❌ 发送面相对基线发生漂移："
    echo "  [新增] $id  (fake.Do @ $f:1)"
    status=1
  fi
done
[ $status -eq 0 ] && echo "✅ 当前发送面通过"
exit $status
"""
FAKE_MAKEFILE = "egress-scanner-check:\n\t@sh scan.sh\n"
FORK_SINK = "fork.Base@backend/base.go#net_http_client_do#1"
UPSTREAM_SINK = "upstream.New@backend/new.go#net_http_client_do#1"


class UpstreamSinkScanTest(unittest.TestCase):
    """UM-13：-X ours 试扫描树在有冲突时仍能列出上游新增、尚未预先登记的发送点。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        run(self.root, "git", "init", "-q", "-b", "main")
        run(self.root, "git", "config", "user.name", "Preflight Test")
        run(self.root, "git", "config", "user.email", "preflight@example.invalid")
        run(self.root, "git", "config", "commit.gpgsign", "false")
        files = {
            "Makefile": FAKE_MAKEFILE,
            "scan.sh": FAKE_SCANNER,
            "registered.txt": FORK_SINK + "\n",
            "backend/base.go": f"// SINK {FORK_SINK}\npackage base\n",
            f"{SCANNER_SOURCE_RELATIVE}/main.go": "package main\n",
            SCANNER_SUCCESSOR_RELATIVE: json.dumps({"schema_version": "x/v1", "from_sha256": "1" * 64, "to_sha256": "2" * 64}) + "\n",
            "conflict.txt": "base\n",
            "shared.txt": "base\n",
        }
        for relative, content in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-q", "-m", "base")
        # 上游：与 fork 冲突的内容改动、修改 fork 已删除的文件，并新增一个发送点文件。
        run(self.root, "git", "checkout", "-q", "-b", "upstream")
        (self.root / "conflict.txt").write_text("upstream\n", encoding="utf-8")
        (self.root / "shared.txt").write_text("upstream change\n", encoding="utf-8")
        (self.root / "backend/new.go").write_text(f"// SINK {UPSTREAM_SINK}\npackage new\n", encoding="utf-8")
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-q", "-m", "upstream")
        self.upstream = run(self.root, "git", "rev-parse", "HEAD")
        # fork：同一行改成不同内容，并删除上游还在修改的文件。
        run(self.root, "git", "checkout", "-q", "main")
        (self.root / "conflict.txt").write_text("fork\n", encoding="utf-8")
        run(self.root, "git", "rm", "-q", "shared.txt")
        run(self.root, "git", "commit", "-q", "-am", "fork")

    def scan(self) -> dict:
        fork_head = run(self.root, "git", "rev-parse", "HEAD")
        work = Path(self.temporary.name) / f"work-{fork_head[:8]}"
        work.mkdir()
        return _preflight_upstream_sink_scan(self.root, fork_head, self.upstream, work, dict(os.environ))

    def test_lists_unregistered_upstream_sink_despite_conflicts(self) -> None:
        # 常规试合并在这里会因冲突停下（旧实现只标 deferred）；试扫描树照常得出结论。
        conflicted = subprocess.run(
            ["git", "merge-tree", "--write-tree", "main", self.upstream],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(conflicted.returncode, 0)
        parsed = self.scan()
        self.assertEqual(parsed["outcome"], "drift")
        self.assertEqual([item["scan_candidate_id"] for item in parsed["added"]], [UPSTREAM_SINK])
        # conflict.txt 由 -X ours 取 fork 侧；shared.txt 的修改／删除冲突按 fork 侧（已删除）处理。
        self.assertEqual(parsed["unresolved_conflict_count"], 1)
        coverage = scanner_coverage(parsed, {"status": "deferred"})
        self.assertEqual((coverage["status"], coverage["unregistered_added_count"]), ("blocked", 1))
        # 试扫描不改主仓库：工作树干净、临时 worktree 已移除。
        self.assertEqual(run(self.root, "git", "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(len(run(self.root, "git", "worktree", "list").splitlines()), 1)

    def test_registered_upstream_sink_passes(self) -> None:
        # 按报告在主干预先登记后重跑：不再阻断。
        with (self.root / "registered.txt").open("a", encoding="utf-8") as handle:
            handle.write(UPSTREAM_SINK + "\n")
        run(self.root, "git", "commit", "-q", "-am", "预先登记上游新增发送点")
        parsed = self.scan()
        self.assertEqual(parsed["outcome"], "passed")
        self.assertEqual(scanner_coverage(parsed, {"status": "deferred"})["status"], "passed")

    def test_unclassified_upstream_sink_is_listed_in_one_pass(self) -> None:
        # 上游再新增一个缺分类规则的发送点：第一轮在基线比较前失败，第二轮补临时分类后一次列全。
        referral = "upstream.Referral@backend/referral.go#reqv3_get#1"
        run(self.root, "git", "checkout", "-q", "upstream")
        (self.root / "backend/referral.go").write_text(
            f"// SINK {referral}\n// UNCLASSIFIED\npackage referral\n", encoding="utf-8"
        )
        run(self.root, "git", "add", "--all")
        run(self.root, "git", "commit", "-q", "-m", "upstream referral")
        self.upstream = run(self.root, "git", "rev-parse", "HEAD")
        run(self.root, "git", "checkout", "-q", "main")
        parsed = self.scan()
        self.assertEqual(parsed["temporarily_classified"], [referral])
        self.assertEqual([item["scan_candidate_id"] for item in parsed["unclassified"]], [referral])
        self.assertEqual(sorted(item["scan_candidate_id"] for item in parsed["added"]), sorted([referral, UPSTREAM_SINK]))
        coverage = scanner_coverage(parsed, {})
        self.assertEqual((coverage["status"], coverage["unregistered_added_count"]), ("blocked", 2))
        flags = {item["scan_candidate_id"]: item["missing_classification"] for item in coverage["unregistered_added_sinks"]}
        self.assertEqual(flags, {referral: True, UPSTREAM_SINK: False})
        self.assertEqual(run(self.root, "git", "status", "--porcelain", "--untracked-files=all"), "")

    def test_scan_only_rules_follow_scanner_digest(self) -> None:
        tree = Path(self.temporary.name) / "scan-tree"
        (tree / SCANNER_SOURCE_RELATIVE).mkdir(parents=True)
        (tree / SCANNER_SOURCE_RELATIVE / "main.go").write_text("package main\n", encoding="utf-8")
        (tree / SCANNER_SOURCE_RELATIVE / "main_test.go").write_text("package main\n", encoding="utf-8")
        successor = tree / SCANNER_SUCCESSOR_RELATIVE
        successor.parent.mkdir(parents=True)
        successor.write_text(json.dumps({"from_sha256": "1" * 64, "to_sha256": "2" * 64, "reason": "x"}), encoding="utf-8")
        identifier = "example.com/repo.*R.QueryEligibility@backend/repo/r.go#reqv3_get#1"
        _write_scan_only_rules(tree, [identifier])
        rules = (tree / SCAN_ONLY_RULES_RELATIVE).read_text(encoding="utf-8")
        self.assertIn(f"candidateExact: {json.dumps(identifier)}", rules)
        self.assertIn('persona: "out-of-scope"', rules)
        document = json.loads(successor.read_text(encoding="utf-8"))
        self.assertEqual(document["from_sha256"], "1" * 64)
        self.assertEqual(document["to_sha256"], _scanner_algorithm_digest(tree / SCANNER_SOURCE_RELATIVE))
        # 测试文件不计入扫描器算法摘要，与 egressscan 的 scannerAlgorithmDigest 一致。
        before = _scanner_algorithm_digest(tree / SCANNER_SOURCE_RELATIVE)
        (tree / SCANNER_SOURCE_RELATIVE / "other_test.go").write_text("package main\n", encoding="utf-8")
        self.assertEqual(before, _scanner_algorithm_digest(tree / SCANNER_SOURCE_RELATIVE))
        # 没有后继文件时从 bootstrap lock 取前序摘要。
        successor.unlink()
        lock = tree / "docs/egress/maintenance/bootstrap-inventory-lock.json"
        lock.write_text(json.dumps({"scanner_algorithm_sha256": "3" * 64}), encoding="utf-8")
        _write_scan_only_rules(tree, [identifier])
        created = json.loads(successor.read_text(encoding="utf-8"))
        self.assertEqual(created["from_sha256"], "3" * 64)
        self.assertEqual(created["schema_version"], "official-egress-scanner-algorithm-successor/v1")

    def test_missing_scanner_target_is_not_a_pass(self) -> None:
        (self.root / "Makefile").write_text("other:\n\t@true\n", encoding="utf-8")
        run(self.root, "git", "commit", "-q", "-am", "去掉扫描目标")
        parsed = self.scan()
        self.assertEqual(parsed["outcome"], "error")
        self.assertEqual(scanner_coverage(parsed, {})["status"], "failed")


if __name__ == "__main__":
    unittest.main()
