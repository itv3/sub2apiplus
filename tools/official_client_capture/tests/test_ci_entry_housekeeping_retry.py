"""记录库清理对补跑记录的处理（优化第 2 项，``tools/ci/entry_housekeeping.py``）：运行清单的 ``retries`` 把补跑尝试和它的首次失败记录都列出来，
被引用或保留期内的运行，这两条记录与各自的日志都跟着留；没人引用的旧运行连同它们一起删。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.ci import entry_housekeeping as hk
from tools.ci import retry_policy as rp
from tools.ci import unit_records as ur
from tools.official_client_capture.tests.test_ci_entry_housekeeping import NOW, OLD, RECENT, _Store, _age


class RetryRecordsHousekeepingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.store_root = self.base / "unit-records"
        self.store = _Store(self.store_root)
        self.refs = self.base / "data" / "control"
        self.refs.mkdir(parents=True)

    def retried_run(self, run_id: str, when: float) -> tuple[dict, dict, Path]:
        """一次有补跑的运行：单元首次失败（记录 A）、补跑通过（记录 B）；清单 units 引用 B，retries 同时列出 B 与首次失败的 A。"""

        s = self.store
        first = s.record("capture:flaky", run_id, s.log(f"{run_id} 首次失败", when), when)
        retry = s.record("capture:flaky", run_id, s.log(f"{run_id} 补跑通过", when), when)
        units = [{"unit_id": "capture:flaky", "disposition": "executed", "record_sha256": retry["record_sha256"]}]
        retries = [{"unit_id": "capture:flaky", "record_sha256": retry["record_sha256"], "of": first["record_sha256"],
                    "of_record_path": str(s.record_file(first))}]
        manifest = ur.build_manifest(run_id=run_id, mode=ur.RE_EXECUTE, record_store=str(self.store_root), units=units, diagnostic=[], retries=retries)
        path = s.api.put_manifest(manifest)
        _age(path, when)
        return first, retry, path

    def gc(self) -> dict:
        return hk.gc_record_store(self.store_root, reference_roots=[self.refs.parent], now=NOW)

    def test_recent_run_keeps_both_attempts_and_both_logs(self) -> None:
        first, retry, manifest = self.retried_run("r-recent", RECENT)
        report = self.gc()
        self.assertEqual(report["status"], "done", report)
        for record in (first, retry):
            self.assertTrue(self.store.record_file(record).exists(), f"{record['record_sha256'][:8]} 的记录要留")
            self.assertTrue(self.store.api.log_path(record["log"]["sha256"]).exists(), "两次日志都要留")
        self.assertTrue(manifest.exists())

    def test_unreferenced_old_run_is_removed_with_both_attempts(self) -> None:
        first, retry, manifest = self.retried_run("r-old", OLD)
        self.gc()
        self.assertFalse(manifest.exists())
        for record in (first, retry):
            self.assertFalse(self.store.record_file(record).exists())

    def test_referenced_old_run_keeps_the_first_failed_attempt_too(self) -> None:
        first, retry, manifest = self.retried_run("r-ref", OLD)
        (self.refs / "pre-a3-path-certification.json").write_text(json.dumps({
            "schema_version": "pre-a3-path-certification/v2", "record_store": str(self.store_root),
            "unit_manifest": {"run_id": "r-ref", "manifest_sha256": "0" * 64, "mode": "re-execute"}}), encoding="utf-8")
        self.gc()
        self.assertTrue(manifest.exists())
        for record in (first, retry):
            self.assertTrue(self.store.record_file(record).exists(), "被引用的运行连补跑前的首次失败记录一起留")
            self.assertTrue(self.store.api.log_path(record["log"]["sha256"]).exists())


class RetryLedgerSurvivesCleanupTests(unittest.TestCase):
    """验收修正建议 13：记录库清理（保留期 30 天）不会动补跑账本——账本的累计规则是“最近 5 轮”，跨度远超保留期，被清理掉等于规则作废。

    账本在记录库的 ``retry-ledger/`` 下（或 ``UNIT_EXECUTOR_RETRY_LEDGER`` 指向的共享目录），清理只处理运行清单、记录、日志与逐单元承接目录。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.store_root = self.base / "unit-records"
        self.store = _Store(self.store_root)
        self.refs = self.base / "data" / "control"
        self.refs.mkdir(parents=True)

    def write_old_ledger(self, root: Path) -> rp.RetryLedger:
        ledger = rp.RetryLedger(root)
        ledger.append({"unit_id": "flaky:acc", "run_id": f"r{number}", "outcome": "retry-passed" if number == 0 else "passed"} for number in range(5))
        for path in root.iterdir():
            _age(path, OLD)       # 比保留期还老：清理若按文件年龄删，它会被删
        return ledger

    def test_an_old_ledger_in_the_record_store_survives_a_cleanup_and_still_blocks_a_second_retry(self) -> None:
        ledger = self.write_old_ledger(self.store_root / rp.LEDGER_DIRECTORY)
        before = ledger.allowance("flaky:acc")
        self.assertEqual((before["prior_retry_passed"], before["allowed"]), (1, False), "窗口里有一次补跑通过：不再放行")
        # 旧的、没人引用的运行与记录会被清走，账本不会。
        first = self.store.record("capture:old", "r-old", self.store.log("旧运行", OLD), OLD)
        self.store.manifest("r-old", OLD, executed=[first])
        report = hk.gc_record_store(self.store_root, reference_roots=[self.refs.parent], now=NOW)
        self.assertEqual(report["status"], "done", report)
        self.assertFalse(self.store.record_file(first).exists(), "没人引用的旧记录照常清走")
        self.assertEqual(ledger.allowance("flaky:acc"), before, "账本一字未变，累计规则照样生效")
        self.assertEqual(len(list((self.store_root / rp.LEDGER_DIRECTORY).glob("*.jsonl"))), 1)

    def test_the_shared_ledger_directory_outside_the_store_is_never_touched(self) -> None:
        shared = self.base / "gate-retry-ledger"
        ledger = self.write_old_ledger(shared)
        before = ledger.allowance("flaky:acc")
        hk.gc_record_store(self.store_root, reference_roots=[self.refs.parent], now=NOW)
        self.assertEqual(ledger.allowance("flaky:acc"), before)

    def test_a_dry_run_and_repeated_cleanups_also_leave_the_ledger_alone(self) -> None:
        ledger = self.write_old_ledger(self.store_root / rp.LEDGER_DIRECTORY)
        before = ledger.allowance("flaky:acc")
        for dry_run in (True, False, False):
            hk.gc_record_store(self.store_root, reference_roots=[self.refs.parent], now=NOW, dry_run=dry_run)
            self.assertEqual(ledger.allowance("flaky:acc"), before)


class DriverCopyTests(unittest.TestCase):
    def test_driver_carries_an_identical_copy(self) -> None:
        root = Path(hk.__file__).resolve().parents[2]
        self.assertEqual((root / "tools" / "arm64_capture_driver" / "driver" / "entry_housekeeping.py").read_bytes(),
                         (root / "tools" / "ci" / "entry_housekeeping.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
