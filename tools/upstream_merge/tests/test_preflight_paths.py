"""预检报告在新私有目录中的真实 Git 试合并与输出边界测试。"""

from __future__ import annotations

import json
from unittest import mock

from tools.upstream_merge import workflow
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.tests.test_covered_tags import CoveredTagsFixture, git


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

    def test_invalid_output_is_rejected_before_loading_request(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "主仓库之外"):
            workflow.run_preflight(self.root.parent / "missing.json", self.root, self.root / "report.json")
        self.assertFalse((self.root / "report.json").exists())
