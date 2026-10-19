"""预检报告在新私有目录中的真实 Git 试合并与输出边界测试。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest import mock

from tools.upstream_merge import workflow
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.preflight_report import parse_scanner_check_output
from tools.upstream_merge.tests.test_covered_tags import CoveredTagsFixture, git

SOURCE_ROOT = Path(__file__).resolve().parents[3]


def secret_scan_gates() -> list[dict]:
    """标准模板里的 secret_scan 门禁：预检第七项执行它。路径测试只需要这一个门禁，其余 11 个与本测试无关。"""

    template = json.loads(
        (SOURCE_ROOT / "tools/upstream_merge/request_template_v2.json").read_text(encoding="utf-8")
    )
    return [copy.deepcopy(gate) for gate in template["request"]["gates"] if gate["category"] == "secret_scan"]


class PreflightPathTest(CoveredTagsFixture):
    def test_preflight_writes_ignored_internal_report_without_dirtying_repository(self) -> None:
        (self.root / ".gitignore").write_text("/local-analysis/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-q", "-m", "忽略私有分析资料")
        request_path = self.root.parent / "request.json"
        request = {
            "plan_id": "preflight-private-path",
            "repository": {"managed_ref": "refs/heads/main"},
            "upstream": self.upstream(tag="v1.0.1"),
            "gates": secret_scan_gates(),
        }
        request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
        output = self.root / "local-analysis/upstream/preflight/report.json"
        # 保留真实临时 worktree 和合并；路径测试不启动 Go 扫描器或网络下载。
        with mock.patch.object(workflow, "load_request", return_value=request), mock.patch.object(
            workflow, "run_egress_snapshot", side_effect=UpstreamMergeError("测试仓库不包含扫描器")
        ), mock.patch.object(
            workflow, "_preflight_upstream_sink_scan", side_effect=UpstreamMergeError("测试仓库不包含扫描器")
        ), mock.patch.object(
            workflow, "tool_bundle_disturbance", return_value={"status": "passed", "changed_paths": []}
        ):
            result = workflow.run_preflight(request_path, self.root, output)
        self.assertTrue(result["non_authoritative"])
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual(git(self.root, "worktree", "list", "--porcelain").count("worktree "), 1)
        # 守护：run_preflight 产出的 report 键集合必须与 artifacts schema 里 preflight report 的 required 一致。
        # 往报告里加了项却忘了改 schema（或反过来）都在这里失败；schema 的 report 是 additionalProperties=false。
        schema = json.loads(
            (SOURCE_ROOT / "tools/upstream_merge_artifacts.schema.json").read_text(encoding="utf-8")
        )
        report_schema = schema["$defs"]["preflight"]["properties"]["report"]
        self.assertEqual(set(result["report"]), set(report_schema["required"]))
        self.assertEqual(set(result["report"]), set(report_schema["properties"]))
        self.assertIn("secret_scan", result["report"])
        # 第七项主干密钥扫描在干净主干上通过，且没有留下检出（上面已断言只剩主 worktree）。
        self.assertEqual(result["report"]["secret_scan"]["status"], "passed")
        self.assertEqual(result["report"]["secret_scan"]["gate_id"], "10-secret-scan")
        self.assertEqual(result["report"]["secret_scan"]["source_commit"], result["repository"]["fork_head"])
        self.assertFalse([item for item in result["blockers"] if "主干密钥扫描" in item])

    def test_failed_go_mod_tidy_is_named_as_root_cause_in_the_scan_blocker(self) -> None:
        """试扫描因 go.mod 不一致没有结论、离线 go mod tidy 又失败时，阻断信息要直接点出根因。"""

        scan = parse_scanner_check_output("", "go: updates to go.mod needed; to update it:\n\tgo mod tidy\n", 1)
        scan["unresolved_conflict_count"] = 0
        scan["go_mod_tidy"] = {
            "status": "failed",
            "argv": ["go", "mod", "tidy"],
            "exit_code": 1,
            "changed": False,
            "error": "go: example.com/newdep@v0.3.0: module lookup disabled by GOPROXY=off",
        }
        request_path = self.root.parent / "request.json"
        request = {
            "plan_id": "preflight-go-mod-tidy",
            "repository": {"managed_ref": "refs/heads/main"},
            "upstream": self.upstream(tag="v1.0.1"),
            "gates": secret_scan_gates(),
        }
        request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
        with mock.patch.object(workflow, "load_request", return_value=request), mock.patch.object(
            workflow, "run_egress_snapshot", side_effect=UpstreamMergeError("测试仓库不包含扫描器")
        ), mock.patch.object(workflow, "_preflight_upstream_sink_scan", return_value=scan), mock.patch.object(
            workflow, "tool_bundle_disturbance", return_value={"status": "passed", "changed_paths": []}
        ):
            result = workflow.run_preflight(request_path, self.root)
        coverage = result["report"]["scanner_coverage"]
        self.assertEqual(coverage["status"], "failed")
        self.assertEqual(coverage["go_mod_tidy"], scan["go_mod_tidy"])
        (blocker,) = [item for item in result["blockers"] if item.startswith("上游发送点试扫描未得出结论：")]
        self.assertIn("试扫描树离线 go mod tidy 失败", blocker)
        self.assertIn("go mod download", blocker)
        self.assertIn("go: updates to go.mod needed", blocker)
        self.assertEqual(result["result"], "blocked")
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual(git(self.root, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_invalid_output_is_rejected_before_loading_request(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "主仓库之外"):
            workflow.run_preflight(self.root.parent / "missing.json", self.root, self.root / "report.json")
        self.assertFalse((self.root / "report.json").exists())
