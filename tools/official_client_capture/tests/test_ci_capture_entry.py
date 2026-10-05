"""CI 接入与回退的隔离验收；比较真实子进程结果，不依赖 GitHub 或生产账户。"""

import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools.ci import capture_ci as ci
from tools.ci import unit_executor as executor

ROOT = Path(__file__).resolve().parents[3]


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.ids = {"test_a": ["test_a.C.test_one", "test_a.C.test_two"],
                    "test_a+pkg": ["tools.official_client_capture.tests.test_a.C.test_one"],
                    "test_b": ["test_b.C.test_three"]}
        self.selection = self.root / "selection.json"
        self.document = {"schema_version": "unit-executor-selection/v1",
                         "full_test_ids_sha256": ci.digest(sorted(item for ids in self.ids.values() for item in ids)),
                         "test_ids": self.ids["test_a"] + self.ids["test_a+pkg"]}
        self.config = executor.ExecutorConfig(2, executor.Quota(1, 128), {}, {}, (), 30, 1, {})

    def prepare(self, document=None):
        self.selection.write_text(json.dumps(document or self.document))
        args = executor._parse(["plan", "--selection-file", str(self.selection)])
        with mock.patch.object(executor, "discover_test_ids", return_value=self.ids), \
             mock.patch.object(executor, "load_config", return_value=self.config), \
             mock.patch.object(executor, "load_weights", return_value={}), \
             mock.patch.object(executor, "load_durations", return_value={}):
            result = executor._prepare(args)
        return args, result

    def test_selection_keeps_alias_ids_in_same_old_shard(self):
        args, result = self.prepare()
        self.assertEqual(result[-1], "shard")
        self.assertEqual(sorted(test_id for unit in result[6] for test_id in unit.test_ids), sorted(self.document["test_ids"]))
        self.assertEqual(args.selection_binding["sha256"], ci.file_digest(self.selection))

    def test_unknown_duplicate_empty_and_partial_unit_ids_are_rejected(self):
        for ids in [["test_unknown.C.test_a"], [], self.document["test_ids"] * 2, ["test_a.C.test_one"]]:
            with self.subTest(ids=ids):
                document = {**self.document, "test_ids": ids}
                with self.assertRaises(executor.ExecutorError):
                    self.prepare(document)

    def test_full_set_digest_and_schema_drift_are_rejected(self):
        for changes in [{"full_test_ids_sha256": "0" * 64}, {"schema_version": "unknown"}, {"unknown": True}]:
            with self.subTest(changes=changes), self.assertRaises(executor.ExecutorError):
                self.prepare({**self.document, **changes})

    def test_full_configuration_is_checked_before_selecting(self):
        self.config = executor.ExecutorConfig(2, executor.Quota(1, 128), {}, {"test_missing": 2}, (), 30, 1, {})
        with self.assertRaisesRegex(executor.ExecutorError, "不存在的模块"):
            self.prepare()


class CaptureEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.tests = self.root / "tests"
        self.tests.mkdir()
        self.tests.joinpath("test_alpha.py").write_text(
            "import unittest\nclass C(unittest.TestCase):\n"
            " def test_a(self): self.assertEqual(2 + 2, 4)\n"
            " @unittest.skip('隔离跳过示例')\n def test_b(self): pass\n")
        self.tests.joinpath("test_beta.py").write_text(
            "import unittest\nclass C(unittest.TestCase):\n"
            " def test_c(self): self.assertTrue(True)\n"
            " @unittest.expectedFailure\n def test_d(self): self.fail('隔离预期失败')\n")
        self.weights = self.root / "weights.json"
        self.weights.write_text(json.dumps({"schema_version": executor.WEIGHTS_SCHEMA,
                                           "weights": {"test_alpha": 2, "test_beta": 1}}))
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"schema_version": executor.CONFIG_SCHEMA, "default_parallelism": 2,
            "default_quota": {"cores": 1, "memory_mb": 128}, "quotas": {}, "splits": {}, "exclusive": [],
            "unit_timeout_seconds": 30, "orphan_grace_seconds": 1}))
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")}
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def run_entry(self, name, mode="unified", index=1, count=2, extra_env=None, bytecode_cache="off", identity_memo="off"):
        output = self.root / name
        argv = [sys.executable, "-B", str(ROOT / "tools/ci/capture_ci.py"), "--count", str(count), "--index", str(index),
                "--executor", mode, "--start", str(self.tests), "--weights", str(self.weights), "--config", str(self.config),
                "--durations", str(self.root / "no-durations.json"), "--out-dir", str(output), "--parallel", "2", "--cores", "2"]
        if bytecode_cache is not None:
            argv.extend(["--bytecode-cache", bytecode_cache])
        if identity_memo is not None:
            argv.extend(["--identity-memo", identity_memo])
        result = subprocess.run(argv, cwd=ROOT, env={**self.environment, **(extra_env or {})}, capture_output=True, text=True, timeout=180)
        receipt = json.loads((output / "receipt.json").read_text()) if (output / "receipt.json").is_file() else None
        if result.returncode:
            summary = output / "executor/summary.json"
            result.stdout += "\n" + json.dumps({"receipt": receipt,
                "summary": json.loads(summary.read_text()) if summary.is_file() else None}, ensure_ascii=False)
        return result, receipt, output

    def test_all_shards_match_legacy_results_identity_and_coverage(self):
        union = []
        for index in (1, 2):
            old, old_receipt, old_out = self.run_entry(f"legacy-{index}", "legacy", index)
            new, new_receipt, new_out = self.run_entry(f"unified-{index}", "unified", index)
            self.assertEqual(old.returncode, 0, old.stderr + old.stdout)
            self.assertEqual(new.returncode, 0, new.stderr + new.stdout)
            self.assertEqual(old_receipt["source"], new_receipt["source"])
            self.assertEqual(old_receipt["outcomes_sha256"], new_receipt["outcomes_sha256"])
            self.assertEqual(old_receipt["counts"], new_receipt["counts"])
            old_plan = json.loads((old_out / "plan.json").read_text())
            new_plan = json.loads((new_out / "plan.json").read_text())
            self.assertEqual(old_plan, new_plan)
            union += new_plan["test_ids"]
            for receipt, output in ((old_receipt, old_out), (new_receipt, new_out)):
                self.assertTrue(receipt["coverage_closed"])
                self.assertTrue(receipt["source_unchanged"])
                self.assertGreater(receipt["elapsed_seconds"], 0)
                self.assertFalse(receipt["automatic_fallback"])
                for relative, expected in receipt["files"].items():
                    self.assertEqual(ci.file_digest(output / relative), expected)
        self.assertEqual(len(union), 4)
        self.assertEqual(len(union), len(set(union)))

    def test_failure_never_automatically_falls_back_or_promotes_diagnostic(self):
        self.tests.joinpath("test_alpha.py").write_text("import unittest\nclass C(unittest.TestCase):\n def test_a(self): self.fail('必须保留失败')\n")
        for mode in ("unified", "legacy"):
            result, receipt, output = self.run_entry(mode, mode)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["counts"], {"failed": 1})
            self.assertFalse(receipt["automatic_fallback"])
            self.assertTrue((output / "execution.log").is_file())

    def test_default_bytecode_is_shared_read_only_without_external_caches_or_memo(self):
        """两个并行单元都必须真正加载预编译字节码，禁止源码回编及外部身份缓存泄漏。"""
        self.tests.joinpath("cache_probe_payload.py").write_text("VALUE = 7\n")
        for name in ("alpha", "beta"):
            target = self.root / (name + "-probe.json")
            self.tests.joinpath("test_" + name + ".py").write_text(
                "import hashlib, importlib.machinery, json, os, sys, unittest\n"
                "from pathlib import Path\nfrom unittest import mock\n"
                "class C(unittest.TestCase):\n def test_cache(self):\n"
                "  with mock.patch.object(importlib.machinery.SourceFileLoader, 'source_to_code', side_effect=AssertionError('缓存未命中')):\n"
                "   import cache_probe_payload as payload\n"
                "  self.assertEqual(payload.VALUE, 7)\n"
                "  cached = Path(payload.__cached__)\n"
                "  self.assertEqual(int.from_bytes(cached.read_bytes()[4:8], 'little'), 3)\n"
                f"  Path({str(target)!r}).write_text(json.dumps({{'prefix': sys.pycache_prefix, 'cached': str(cached), "
                "'dont_write': sys.dont_write_bytecode, 'memo': os.environ.get('CODEX_UPGRADE_IDENTITY_MEMO'), "
                "'sha256': hashlib.sha256(cached.read_bytes()).hexdigest()}))\n")
        foreign = self.root / "foreign-pycache"
        foreign.mkdir()
        result, receipt, output = self.run_entry("cached", count=1, bytecode_cache=None,
            extra_env={"PYTHONPYCACHEPREFIX": str(foreign), "CODEX_UPGRADE_IDENTITY_MEMO": str(self.root / "foreign-memo")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prefix = str(output / "executor/pycache-shared")
        self.assertEqual(receipt["bytecode_cache_mode"], "auto")
        self.assertEqual(receipt["detail"]["bytecode_cache"]["status"], "ready")
        self.assertIsNone(receipt["detail"]["identity_memo"])
        for name in ("alpha", "beta"):
            probe = json.loads((self.root / (name + "-probe.json")).read_text())
            self.assertEqual(probe["prefix"], prefix)
            self.assertTrue(probe["cached"].startswith(prefix + "/") and probe["dont_write"])
            self.assertIsNone(probe["memo"])
            self.assertEqual(ci.file_digest(Path(probe["cached"])), probe["sha256"])
        self.assertEqual(list(foreign.iterdir()), [])
        self.assertFalse((output / "executor/identity-memo").exists())
        self.assertEqual(list(self.tests.rglob("__pycache__")), [])

    def test_bytecode_off_preserves_outcomes_and_clears_external_prefix(self):
        cached, cached_receipt, _ = self.run_entry("cached", count=1, bytecode_cache="auto")
        cold, cold_receipt, output = self.run_entry("cold", count=1, bytecode_cache="off",
            extra_env={"PYTHONPYCACHEPREFIX": str(self.root / "foreign"), "CODEX_UPGRADE_IDENTITY_MEMO": str(self.root / "memo")})
        for result in (cached, cold):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(cached_receipt["outcomes_sha256"], cold_receipt["outcomes_sha256"])
        self.assertEqual(cached_receipt["source"], cold_receipt["source"])
        self.assertEqual(cold_receipt["detail"]["bytecode_cache"]["status"], "off")
        self.assertIsNone(cold_receipt["detail"]["bytecode_cache"]["prefix"])
        self.assertIsNone(cold_receipt["detail"]["identity_memo"])
        self.assertFalse((output / "executor/pycache-shared").exists())

    def test_precompile_failure_keeps_ci_failed_when_formal_tests_pass(self):
        self.tests.joinpath("cache_bad_payload.py").write_text("if :\n")
        result, receipt, output = self.run_entry("broken-cache", count=1, bytecode_cache="auto")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["detail"]["bytecode_cache"]["status"], "failed")
        summary = json.loads((output / "executor/summary.json").read_text())
        self.assertEqual(summary["failed_units"], [])
        self.assertTrue(receipt["coverage_closed"])
        self.assertFalse(receipt["automatic_fallback"])

    def test_default_identity_directory_is_per_run_and_external_memo_is_cleared(self):
        foreign = self.root / "foreign-memo"
        foreign.mkdir()
        receipts = []
        for name, bytecode in (("one", "off"), ("two", "auto")):
            result, receipt, output = self.run_entry(name, count=1, identity_memo=None, bytecode_cache=bytecode,
                extra_env={"CODEX_UPGRADE_IDENTITY_MEMO": str(foreign)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(receipt["identity_memo_mode"], "auto")
            self.assertEqual(receipt["detail"]["identity_memo"], str(output / "executor/identity-memo"))
            self.assertEqual(receipt["detail"]["dispatch_verification"]["status"], "passed")
            receipts.append(receipt)
        disabled, receipt, _ = self.run_entry("off", count=1, identity_memo="off", bytecode_cache="auto",
            extra_env={"CODEX_UPGRADE_IDENTITY_MEMO": str(foreign)})
        self.assertEqual(disabled.returncode, 0, disabled.stdout + disabled.stderr)
        self.assertEqual(receipt["identity_memo_mode"], "off")
        self.assertIsNone(receipt["detail"]["identity_memo"])
        self.assertEqual(len({row["detail"]["identity_memo"] for row in receipts}), 2)
        self.assertTrue(all(row["outcomes_sha256"] == receipt["outcomes_sha256"] for row in receipts))
        self.assertEqual(list(foreign.iterdir()), [])

    def test_registered_exclusive_test_is_verified_from_real_dispatch_events(self):
        config = json.loads(self.config.read_text())
        config["exclusive"] = [{"tests": "test_alpha.C.test_a", "reason": "隔离计时敏感用例"}]
        self.config.write_text(json.dumps(config))
        result, receipt, output = self.run_entry("exclusive", count=1, identity_memo="auto")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        audit = receipt["detail"]["dispatch_verification"]
        self.assertEqual((audit["exclusive_tests"], audit["exclusive_units"], audit["formal_units"]), (1, 1, 3))
        self.assertEqual(audit["events_sha256"], ci.file_digest(output / "executor/events.jsonl"))
        config["exclusive"][0]["tests"] = "test_missing.C.test_a"
        self.config.write_text(json.dumps(config))
        result, receipt, output = self.run_entry("stale-exclusive", count=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("独占登记", receipt["reason"])
        self.assertFalse((output / "execution.log").exists())

    def test_changed_test_source_is_rejected_after_execution(self):
        self.tests.joinpath("test_alpha.py").write_text(
            "import unittest\nfrom pathlib import Path\nclass C(unittest.TestCase):\n"
            " def test_a(self):\n  p=Path(__file__); p.write_text(p.read_text() + '# 修改输入\\n')\n")
        result, receipt, _ = self.run_entry("changed", "legacy")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(receipt["source_unchanged"])

    def test_existing_output_is_not_overwritten(self):
        result, _, output = self.run_entry("once", "legacy")
        self.assertEqual(result.returncode, 0)
        original = (output / "receipt.json").read_bytes()
        result, _, _ = self.run_entry("once", "unified")
        self.assertEqual(result.returncode, 2)
        self.assertEqual((output / "receipt.json").read_bytes(), original)

    def test_empty_invalid_and_stale_shards_fail_before_execution(self):
        for index, count in ((0, 2), (3, 2), (4, 4)):
            result, receipt, output = self.run_entry(f"bad-{index}-{count}", index=index, count=count)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(receipt["status"], "failed")
            self.assertFalse((output / "execution.log").exists())
        self.weights.write_text(json.dumps({"schema_version": executor.WEIGHTS_SCHEMA, "weights": {"test_missing": 1}}))
        result, receipt, _ = self.run_entry("stale")
        self.assertEqual(result.returncode, 2)
        self.assertIn("不存在", receipt["reason"])

    def test_wrong_ci_commit_is_rejected_without_running_tests(self):
        result, receipt, output = self.run_entry("wrong-ci", extra_env={"GITHUB_ACTIONS": "true", "GITHUB_SHA": "0" * 40})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse((output / "execution.log").exists())

    def test_sigterm_aborts_unified_and_reaps_running_test(self):
        marker = self.root / "running-pid"
        self.tests.joinpath("test_alpha.py").write_text(
            "import unittest, os, time\nfrom pathlib import Path\nclass C(unittest.TestCase):\n"
            f" def test_a(self):\n  Path({str(marker)!r}).write_text(str(os.getpid()))\n  time.sleep(60)\n")
        output = self.root / "interrupted"
        command = [sys.executable, "-B", str(ROOT / "tools/ci/capture_ci.py"),
                   "--count", "2", "--index", "1", "--start", str(self.tests),
                   "--weights", str(self.weights), "--config", str(self.config),
                   "--durations", str(self.root / "no-durations.json"), "--out-dir", str(output)]
        process = subprocess.Popen(command, cwd=ROOT, env=self.environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "测试进程未启动")
            child_pid = int(marker.read_text())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=40)
            self.assertEqual(process.returncode, 143, stdout + stderr)
            receipt = json.loads((output / "receipt.json").read_text())
            self.assertEqual(receipt["status"], "aborted")
            self.assertFalse(receipt["automatic_fallback"])
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=40)


class DispatchVerificationTests(unittest.TestCase):
    """损坏的派发记录必须失败关闭，即便测试结果自称通过。"""

    def setUp(self):
        self.plan = {"test_ids": ["test_a", "test_b"], "exclusive_test_ids": ["test_b"]}
        self.execution = {"units": [{"unit_id": name, "tests": ["test_" + name], "exclusive": name == "b"}
                                     for name in ("a", "b")]}
        self.summary = {"units": [{"unit_id": name, "kind": "formal", "exclusive": name == "b"}
                                  for name in ("a", "b")], "diagnostic": []}
        self.events = [{"event": event, "unit": name, "kind": "formal", "pid": pid,
                        "running": [name] if event == "start" else []}
                       for name, pid in (("a", 11), ("b", 12)) for event in ("start", "exit")]

    def verify(self):
        return ci.verify_dispatch(self.plan, self.execution, self.events, self.summary)

    def test_complete_dispatch_is_accepted(self):
        self.assertEqual(self.verify()["start_exit_pairs"], 2)

    def test_overlapping_exclusive_is_rejected(self):
        self.events[1], self.events[2] = self.events[2], self.events[1]
        self.events[1]["running"] = ["a", "b"]
        with self.assertRaisesRegex(ci.shards.ShardError, "重叠"):
            self.verify()

    def test_wrong_flags_and_missing_test_are_rejected(self):
        original = copy.deepcopy(self.execution)
        for changes in ({"exclusive": False}, {"tests": ["test_b", "test_a"]}, {"tests": []}):
            self.execution = copy.deepcopy(original)
            self.execution["units"][1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ci.shards.ShardError):
                self.verify()

    def test_missing_duplicate_unpaired_and_wrong_running_events_are_rejected(self):
        original = copy.deepcopy(self.events)
        for events in (original[:-1], original[:2], original + original[2:], original[1:],
                       [dict(original[0], running=[])] + original[1:]):
            self.events = events
            with self.subTest(events=events), self.assertRaises(ci.shards.ShardError):
                self.verify()

    def test_missing_or_diagnostic_formal_summary_is_rejected(self):
        original = copy.deepcopy(self.summary["units"])
        for units in (original[:1], original + original[:1], [dict(row, kind="diagnostic") for row in original]):
            self.summary["units"] = units
            with self.subTest(units=units), self.assertRaises(ci.shards.ShardError):
                self.verify()


if __name__ == "__main__":
    unittest.main()
