"""assertion bundle 编排必须完整承接跨 Campaign 的逐 Job 证据根。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class PrepareAssertionBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo_root = Path(__file__).parents[3]
        self.script = self.repo_root / "tools" / "prepare_assertion_bundle.sh"
        self.campaign = self.root / "new-campaign"
        self.attempt_id = "attempt-1"
        self.candidate_id = "candidate-1"
        self.attempt = (
            self.campaign
            / "candidates"
            / self.candidate_id
            / "attempts"
            / self.attempt_id
        )
        (self.attempt / "evidence").mkdir(parents=True)

        reused_roots = [self._make_root(f"reused-root-{index}") for index in range(1, 8)]
        executed_roots = [
            self._make_root(f"executed-root-{index}") for index in range(1, 8)
        ]
        jobs = []
        results = []
        declaration_entries = []
        for index in range(1, 10):
            job_id = f"candidate-job-{index}"
            jobs.append({"id": job_id, "phase": "candidate", "required": True})
            roots = (
                [reused_roots[index - 1]]
                if index <= 7
                else (executed_roots[:6] if index == 8 else executed_roots[6:])
            )
            results.append(
                {
                    "id": job_id,
                    "status": "complete",
                    "required": True,
                    "disposition": "reused" if index <= 7 else "executed",
                    "evidence_roots": [str(path) for path in roots],
                }
            )
            declaration_entries.append(
                {
                    "job_id": job_id,
                    "side": "candidate",
                    "rules": [
                        {
                            "glob": "wire.jsonl",
                            "scenario_ids": [f"S{index}"],
                            "kind": "http_trace",
                            "parser": "mitm_http_jsonl",
                            "labels": {"fixture": "authority-root"},
                            "rationale": "验证跨 Campaign 权威根闭合。",
                        }
                    ],
                }
            )

        self._write_json(
            self.campaign / "campaign.json",
            {
                "campaign_id": "new-campaign",
                "target_version": "0.151.0",
                "jobs": jobs,
            },
        )
        # 顶层只保留 7 个当前 Campaign 根，专门复现旧脚本漏掉
        # 7 个跨 Campaign 复用根的情况。
        self._write_json(
            self.attempt / "attempt.json",
            {
                "candidate_id": self.candidate_id,
                "evidence_roots": [str(path) for path in executed_roots],
                "results": results,
            },
        )
        self.declaration = self.root / "labels.json"
        self._write_json(
            self.declaration,
            {
                "schema_version": "codex-upgrade-evidence-labels/v1",
                "codex_version": "0.151.0",
                "entries": declaration_entries,
            },
        )

    def _make_root(self, name: str) -> Path:
        root = self.root / "runs" / name
        root.mkdir(parents=True)
        (root / "wire.jsonl").write_text('{"ok":true}\n', encoding="utf-8")
        return root

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _run(self) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "CAMPAIGN_DIR": str(self.campaign),
                "ATTEMPT_ID": self.attempt_id,
                "SIDE": "candidate",
                "CANDIDATE_ID": self.candidate_id,
                "DECLARATION": str(self.declaration),
                "REPO_ROOT": str(self.repo_root),
            }
        )
        return subprocess.run(
            ["bash", str(self.script)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    def test_uses_all_fourteen_result_roots_instead_of_top_level_roots(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        bundle = self.attempt / "evidence" / "assertion-bundle"
        provenance = json.loads((bundle / "provenance.json").read_text(encoding="utf-8"))
        actual_roots = {entry["source_root"] for entry in provenance["entries"]}
        self.assertEqual(len(actual_roots), 14)
        self.assertEqual(
            actual_roots,
            {f"reused-root-{index}" for index in range(1, 8)}
            | {f"executed-root-{index}" for index in range(1, 8)},
        )
        self.assertIn("assertion bundle 权威根闭合：14 个", result.stdout)

    def test_declared_go_test_log_missing_fails_closed(self) -> None:
        """标签声明了 candidate-go-test.jsonl 而 bundle 没有它：候选侧必须失败关闭。"""

        declaration = json.loads(self.declaration.read_text(encoding="utf-8"))
        declaration["entries"].append(
            {
                "job_id": "candidate-trace-test",
                "side": "candidate",
                "rules": [
                    {
                        "glob": "candidate-go-test.jsonl",
                        "scenario_ids": ["S1"],
                        "kind": "stdout_log",
                        "parser": "opaque_bound_source",
                        "labels": {"surface": "test"},
                        "rationale": "夹具：声明但未产出。",
                    }
                ],
            }
        )
        self._write_json(self.declaration, declaration)
        campaign = json.loads((self.campaign / "campaign.json").read_text(encoding="utf-8"))
        campaign["jobs"].append({"id": "candidate-trace-test", "phase": "candidate", "required": True})
        self._write_json(self.campaign / "campaign.json", campaign)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.attempt / "evidence" / "assertion-bundle" / "provenance.json").exists())

    def test_missing_result_root_fails_before_bundle_publication(self) -> None:
        attempt = json.loads((self.attempt / "attempt.json").read_text(encoding="utf-8"))
        attempt["results"][0]["evidence_roots"] = [str(self.root / "missing-root")]
        self._write_json(self.attempt / "attempt.json", attempt)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("evidence_root 不存在或不可信", result.stderr)
        self.assertFalse((self.attempt / "evidence" / "assertion-bundle").exists())

    def test_catalog_failure_leaves_no_partial_bundle(self) -> None:
        declaration = json.loads(self.declaration.read_text(encoding="utf-8"))
        declaration["entries"][0]["rules"][0]["glob"] = "missing.jsonl"
        self._write_json(self.declaration, declaration)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("编目不完整", result.stderr)
        self.assertFalse((self.attempt / "evidence" / "assertion-bundle").exists())

    def test_candidate_trace_runs_before_atomic_publication(self) -> None:
        """含 Go 测试证据的候选 bundle 必须先派生 trace，再原子发布。"""

        script = self.script.read_text(encoding="utf-8")
        trace_index = script.index('"$tool_root/candidate_test_trace.py"')
        publish_index = script.index("os.rename(source, target)")
        self.assertLess(trace_index, publish_index)
        self.assertIn("--output-receipt candidate-trace/trace-receipt.json", script)


if __name__ == "__main__":
    unittest.main()
