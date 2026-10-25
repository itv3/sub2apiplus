"""记录库清理对补跑记录的处理（优化第 2 项，``tools/ci/entry_housekeeping.py``）：运行清单的 ``retries`` 把补跑尝试和它的首次失败记录都列出来，
被引用或保留期内的运行，这两条记录与各自的日志都跟着留；没人引用的旧运行连同它们一起删。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.ci import entry_housekeeping as hk
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


class DriverCopyTests(unittest.TestCase):
    def test_driver_carries_an_identical_copy(self) -> None:
        root = Path(hk.__file__).resolve().parents[2]
        self.assertEqual((root / "tools" / "arm64_capture_driver" / "driver" / "entry_housekeeping.py").read_bytes(),
                         (root / "tools" / "ci" / "entry_housekeeping.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
