"""入口日常维护（E4-02，``tools/ci/entry_housekeeping.py``）：记录库清理（保留期、被 v2 认证与 P0 v2 证据引用的运行整份保留、
保留记录的日志与原运行清单、与发布并发时放回、中断后放回、不并发、只算不删）、发布方续期、Go 编译缓存只删久未用的条目、
空跑只留最近几次、部署留下的旧暂存树每组只留最近两份（E4-03）、根盘停线与 guard.sh 同一条。

记录库用 ``unit_records`` 的真实写入接口在临时目录里造，文件新旧用修改时间摆出来；清理时刻按参数给定，不依赖墙钟。
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

from tools.ci import entry_housekeeping as hk
from tools.ci import unit_records as ur

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE = REPO_ROOT / "tools" / "ci" / "entry_housekeeping.py"
DRIVER_COPY = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry_housekeeping.py"
NOW = time.time()     # 发布方续期用墙钟，清理时刻与它对齐
DAY = 86400.0
OLD = NOW - 40 * DAY          # 早于默认保留期（30 天）
RECENT = NOW - 2 * DAY


def _age(path: Path, when: float) -> None:
    os.utime(path, (when, when))


class _Store:
    """在临时目录里用真实写入接口造记录库：运行清单、执行记录（本次执行／承接）、日志。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.api = ur.RecordStore(root)
        self.log_dir = root.parent / "raw-logs"
        self.log_dir.mkdir(exist_ok=True)

    def log(self, text: str, when: float) -> str:
        source = self.log_dir / f"{len(list(self.log_dir.iterdir()))}.log"
        source.write_text(text, encoding="utf-8")
        digest = ur.file_sha256(source)
        _age(self.api.put_log(source, digest), when)
        return digest

    def record(self, unit_id: str, run_id: str, log: str, when: float) -> dict:
        record = ur.seal_record({"schema_version": ur.RECORD_SCHEMA, "unit_id": unit_id, "unit_type": "command", "kind": "formal",
                                 "run": {"run_id": run_id}, "log": {"sha256": log}, "passed": True, "exit_code": 0,
                                 "signal": None, "timed_out": False})
        _age(self.api.put_record(record), when)
        return record

    def manifest(self, run_id: str, when: float, *, executed: list[dict] = (), inherited: list[dict] = (),
                 diagnostic: list[dict] = ()) -> Path:
        units = [{"unit_id": record["unit_id"], "disposition": "executed", "record_sha256": record["record_sha256"]} for record in executed]
        units += [{"unit_id": record["unit_id"], "disposition": "inherited", "record_sha256": record["record_sha256"],
                   "basis": {"run_id": record["run"]["run_id"]}} for record in inherited]
        manifest = ur.build_manifest(run_id=run_id, mode=ur.FULL_SET_PASS, record_store=str(self.root), units=units,
                                     diagnostic=[{"unit_id": record["unit_id"], "record_sha256": record["record_sha256"]}
                                                 for record in diagnostic])
        path = self.api.put_manifest(manifest)
        _age(path, when)
        return path

    def record_file(self, record: dict) -> Path:
        return self.api.record_path(record["unit_id"], record["record_sha256"])


class RecordStoreCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.store_root = self.base / "unit-records"
        self.store = _Store(self.store_root)
        self.refs = self.base / "data" / "control"
        self.refs.mkdir(parents=True)

    def reference(self, name: str, run_id: str, *, schema: str = "pre-a3-path-certification/v2", store: Path | None = None,
                  directory: Path | None = None) -> Path:
        path = (directory or self.refs) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": schema, "record_store": str(store or self.store_root),
                                    "unit_manifest": {"run_id": run_id, "manifest_sha256": "0" * 64, "mode": "full-set-pass"}}),
                        encoding="utf-8")
        return path

    def gc(self, **kwargs: object) -> dict:
        return hk.gc_record_store(self.store_root, reference_roots=[self.refs.parent], now=NOW, **kwargs)

    def test_keeps_recent_and_referenced_runs_with_what_their_verification_reads(self) -> None:
        s = self.store
        # 没人引用的旧运行：清单、两条记录、两份日志都该删。
        stale = [s.record(f"capture:stale-{n}", "r-stale", s.log(f"stale {n}", OLD), OLD) for n in range(2)]
        stale_manifest = s.manifest("r-stale", OLD, executed=stale)
        # 原运行：B 被引用运行承接；C 只在原运行里。
        b = s.record("capture:b", "r-origin", s.log("b", OLD), OLD)
        c = s.record("capture:c", "r-origin", s.log("c", OLD), OLD)
        origin = s.manifest("r-origin", OLD, executed=[b, c])
        # 被 v2 认证引用的旧运行：本次执行 A、承接 B、诊断 Z。
        a = s.record("capture:a", "r-ref", s.log("a", OLD), OLD)
        z = s.record("capture:z", "r-ref", s.log("z", OLD), OLD)
        referenced = s.manifest("r-ref", OLD, executed=[a], inherited=[b], diagnostic=[z])
        self.reference("pre-a3-path-certification.json", "r-ref")
        # 保留期内的运行：本次执行 D、承接旧运行里的 E（E 的日志、原运行清单跟着留）。
        e = s.record("capture:e", "r-old-origin", s.log("e", OLD), OLD)
        old_origin = s.manifest("r-old-origin", OLD, executed=[e])
        d = s.record("capture:d", "r-recent", s.log("d", RECENT), RECENT)
        recent = s.manifest("r-recent", RECENT, executed=[d], inherited=[e])
        # 保留期内写入、还没有清单的记录（在跑或被停下的运行）：它的日志是同内容寻址的旧文件。
        f = s.record("capture:f", "r-running", s.log("stale 0", OLD), RECENT)

        report = self.gc()
        self.assertEqual(report["status"], "done", report)
        for path in (stale_manifest, *map(s.record_file, stale), s.api.log_path(stale[1]["log"]["sha256"]), s.record_file(c),
                     s.api.log_path(c["log"]["sha256"])):
            self.assertFalse(path.exists(), f"没人引用的旧文件要删：{path}")
        for path in (referenced, origin, s.record_file(a), s.record_file(b), s.record_file(z), recent, old_origin, s.record_file(d),
                     s.record_file(e), s.record_file(f)):
            self.assertTrue(path.exists(), f"该留的被删了：{path}")
        for record in (a, b, z, d, e, f):
            self.assertTrue(s.api.log_path(record["log"]["sha256"]).exists(), f"{record['unit_id']} 的日志要留")
        self.assertEqual(report["referenced_runs"], 1)
        self.assertEqual(report["recent_runs"], 1)
        self.assertEqual(report["counts"]["runs"], {"total": 5, "kept": 4, "removed": 1, "removed_bytes": report["counts"]["runs"]["removed_bytes"]})
        self.assertEqual(report["counts"]["records"]["removed"], 3)
        self.assertEqual(report["counts"]["logs"]["removed"], 2, "stale-0 的日志被保留期内的 F 引用，留着")

    def test_dry_run_counts_without_removing(self) -> None:
        s = self.store
        stale = s.record("capture:x", "r-x", s.log("x", OLD), OLD)
        manifest = s.manifest("r-x", OLD, executed=[stale])
        report = self.gc(dry_run=True)
        self.assertEqual((report["counts"]["runs"]["removed"], report["counts"]["records"]["removed"], report["counts"]["logs"]["removed"]),
                         (1, 1, 1))
        self.assertTrue(manifest.exists() and s.record_file(stale).exists())

    def test_reference_shapes_other_stores_skipped_directories_and_dangling_references(self) -> None:
        s = self.store
        kept = s.record("capture:p0", "r-p0", s.log("p0", OLD), OLD)
        s.manifest("r-p0", OLD, executed=[kept])
        other = s.record("capture:other", "r-other", s.log("other", OLD), OLD)
        s.manifest("r-other", OLD, executed=[other])
        tools = s.record("capture:tools", "r-tools", s.log("tools", OLD), OLD)
        s.manifest("r-tools", OLD, executed=[tools])
        evidence = self.base / "data" / "evidence"
        self.reference("p0/test-capture-tools.json", "r-p0", schema="codex-p0-offline-gate-evidence/v2", directory=evidence)
        self.reference("other-store.json", "r-other", store=self.base / "elsewhere")
        self.reference("codex-entry-refactor-managed-tools/fixture.json", "r-tools", directory=self.base / "data" / "staging")
        link_target = self.reference("../outside.json", "r-tools", directory=self.base / "data" / "staging")
        (self.base / "data" / "staging" / "link.json").symlink_to(link_target)
        self.reference("gone.json", "r-gone")
        report = hk.gc_record_store(self.store_root, reference_roots=[self.base / "data" / "control", evidence, self.base / "data" / "staging"],
                                    now=NOW)
        self.assertTrue(s.record_file(kept).exists(), "P0 v2 证据引用的运行留着")
        self.assertFalse(s.record_file(other).exists(), "登记别的记录库的文档不算引用")
        self.assertFalse(s.record_file(tools).exists(), "受管工具树副本里的文件与符号链接不算引用")
        self.assertEqual(list(report["dangling_references"]), ["r-gone"])

    def test_renewed_file_is_put_back_and_publisher_renews_existing_logs(self) -> None:
        s = self.store
        digest = s.log("same output", OLD)
        path = s.api.log_path(digest)
        _age(path, OLD)
        source = s.log_dir / "again.log"
        source.write_text("same output", encoding="utf-8")
        self.assertEqual(s.api.put_log(source, digest), path)
        self.assertGreater(path.stat().st_mtime, NOW - 5 * DAY, "发布方遇到同名文件刷新修改时间（续期）")
        self.assertEqual(hk._remove(path, NOW - 30 * DAY), ("renewed", 0), "移走后复查：被续期的放回")
        self.assertTrue(path.exists())
        self.assertEqual([item.name for item in path.parent.iterdir() if hk.TRASH_MARK in item.name], [])
        _age(path, OLD)
        self.assertEqual(hk._remove(path, NOW - 30 * DAY)[0], "removed")
        self.assertFalse(path.exists())

    def test_trash_left_by_an_interrupted_run_is_put_back_and_judged_again(self) -> None:
        s = self.store
        record = s.record("capture:r", "r-recent", s.log("r", RECENT), RECENT)
        s.manifest("r-recent", RECENT, executed=[record])
        path = s.record_file(record)
        trash = path.with_name(f".{path.name}{hk.TRASH_MARK}4242")
        os.rename(path, trash)
        report = self.gc()
        self.assertEqual(report["restored_from_trash"], 1)
        self.assertTrue(path.exists(), "保留期内的记录放回后留着")
        self.assertFalse(trash.exists())

    def test_old_temporary_files_are_removed(self) -> None:
        (self.store_root / "logs").mkdir(parents=True, exist_ok=True)
        old = self.store_root / "logs" / ".x.log.123.ab.tmp"
        fresh = self.store_root / "logs" / ".y.log.124.cd.tmp"
        old.write_text("", encoding="utf-8")
        fresh.write_text("", encoding="utf-8")
        _age(old, NOW - 2 * DAY)
        _age(fresh, NOW - 60)
        self.assertEqual(self.gc()["temporary_removed"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_cli_judges_the_store_at_a_given_time_and_exits_5_over_the_line(self) -> None:
        s = self.store
        record = s.record("capture:n", "r-n", s.log("n", RECENT), RECENT)
        s.manifest("r-n", RECENT, executed=[record])
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 60 * DAY))
        argv = ["run", "--data-root", str(self.base / "data"), "--runroot", str(self.base / "run"), "--record-store", str(self.store_root),
                "--go-cache", "off", "--store-now-utc", later]

        def main(used: int, arguments: list[str]) -> tuple[int, str]:
            fake = types.SimpleNamespace(total=100, used=used, free=10 ** 12)
            with unittest.mock.patch.object(hk.shutil, "disk_usage", return_value=fake), contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()):
                code = hk.main(arguments)
            return code, out.getvalue()

        code, output = main(50, [*argv, "--dry-run"])
        self.assertEqual((code, json.loads(output)["record_store"]["counts"]["records"]["removed"]), (0, 1))
        self.assertTrue(s.record_file(record).exists(), "只算不删")
        code, output = main(90, argv)
        self.assertEqual(code, 5, "根盘越过停线退出 5（维护照做）")
        self.assertFalse(s.record_file(record).exists(), "按给定时刻已过保留期")
        self.assertEqual(main(50, [*argv[:-1], "tomorrow"])[0], 2)

    def test_retention_floor_absent_store_and_concurrent_cleanup(self) -> None:
        with self.assertRaises(hk.HousekeepingError):
            self.gc(retention_hours=ur.MAX_AGE_HOURS)
        self.assertEqual(hk.gc_record_store(self.base / "missing", reference_roots=[], now=NOW)["status"], "absent")
        self.store_root.mkdir(parents=True, exist_ok=True)
        with open(self.store_root / hk.GC_LOCK, "w", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertEqual(self.gc()["status"], "busy", "两次清理不同时跑")


class GoCacheAndDryRunRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()

    def test_go_cache_drops_only_entries_unused_for_six_hours(self) -> None:
        cache = self.base / "go-build"
        (cache / "ab").mkdir(parents=True)
        (cache / "fuzz").mkdir()
        files = {name: cache / name for name in ("ab/old-a", "ab/fresh-d", "fuzz/corpus", "README", "trim.txt")}
        for name, path in files.items():
            path.write_bytes(b"0123456789")
            _age(path, NOW - 7 * 3600 if name != "ab/fresh-d" else NOW - 5 * 3600)
        (cache / "ab" / "link").symlink_to(files["README"])
        report = hk.trim_go_cache(cache, now=NOW)
        self.assertEqual((report["removed_files"], report["removed_bytes"], report["kept_files"]), (1, 10, 1))
        self.assertEqual(sorted(name for name, path in files.items() if path.exists()), ["README", "ab/fresh-d", "fuzz/corpus", "trim.txt"])
        self.assertTrue((cache / "ab" / "link").is_symlink())
        self.assertEqual(hk.trim_go_cache(None)["status"], "absent")

    def test_go_cache_location_follows_gocache_then_go_env(self) -> None:
        self.assertEqual(hk.go_cache_dir("auto", {"GOCACHE": str(self.base)}), self.base)
        self.assertIsNone(hk.go_cache_dir("auto", {"GOCACHE": "off"}))
        self.assertIsNone(hk.go_cache_dir("off", {"GOCACHE": str(self.base)}))
        self.assertEqual(hk.go_cache_dir(str(self.base / "x")), self.base / "x")

    def test_dry_runs_keep_the_newest_five_and_running_ones(self) -> None:
        data, runroot = self.base / "data", self.base / "run"
        stamps = [f"202610{day:02d}t000000z" for day in range(1, 9)]
        for stamp in stamps:
            (data / "staging" / f"entry-dryrun-{stamp}").mkdir(parents=True)
            (runroot / "entry-dryrun" / f"run-{stamp}").mkdir(parents=True)
            (data / "staging" / f"entry-dryrun-{stamp}" / "x").write_bytes(b"12345")
        (data / "staging" / "entry-dryrun-notes").mkdir()
        report = hk.prune_dryruns(data, runroot, keep=5, busy=[stamps[0]])
        left = sorted(path.name for path in (data / "staging").iterdir())
        self.assertEqual(left, ["entry-dryrun-20261001t000000z", *[f"entry-dryrun-{stamp}" for stamp in stamps[3:]], "entry-dryrun-notes"],
                         "留最近 5 次与在跑的那次，不认识的目录不动")
        self.assertEqual(sorted(path.name for path in (runroot / "entry-dryrun").iterdir()),
                         ["run-20261001t000000z", *[f"run-{stamp}" for stamp in stamps[3:]]])
        self.assertEqual((len(report["removed"]), report["removed_bytes"]), (4, 10))
        with self.assertRaises(hk.HousekeepingError):
            hk.prune_dryruns(data, runroot, keep=0)

    def test_superseded_staging_trees_keep_the_newest_two_per_tree(self) -> None:
        """E4-03：部署留下的旧暂存树按名字分组、每组只留最近两份；当前暂存树不在的那组、名字对不上的、符号链接都不动。"""

        data = self.base / "data"
        staging = data / "staging"
        for name in ("codex-entry-refactor-managed-tools", "codex-0.160.0-managed-tools"):
            (staging / name).mkdir(parents=True)
        entry = [f"codex-entry-refactor-managed-tools.superseded-2026100{day}t000000z" for day in (1, 2, 3)]
        entry.append("codex-entry-refactor-managed-tools.superseded-20261003t000000z-4242")   # 同一时刻、名字带进程号
        target = ["codex-0.160.0-managed-tools.superseded-20261004t000000z"]
        orphan = [f"codex-0.157.0-managed-tools.superseded-2026092{day}t000000z" for day in (5, 6, 7)]
        for name in (*entry, *target, *orphan):
            (staging / name).mkdir()
            (staging / name / "f").write_bytes(b"123")
        (staging / "codex-entry-refactor-managed-tools.superseded-notes").mkdir()
        link = staging / "codex-entry-refactor-managed-tools.superseded-20250101t000000z"
        link.symlink_to(staging / entry[2], target_is_directory=True)
        dry = hk.prune_superseded_staging(data, dry_run=True)
        self.assertEqual(dry["removed"], entry[:2])
        self.assertTrue(all((staging / name).is_dir() for name in entry), "只算不删")
        report = hk.prune_superseded_staging(data)
        self.assertEqual((report["removed"], report["removed_bytes"]), (entry[:2], 6))
        self.assertEqual(report["groups"], {"codex-0.157.0-managed-tools": {"total": 3, "kept": 3, "removed": 0},
                                            "codex-0.160.0-managed-tools": {"total": 1, "kept": 1, "removed": 0},
                                            "codex-entry-refactor-managed-tools": {"total": 4, "kept": 2, "removed": 2}})
        self.assertEqual(report["skipped_without_current_tree"], ["codex-0.157.0-managed-tools"])
        self.assertEqual({path.name for path in staging.iterdir()},
                         {"codex-entry-refactor-managed-tools", "codex-0.160.0-managed-tools", *entry[2:], *target, *orphan,
                          "codex-entry-refactor-managed-tools.superseded-notes", link.name})
        self.assertTrue(link.is_symlink())
        with self.assertRaises(hk.HousekeepingError):
            hk.prune_superseded_staging(data, keep=-1)


class DiskLineTests(unittest.TestCase):
    def usage(self, used_percent: float, free_gib: float) -> object:
        total = 142 * 2 ** 30
        fake = types.SimpleNamespace(total=total, used=int(total * used_percent / 100), free=int(free_gib * 2 ** 30))
        return unittest.mock.patch.object(hk.shutil, "disk_usage", return_value=fake)

    def test_same_line_as_the_dispatch_guard(self) -> None:
        cases = [((67.0, 47.0), 40, True), ((69.5, 47.0), 40, False), ((60.0, 39.0), 40, False), ((60.0, 45.0), 50, False),
                 ((60.0, 45.0), 10, True)]
        for (used, free), floor, expected in cases:
            with self.usage(used, free):
                status = hk.disk_status(Path("/"), min_free_gib=floor)
            self.assertEqual(status["ok"], expected, (used, free, floor, status))
        with self.usage(70.0, 40.0):
            self.assertIn("根盘越过停线", hk.disk_reason(hk.disk_status(Path("/"))))

    def test_run_reports_every_step_and_the_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.usage(50.0, 60.0):
            base = Path(directory).resolve()
            report = hk.run(data_root=base / "data", runroot=base / "run", go_cache="off")
        self.assertEqual(report["schema_version"], hk.SCHEMA)
        self.assertEqual((report["go_cache"]["status"], report["record_store"]["status"], report["dryruns"]["status"],
                          report["superseded_staging"]["status"]), ("absent", "absent", "done", "absent"))
        self.assertTrue(report["disk"]["ok"])


class DriverCopyTests(unittest.TestCase):
    def test_driver_carries_an_identical_copy(self) -> None:
        self.assertEqual(DRIVER_COPY.read_bytes(), MODULE.read_bytes())


if __name__ == "__main__":
    unittest.main()
