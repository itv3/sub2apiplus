"""candidate-trace-test：候选同源源码树上的冻结 go test -json Job。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade


TOOL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = TOOL_ROOT / "run_candidate_trace_test.sh"
MAPPING = TOOL_ROOT / "candidate_test_fact_map_0_154_0.json"

# 替身 go：按 -run 正则里的测试名逐个输出 run／pass 事件，可通过环境变量
# 让它漏掉某个测试或让某个测试 fail，用来验证脚本的失败关闭。
FAKE_GO = r'''#!/usr/bin/env python3
import json, os, re, sys
argv = sys.argv[1:]
if argv[:1] == ["version"]:
    print("go version go1.27.1 linux/arm64")
    raise SystemExit(0)
run_index = argv.index("-run")
pattern = argv[run_index + 1]
names = pattern[2:-2].split("|")
packages = [item for item in argv[run_index + 2:]]
skip = os.environ.get("FAKE_GO_SKIP", "")
fail = os.environ.get("FAKE_GO_FAIL", "")
module = "github.com/Wei-Shaw/sub2api"
for package in packages:
    full = module + package[1:]
    print(json.dumps({"Time": "2026-09-18T00:00:00Z", "Action": "start", "Package": full}))
    for index, name in enumerate(names):
        # 把测试轮流分配到各包，只是为了产生多包日志；映射解析只看 Test 字段。
        if index % len(packages) != packages.index(package):
            continue
        if name == skip:
            continue
        print(json.dumps({"Time": "2026-09-18T00:00:01Z", "Action": "run", "Package": full, "Test": name}))
        outcome = "fail" if name == fail else "pass"
        print(json.dumps({"Time": "2026-09-18T00:00:02Z", "Action": outcome, "Package": full, "Test": name, "Elapsed": 0.01}))
    print(json.dumps({"Time": "2026-09-18T00:00:03Z", "Action": "pass", "Package": full, "Elapsed": 0.02}))
raise SystemExit(1 if fail else 0)
'''


class CandidateTraceTestScriptTests(unittest.TestCase):
    def _fixture(self, root: Path, *, candidate_id: str = "cand-1") -> dict[str, str]:
        source = root / "source"
        (source / "backend").mkdir(parents=True)
        (source / "backend" / "go.mod").write_text("module github.com/Wei-Shaw/sub2api\n", encoding="utf-8")
        (source / "tools" / "official_client_capture").mkdir(parents=True)
        (source / "tools" / "official_client_capture" / "candidate_test_fact_map_0_154_0.json").write_bytes(
            MAPPING.read_bytes()
        )
        campaign = root / "campaign"
        (campaign / "candidates" / candidate_id).mkdir(parents=True)
        (campaign / "candidates" / candidate_id / "build-receipt.json").write_text(
            json.dumps({"candidate_id": candidate_id, "source": {"root": str(source), "git_commit": "a" * 40}}) + "\n",
            encoding="utf-8",
        )
        fake_go = root / "fake-go"
        fake_go.write_text(FAKE_GO, encoding="utf-8")
        fake_go.chmod(fake_go.stat().st_mode | stat.S_IXUSR)
        capture = root / "capture"
        (capture / "runs").mkdir(parents=True)
        return {
            "CODEX_VERSION": "0.154.0",
            "CAMPAIGN_DIR": str(campaign),
            "CANDIDATE_ID": candidate_id,
            "CAPTURE_ROOT": str(capture),
            "RUN_ID": "campaign-x-candidate-trace-test",
            "GO_BIN": str(fake_go),
            "GOMODCACHE": str(root / "modcache"),
        }

    def _run(self, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env={**os.environ, **environment},
            timeout=120,
        )

    def test_shell_syntax_is_valid(self) -> None:
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_writes_log_and_summary_for_every_mapped_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = self._fixture(root)
            result = self._run(environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = root / "capture" / "runs" / "campaign-x-candidate-trace-test"
            log = evidence / "candidate-go-test.jsonl"
            summary = json.loads((evidence / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["verdict"], "pass")
            self.assertEqual(summary["exit_code"], 0)
            self.assertEqual(summary["source_git_commit"], "a" * 40)
            self.assertEqual(summary["packages"], ["./internal/service", "./internal/repository"])
            self.assertEqual(summary["go_flags"], "-mod=mod")
            self.assertEqual(summary["command"][1:4], ["test", "-json", "-count=1"])
            mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
            expected = {test["name"] for test in mapping["tests"]}
            passed = {
                json.loads(line)["Test"]
                for line in log.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("Action") == "pass" and json.loads(line).get("Test")
            }
            self.assertEqual(passed, expected)
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertFalse((evidence / ".gocache").exists())
            # 证据根已存在时拒绝覆盖。
            again = self._run(environment)
            self.assertEqual(again.returncode, 2)
            self.assertIn("拒绝覆盖", again.stderr)

    def test_missing_or_failed_test_fails_closed(self) -> None:
        mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
        first = mapping["tests"][0]["name"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = self._fixture(root)
            missing = self._run({**environment, "FAKE_GO_SKIP": first})
            self.assertEqual(missing.returncode, 1)
            summary = json.loads(
                (root / "capture" / "runs" / "campaign-x-candidate-trace-test" / "run-summary.json").read_text(encoding="utf-8")
            )
            self.assertIn(f"missing=['{first}']", summary["verdict"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = self._fixture(root)
            failed = self._run({**environment, "FAKE_GO_FAIL": first})
            self.assertEqual(failed.returncode, 1)
            self.assertIn("verdict=fail", failed.stderr)

    def test_receipt_and_mapping_identity_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = self._fixture(root)
            wrong = self._run({**environment, "CANDIDATE_ID": "cand-2"})
            self.assertEqual(wrong.returncode, 2)
            self.assertIn("构建收据不存在", wrong.stderr)
            receipt = root / "campaign" / "candidates" / "cand-1" / "build-receipt.json"
            receipt.write_text(
                json.dumps({"candidate_id": "other", "source": {"root": str(root / "source"), "git_commit": "a" * 40}}) + "\n",
                encoding="utf-8",
            )
            mismatch = self._run(environment)
            self.assertEqual(mismatch.returncode, 2)
            self.assertIn("无法解析源码根", mismatch.stderr)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = self._fixture(root)
            version_mismatch = self._run({**environment, "CODEX_VERSION": "0.151.0"})
            self.assertEqual(version_mismatch.returncode, 2)
            self.assertIn("缺少目标版本的测试事实映射", version_mismatch.stderr)


class CandidateTraceTestScenarioTests(unittest.TestCase):
    def test_job_is_declared_in_scenarios_and_labels(self) -> None:
        scenarios = json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_154_0.json").read_text(encoding="utf-8"))
        job = next(item for item in scenarios["capture_jobs"] if item["id"] == "candidate-trace-test")
        self.assertEqual(job["phase"], "candidate")
        self.assertTrue(job["required"])
        # 模型轨道四字段全部省略：零请求 Job 沿用 Job 数据类默认（main 轨、不要求模型收据）。
        self.assertNotIn("required_model_receipt", job)
        self.assertNotIn("track", job)
        self.assertEqual(len(job["steps"]), 1)
        environment = job["steps"][0]["environment"]
        self.assertEqual(environment["CODEX_VERSION"], "{target_version}")
        self.assertEqual(environment["CAMPAIGN_DIR"], "{campaign_dir}")
        self.assertEqual(environment["CANDIDATE_ID"], "{candidate_id}")
        self.assertEqual(job["evidence_roots"], ["{capture_root}/runs/{campaign_id}-candidate-trace-test"])
        self.assertEqual(job["steps"][0]["argv"][-1], "{repo_root}/tools/official_client_capture/run_candidate_trace_test.sh")
        # covers 与冻结事实映射的 record_type 精确对应：每条规则至少有一个 check
        # 选择 go test 事实的 record_type。
        expectations = json.loads((TOOL_ROOT / "candidate_rule_expectations_0_154_0.json").read_text(encoding="utf-8"))
        mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
        fact_types = {fact["record_type"] for test in mapping["tests"] for fact in test["facts"]}
        covered = sorted(
            rule["rule_id"]
            for rule in expectations["rules"]
            if {check.get("select", {}).get("record_type") for check in rule.get("checks", [])} & fact_types
        )
        self.assertEqual(sorted(job["covers"]), covered)
        self.assertEqual(sorted(job["scenario_ids"]), sorted({fact["scenario_id"] for test in mapping["tests"] for fact in test["facts"]}))
        labels = json.loads((TOOL_ROOT / "codex_upgrade_evidence_labels_0_154_0.json").read_text(encoding="utf-8"))
        entry = next(item for item in labels["entries"] if item["job_id"] == "candidate-trace-test")
        self.assertEqual(entry["side"], "candidate")
        self.assertEqual([rule["glob"] for rule in entry["rules"]], ["candidate-go-test.jsonl"])
        self.assertEqual(sorted(entry["rules"][0]["scenario_ids"]), sorted(job["scenario_ids"]))
        # 分类 Candidate 复用是 v7 时代的九项冻结闭集，新 Job 不进入。
        self.assertNotIn("candidate-trace-test", codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS)
        self.assertEqual(len(codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS), 9)
        self.assertEqual(
            codex_upgrade.RUNTIME_SUCCESSOR_CHANGED_TOOL_PATH_JOB_IDS["run_candidate_trace_test.sh"],
            frozenset({"candidate-trace-test"}),
        )
        self.assertEqual(codex_upgrade._tool_component_for_path("run_candidate_trace_test.sh"), "producer")

    def test_job_loads_through_scenario_loader_with_expanded_context(self) -> None:
        context = {
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "campaign_id": "campaign-x",
            "candidate_id": "cand-1",
            "capture_root": "/root/oauth-capture",
            "output": "/data/evidence/campaigns/campaign-x",
            "campaign_dir": "/data/evidence/campaigns/campaign-x",
            "repo_root": "/data",
            "model": "gpt-5.5",
            "lite_model": "gpt-6-astra",
            "runtime_image": "image@sha256:" + "0" * 64,
            "target_sha256": "1" * 64,
            "profile_id": "p",
            "profile_digest": "2" * 64,
            "build_id": "b",
            "deployed_version": "0.154.0",
            "candidate_image_id": "sha256:" + "3" * 64,
            "source_tree_sha256": "4" * 64,
            "capture_container": "capture-cli",
            "service_container": "sub2apiplus",
            "keeper_container": "sub2apiplus-keeper",
            "postgres_container": "sub2apiplus-postgres",
            "redis_container": "sub2apiplus-redis",
            "capture_codex_bin": "/root/.local/bin/codex",
            "relay_codex_bin": "/root/.local/bin/codex",
            "codex_account_id": "1",
            "api_key_id": "1",
            "live_attestation_compose_dir": "/root/docker/sub2apiplus/app",
            "live_attestation_compose_files": "docker-compose.yml",
        }
        jobs = codex_upgrade.load_scenario_jobs(
            TOOL_ROOT / "codex_upgrade_scenarios_0_154_0.json",
            context,
            expected_version="0.154.0",
        )
        job = next(item for item in jobs if item.job_id == "candidate-trace-test")
        self.assertEqual(job.phase, "candidate")
        self.assertFalse(job.required_model_receipt)
        self.assertEqual(job.evidence_roots, ("/root/oauth-capture/runs/campaign-x-candidate-trace-test",))
        environment = job.steps[0]["environment"]
        self.assertEqual(environment["CAMPAIGN_DIR"], "/data/evidence/campaigns/campaign-x")
        self.assertEqual(environment["CANDIDATE_ID"], "cand-1")
        self.assertEqual(environment["RUN_ID"], "campaign-x-candidate-trace-test")
        self.assertEqual(codex_upgrade._job_tool_components(job), ("producer",))
        candidate_ids = sorted(item.job_id for item in jobs if item.phase == "candidate")
        self.assertEqual(len(candidate_ids), 10)


if __name__ == "__main__":
    unittest.main()
