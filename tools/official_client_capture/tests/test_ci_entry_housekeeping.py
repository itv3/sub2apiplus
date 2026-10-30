"""入口日常维护（E4-02，``tools/ci/entry_housekeeping.py``）：记录库清理（保留期、被 v2 认证与 P0 v2 证据引用的运行整份保留、
保留记录的日志与原运行清单、与发布并发时放回、中断后放回、不并发、只算不删）、发布方续期、Go 编译缓存只删久未用的条目、
空跑只留最近几次、部署留下的旧暂存树每组只留最近两份（E4-03）、根盘停线与 guard.sh 同一条。

记录库用 ``unit_records`` 的真实写入接口在临时目录里造，文件新旧用修改时间摆出来；清理时刻按参数给定，不依赖墙钟。

B5a-10：逐单元承接目录（决策收据、撤销登记、消费登记与证据快照）跟着所属运行留或删，证据快照按引用逐层追到底；Claude 的
pre-A3 v3 认证与 P0 v2 证据与 Codex 同形，同样钉住它引用的运行。
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


class UnitInheritCleanupTests(unittest.TestCase):
    """B5a-10：``unit-inherit/`` 目录纳入记录库清理。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.store_root = self.base / "unit-records"
        self.store = _Store(self.store_root)
        self.refs = self.base / "data" / "control"
        self.refs.mkdir(parents=True)
        self.inherit = self.store_root / ur.UNIT_INHERIT_DIR

    def evidence(self, payload: dict, when: float) -> dict:
        """按内容寻址写一份证据快照（与 unit_inherit.put_evidence 同形），返回 ``{path, sha256}`` 引用。"""

        data = (json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
        digest = ur.sha256_bytes(data)
        path = self.inherit / "evidence" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        _age(path, when)
        return {"path": str(path), "sha256": digest}

    def write(self, relative: str, payload: dict, when: float) -> Path:
        path = self.inherit / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        _age(path, when)
        return path

    def run_with(self, run_id: str, when: float, refs: list[dict]) -> dict[str, Path]:
        s = self.store
        record = s.record(f"capture:{run_id}", run_id, s.log(run_id, when), when)
        s.manifest(run_id, when, executed=[record])
        return {
            "decision": self.write(f"decisions/{run_id}.json", {"schema_version": ur.UNIT_INHERIT_DECISION_SCHEMA, "run_id": run_id,
                                                               "sealed_request": refs[0], "checkpoints": [{"consumer_clock": refs[1]}]}, when),
            "consumption": self.write(f"consumptions/{run_id}/pre-a3-certification-20261001t000000z-ab12cd34.json",
                                      {"run_id": run_id, "consumer_clock": refs[2], "revocation": refs[2]}, when),
            "revocation": self.write(f"revocations/{run_id}.json", {"run_id": run_id, "reason": "测试"}, when),
        }

    def test_unit_inherit_directory_follows_the_runs_it_belongs_to(self) -> None:
        nested = self.evidence({"kind": "approval"}, OLD)                 # 只被另一份证据快照引用
        sealed = self.evidence({"kind": "sealed-request", "approvals": [nested]}, OLD)
        clock = self.evidence({"kind": "clock"}, OLD)
        consumed = self.evidence({"kind": "consumer-clock"}, OLD)
        kept = self.run_with("r-claude", OLD, [sealed, clock, consumed])
        # Claude 的 pre-A3 v3 认证（从逐单元承接运行签出，清单引用另带承接证明）钉住这次旧运行。
        certification = self.refs / "policy-certification" / "claude-pre-a3-path-certification-20261001t000000z.json"
        certification.parent.mkdir(parents=True)
        certification.write_text(json.dumps({
            "schema_version": "tool-pre-a3-path-certification/v3", "client": "claude", "status": "passed", "record_store": str(self.store_root),
            "unit_manifest": {"run_id": "r-claude", "manifest_sha256": "0" * 64, "mode": "unit-inherit",
                              "unit_inherit": {"consumption": {"path": str(kept["consumption"]), "sha256": "1" * 64}}}}), encoding="utf-8")
        stale_refs = [self.evidence({"kind": f"stale-{n}"}, OLD) for n in range(3)]
        stale = self.run_with("r-stale", OLD, stale_refs)
        recent_ref = self.evidence({"kind": "recent-decision-ref"}, OLD)
        recent = self.run_with("r-recent", RECENT, [recent_ref, recent_ref, recent_ref])
        orphan = self.evidence({"kind": "orphan"}, OLD)
        fresh = self.evidence({"kind": "fresh"}, RECENT)
        temporary = self.inherit / "evidence" / ".x.json.123.ab.tmp"
        temporary.write_text("", encoding="utf-8")
        _age(temporary, NOW - 2 * DAY)

        report = hk.gc_record_store(self.store_root, reference_roots=[self.refs.parent], now=NOW)
        self.assertEqual((report["status"], report["referenced_runs"]), ("done", 1), report)
        for path in (*kept.values(), *recent.values()):
            self.assertTrue(path.exists(), f"保留下来的运行的承接登记要留：{path}")
        for ref in (sealed, nested, clock, consumed, recent_ref, fresh):
            self.assertTrue(Path(ref["path"]).exists(), f"该留的证据快照被删了：{ref}")
        for path in stale.values():
            self.assertFalse(path.exists(), f"没人引用的旧运行的承接登记要删：{path}")
        for ref in (*stale_refs, orphan):
            self.assertFalse(Path(ref["path"]).exists(), f"没人引用的旧证据快照要删：{ref}")
        self.assertFalse(temporary.exists(), "承接目录里中断留下的临时文件也清掉")
        counts = report["counts"]
        self.assertEqual({kind: counts[kind]["removed"] for kind in ("unit_inherit_decisions", "unit_inherit_revocations",
                                                                      "unit_inherit_consumptions", "unit_inherit_evidence")},
                         {"unit_inherit_decisions": 1, "unit_inherit_revocations": 1, "unit_inherit_consumptions": 1,
                          "unit_inherit_evidence": 4})
        self.assertTrue(certification.exists())

    def test_claude_p0_evidence_pins_its_run_and_trash_in_inherit_directories_is_put_back(self) -> None:
        s = self.store
        record = s.record("capture:claude-p0", "r-p0", s.log("p0", OLD), OLD)
        s.manifest("r-p0", OLD, executed=[record])
        evidence_root = self.base / "data" / "evidence" / "campaigns" / "claude-code-x" / "control" / "vc" / "receipts" / "p0"
        evidence_root.mkdir(parents=True)
        (evidence_root / "test-capture-tools.json").write_text(json.dumps({
            "schema_version": "codex-p0-offline-gate-evidence/v2", "record_store": str(self.store_root),
            "unit_manifest": {"run_id": "r-p0", "manifest_sha256": "0" * 64, "mode": "unit-inherit"},
            "tool_identity": {"client": "claude"}}), encoding="utf-8")
        ref = self.evidence({"kind": "kept-by-recent"}, RECENT)
        path = Path(ref["path"])
        trash = path.with_name(f".{path.name}{hk.TRASH_MARK}4242")
        os.rename(path, trash)
        report = hk.gc_record_store(self.store_root, reference_roots=[self.base / "data" / "control", self.base / "data" / "evidence"],
                                    now=NOW)
        self.assertTrue(s.record_file(record).exists(), "Claude 的 P0 v2 证据引用的运行留着")
        self.assertEqual(report["restored_from_trash"], 1)
        self.assertTrue(path.exists(), "承接目录里上次中断移走的文件放回并重新判定（保留期内，留着）")


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


def _stamp(days_ago: float) -> str:
    """部署名字里的 UTC 时间戳（与部署脚本的 utc_stamp().lower() 同一格式），相对 NOW 往前推 ``days_ago`` 天。"""

    return time.strftime("%Y%m%dt%H%M%Sz", time.gmtime(NOW - days_ago * DAY))


class DeployBackupReclaimTests(unittest.TestCase):
    """磁盘水位保护：housekeeping 自动回收部署回滚备份与文档事务目录（保留期可配）。

    三道闸缺一不回收：①每组最近 N 个之内；②部署时间在保留期之内；③保留期内修改过的收据／证据里仍写着名字。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.data = self.base / "data"
        for sub in ("control", "docs", "evidence"):
            (self.data / sub).mkdir(parents=True)
        self.token = 0

    def backup(self, days_ago: float, kind: str = "managed-tools") -> Path:
        self.token += 1
        path = self.data / "control" / f"{kind}-backup-before-{'ab' * 6}-{_stamp(days_ago)}-{self.token:08x}"
        path.mkdir()
        (path / "tool.py").write_bytes(b"x" * 1000)
        return path

    def transaction(self, days_ago: float, prefix: str = ".codex-01650-deploy") -> Path:
        self.token += 1
        path = self.data / "docs" / f"{prefix}-{_stamp(days_ago)}-{self.token:08x}"
        path.mkdir()
        (path / "doc.md").write_bytes(b"d" * 500)
        return path

    def prune(self, **kwargs) -> dict:
        return hk.prune_deploy_backups(self.data, now=NOW, **kwargs)

    def test_old_backups_beyond_the_newest_few_are_removed_and_the_rest_stay(self) -> None:
        # 10 个备份，1～10 天前各一个再加 5 个 30～70 天前的；保留期 14 天、每组最近 3 个。
        recent = [self.backup(day) for day in (1, 2, 3, 5, 8, 12)]
        old = [self.backup(day) for day in (20, 30, 45, 60, 70)]
        report = self.prune(retention_days=14, keep_latest=3)
        self.assertEqual(sorted(report["removed"]), sorted(path.name for path in old))
        self.assertEqual(report["removed_bytes"], 5 * 1000)
        for path in recent:
            self.assertTrue(path.is_dir(), f"保留期内的不能动：{path.name}")
        for path in old:
            self.assertFalse(path.exists())
        self.assertEqual(report["groups"]["managed-tools"], {"total": 11, "newest_kept": 3, "within_retention": 3, "candidates": 5,
                                                             "referenced": 0, "removed": 5})

    def test_newest_few_of_each_group_survive_even_when_all_are_old(self) -> None:
        tools = [self.backup(day) for day in (20, 30, 40, 50, 60)]
        preparer = [self.backup(day, "assertion-preparer") for day in (25, 35)]
        report = self.prune(retention_days=14, keep_latest=3)
        self.assertEqual(sorted(path.name for path in (self.data / "control").iterdir()),
                         sorted(path.name for path in (*tools[:3], *preparer)), "每组各自留最近 3 个，工具备份 5 个只剩 3 个，准备器 2 个全留")
        self.assertEqual(len(report["removed"]), 2)

    def test_transaction_dirs_form_one_group_across_versions(self) -> None:
        old_version = [self.transaction(day, ".codex-01640-deploy") for day in (20, 22, 24, 26)]
        current = [self.transaction(day) for day in (1, 3)]
        managed = [self.transaction(day, ".managed-deploy") for day in (30, 40)]
        report = self.prune(retention_days=14, keep_latest=3)
        survivors = {path.name for path in (self.data / "docs").iterdir()}
        # 合成一组：最近 3 个是 current 两个加 old_version 最新的一个；其余都超过 14 天，回收。
        self.assertEqual(survivors, {current[0].name, current[1].name, old_version[0].name})
        self.assertEqual(len(report["removed"]), len(old_version) - 1 + len(managed))

    def test_a_recent_file_that_still_names_the_backup_protects_it(self) -> None:
        referenced, orphan, quiet = self.backup(30), self.backup(31), self.backup(32)
        self.backup(1), self.backup(2), self.backup(3)   # 最近 3 个，占满“最近 N 个”闸
        recent_doc = self.data / "evidence" / "verdict.json"
        recent_doc.write_text(json.dumps({"rollback_backup": str(referenced)}), encoding="utf-8")
        _age(recent_doc, RECENT)
        stale_doc = self.data / "control" / "old-receipt.json"      # 只有保留期之外的旧收据提到 orphan：不算保护
        stale_doc.write_text(json.dumps({"rollback_backup": str(orphan)}), encoding="utf-8")
        _age(stale_doc, NOW - 40 * DAY)
        report = self.prune(retention_days=14, keep_latest=3)
        self.assertTrue(referenced.is_dir())
        self.assertFalse(orphan.exists())
        self.assertFalse(quiet.exists())
        self.assertEqual(report["protected_by_recent_reference"], {referenced.name: str(recent_doc)})
        self.assertEqual(report["groups"]["managed-tools"]["referenced"], 1)

    def test_dry_run_changes_nothing_and_matches_the_real_run(self) -> None:
        self.backup(1), self.backup(2), self.backup(3)
        old = [self.backup(day) for day in (30, 40)]
        txn = self.transaction(50)
        self.transaction(1), self.transaction(2), self.transaction(3)
        dry = self.prune(retention_days=14, keep_latest=3, dry_run=True)
        self.assertTrue(all(path.is_dir() for path in (*old, txn)), "只算不删")
        real = self.prune(retention_days=14, keep_latest=3)
        self.assertEqual((dry["removed"], dry["removed_bytes"]), (real["removed"], real["removed_bytes"]))
        self.assertEqual(sorted(real["removed"]), sorted(path.name for path in (*old, txn)))

    def test_unrelated_entries_symlinks_and_files_are_never_touched(self) -> None:
        keep = [self.backup(day) for day in (1, 2, 3)]
        real_old = self.backup(40)
        link = self.data / "control" / f"managed-tools-backup-before-{'cd' * 6}-{_stamp(80)}-deadbeef"
        link.symlink_to(real_old, target_is_directory=True)
        plain_file = self.data / "control" / f"managed-tools-backup-before-{'ef' * 6}-{_stamp(81)}-feedface"
        plain_file.write_bytes(b"file")
        bystander = self.data / "control" / "codex-0.165.0-timing-ledger"
        bystander.mkdir()
        wrong_shape = self.data / "control" / "managed-tools-backup-before-notes"
        wrong_shape.mkdir()
        self.prune(retention_days=14, keep_latest=3)
        self.assertFalse(real_old.exists())
        self.assertTrue(link.is_symlink() and plain_file.is_file() and bystander.is_dir() and wrong_shape.is_dir())
        self.assertTrue(all(path.is_dir() for path in keep))

    def test_trash_from_an_interrupted_run_is_cleared_and_a_failed_delete_puts_the_directory_back(self) -> None:
        for day in (1, 2, 3):
            self.backup(day)
        leftover = self.data / "control" / (hk.TRASH_MARK + self.backup(50).name)
        (self.data / "control" / leftover.name[len(hk.TRASH_MARK):]).rename(leftover)
        report = self.prune(retention_days=14, keep_latest=3)
        self.assertFalse(leftover.exists())
        self.assertEqual(report["trash_leftovers_removed"], 1)
        victim = self.backup(60)
        with unittest.mock.patch.object(hk.shutil, "rmtree", side_effect=OSError("删不动")), self.assertRaises(OSError):
            self.prune(retention_days=14, keep_latest=3)
        self.assertTrue(victim.is_dir(), "删不动就放回原名，下次重新判定")

    def test_retention_floor_and_bad_arguments(self) -> None:
        for kwargs in ({"retention_days": 6.9}, {"retention_days": 14, "keep_latest": 0}):
            with self.assertRaises(hk.HousekeepingError):
                self.prune(**kwargs)
        self.assertEqual(self.prune(retention_days=7)["status"], "absent")   # 没有任何备份：不是错误

    def test_run_reclaims_by_default_and_can_be_switched_off(self) -> None:
        for day in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            self.backup(day)
        old = self.backup(60)
        with unittest.mock.patch.object(hk.shutil, "disk_usage", return_value=types.SimpleNamespace(
                total=100 * 2 ** 30, used=50 * 2 ** 30, free=50 * 2 ** 30)):
            skipped = hk.run(data_root=self.data, runroot=self.base / "run", go_cache="off", deploy_retention_days=None)
            self.assertNotIn("deploy_backups", skipped)
            self.assertTrue(old.is_dir())
            report = hk.run(data_root=self.data, runroot=self.base / "run", go_cache="off")
        self.assertEqual(report["deploy_backups"]["removed"], [old.name])
        self.assertFalse(old.exists())


class DiskWatchTests(unittest.TestCase):
    """起跑前的磁盘预警：离停线还有余量时提前提示并给出可回收清单（不删任何东西）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.data, self.runroot = self.base / "data", self.base / "run"
        for sub in ("control", "docs", "evidence", "staging"):
            (self.data / sub).mkdir(parents=True)
        # 造出四类可回收物：旧部署备份与事务目录、旧暂存树（当前树在）、旧空跑、Go 缓存旧条目。
        self.old_backup = self.data / "control" / f"managed-tools-backup-before-{'ab' * 6}-{_stamp(60)}-00000001"
        self.old_backup.mkdir()
        (self.old_backup / "f").write_bytes(b"b" * 4096)
        for day in (1, 2, 3, 4, 5, 6, 7, 8, 9):    # 保证“最近 8 个”闸被新备份占满
            fresh = self.data / "control" / f"managed-tools-backup-before-{'cd' * 6}-{_stamp(day)}-{day:08x}"
            fresh.mkdir()
        for day in (1, 2, 3, 4, 5, 6, 7, 8, 9):    # 事务目录合成一组，同样要有最近 8 个占满，老的才轮得到回收
            (self.data / "docs" / f".codex-01650-deploy-{_stamp(day)}-{day:08x}").mkdir()
        self.old_txn = self.data / "docs" / f".codex-01650-deploy-{_stamp(70)}-0000000a"
        self.old_txn.mkdir()
        (self.old_txn / "d").write_bytes(b"t" * 2048)
        (self.data / "staging" / "codex-x-managed-tools").mkdir()
        for stamp in ("20260901t000000z", "20260902t000000z", "20260903t000000z"):
            tree = self.data / "staging" / f"codex-x-managed-tools.superseded-{stamp}"
            tree.mkdir()
            (tree / "f").write_bytes(b"s" * 1024)
        self.cache = self.base / "go-build"
        (self.cache / "ab").mkdir(parents=True)
        stale = self.cache / "ab" / "old"
        stale.write_bytes(b"g" * 8192)
        _age(stale, NOW - 10 * 3600)

    def usage(self, used_percent: float, free_gib: float) -> object:
        total = 142 * 2 ** 30
        fake = types.SimpleNamespace(total=total, used=int(total * used_percent / 100), free=int(free_gib * 2 ** 30))
        return unittest.mock.patch.object(hk.shutil, "disk_usage", return_value=fake)

    def watch(self, **kwargs) -> dict:
        return hk.disk_watch(data_root=self.data, runroot=self.runroot, go_cache=str(self.cache), **kwargs)

    def test_close_to_the_stop_line_warns_and_lists_what_can_be_reclaimed_without_deleting(self) -> None:
        """根盘 66%（停线 69%）、可用 47 GiB：还没越线，但到了预警线；清单列出四类可回收物，现场一个文件都没少。"""

        with self.usage(66.0, 47.0):
            report = self.watch()
        self.assertFalse(report["over_stop"])
        self.assertTrue(report["early_warning"])
        self.assertEqual((report["margin_percent"], report["margin_gib"]), (3.0, 7.0))
        names = [item["item"] for item in report["reclaimable"]]
        self.assertEqual(len(names), 4)
        by_name = {item["item"].split("（")[0]: item for item in report["reclaimable"]}
        self.assertEqual(by_name["Go 编译缓存旧条目"]["bytes"], 8192)
        self.assertEqual(by_name["部署留下的旧暂存树"]["count"], 1)
        self.assertEqual(by_name["部署回滚备份与文档事务目录"]["bytes"], 4096 + 2048)
        self.assertEqual(by_name["部署回滚备份与文档事务目录"]["count"], 2)
        self.assertEqual(report["reclaimable_bytes"], sum(item["bytes"] for item in report["reclaimable"]))
        self.assertTrue(self.old_backup.is_dir() and self.old_txn.is_dir() and (self.cache / "ab" / "old").is_file(), "disk-watch 只读，不删任何东西")
        text = hk.disk_watch_text(report)
        for fragment in ("已到预警线", "可回收清单", "Go 编译缓存旧条目", "部署回滚备份与文档事务目录", "housekeeping"):
            self.assertIn(fragment, text)

    def test_plenty_of_room_stays_quiet_and_skips_the_scan(self) -> None:
        with self.usage(50.0, 70.0), unittest.mock.patch.object(hk, "prune_deploy_backups", side_effect=AssertionError("余量充足不扫描")):
            report = self.watch()
        self.assertFalse(report["early_warning"])
        self.assertEqual(report["reclaimable"], [])
        self.assertEqual(len(hk.disk_watch_text(report).splitlines()), 1)
        self.assertIn("余量充足", hk.disk_watch_text(report))

    def test_each_warning_line_is_independent_and_configurable(self) -> None:
        cases = [((63.9, 60.0), {}, False),     # 距停线 5.1 个百分点、可用余量 20 GiB：都没到
                 ((64.0, 60.0), {}, True),      # 刚好停线前 5 个百分点
                 ((50.0, 45.0), {}, True),      # 可用只比 40 GiB 下限多 5 GiB
                 ((50.0, 45.1), {}, False),
                 ((64.0, 60.0), {"warn_percent": 2.0}, False),
                 ((50.0, 45.0), {"warn_gib": 2.0}, False),
                 ((68.9, 60.0), {"warn_percent": 2.0}, True)]
        for (used, free), kwargs, expected in cases:
            with self.usage(used, free):
                self.assertEqual(self.watch(**kwargs)["early_warning"], expected, (used, free, kwargs))

    def test_over_the_stop_line_is_reported_with_the_stop_reason(self) -> None:
        with self.usage(70.0, 41.0):
            report = self.watch()
        self.assertTrue(report["over_stop"] and report["early_warning"])
        self.assertIn("根盘越过停线", report["warnings"][0])

    def test_cli_exit_codes_and_json(self) -> None:
        argv = ["disk-watch", "--data-root", str(self.data), "--runroot", str(self.runroot), "--go-cache", str(self.cache)]
        for used, free, code in ((66.0, 47.0, 0), (70.0, 41.0, 5)):
            with self.usage(used, free), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(hk.main(argv), code)
            self.assertIn("可回收清单", out.getvalue())
        with self.usage(66.0, 47.0), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hk.main([*argv, "--json"]), 0)
        self.assertTrue(json.loads(out.getvalue())["early_warning"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hk.main(["disk-watch", "--data-root", "rel", "--runroot", str(self.runroot)]), 2)

    def test_dispatch_script_and_driver_copy_expose_the_new_subcommands(self) -> None:
        script = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-dryrun.sh").read_text(encoding="utf-8")
        self.assertIn('disk-watch) exec python3 -B "$DRV/entry_housekeeping.py" disk-watch', script)
        self.assertIn('go-cache-check) exec python3 -B "$DRV/entry_housekeeping.py" go-cache-check', script)


class GoCacheWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.cache = self.base / "go-build"
        (self.cache / "ab").mkdir(parents=True)
        (self.cache / "ab" / "entry").write_bytes(b"0" * (3 << 20))

    def test_over_the_threshold_warns_and_under_it_does_not(self) -> None:
        over = hk.go_cache_check(self.cache, warn_gib=1 / 1024)       # 阈值 1 MiB，缓存 3 MiB
        self.assertTrue(over["over"])
        self.assertIn("超过告警阈值", over["warning"])
        self.assertIn("go clean -cache", over["warning"])
        under = hk.go_cache_check(self.cache, warn_gib=1.0)
        self.assertFalse(under["over"])
        self.assertIsNone(under["warning"])
        self.assertEqual(under["size_bytes"], 3 << 20)
        self.assertEqual(hk.go_cache_check(None)["status"], "absent")
        self.assertEqual(hk.go_cache_check(self.base / "nope")["status"], "absent")

    def test_default_threshold_is_thirty_gib(self) -> None:
        self.assertEqual(hk.GO_CACHE_WARN_GIB, 30.0)
        self.assertFalse(hk.go_cache_check(self.cache)["over"])

    def test_housekeeping_report_carries_the_warning_without_blocking(self) -> None:
        fake = types.SimpleNamespace(total=100 * 2 ** 30, used=50 * 2 ** 30, free=50 * 2 ** 30)
        with unittest.mock.patch.object(hk.shutil, "disk_usage", return_value=fake):
            report = hk.run(data_root=self.base / "data", runroot=self.base / "run", go_cache=str(self.cache), go_cache_warn_gib=1 / 1024,
                            dry_run=True)
        self.assertTrue(report["disk"]["ok"], "只警告，不影响根盘结论")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("超过告警阈值", report["warnings"][0])
        with unittest.mock.patch.object(hk.shutil, "disk_usage", return_value=fake):
            quiet = hk.run(data_root=self.base / "data", runroot=self.base / "run", go_cache=str(self.cache), dry_run=True)
        self.assertEqual(quiet["warnings"], [])

    def test_cli_go_cache_check_always_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hk.main(["go-cache-check", "--go-cache", str(self.cache), "--warn-gib", str(1 / 1024)]), 0)
        self.assertIn("超过告警阈值", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hk.main(["go-cache-check", "--go-cache", str(self.cache)]), 0)
        self.assertIn("未超过", out.getvalue())


class DriverCopyTests(unittest.TestCase):
    def test_driver_carries_an_identical_copy(self) -> None:
        self.assertEqual(DRIVER_COPY.read_bytes(), MODULE.read_bytes())


if __name__ == "__main__":
    unittest.main()
