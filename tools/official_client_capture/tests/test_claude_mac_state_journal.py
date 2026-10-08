"""真实临时文件系统专项；不启动产品、不操作账号或用户状态。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.claude_mac_state_journal import (
    SnapshotLimits, StateConflict, StateJournal, StateJournalError, StateLockBusy, file_state, missing_state,
)


class StateJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outer = Path(temporary.name).resolve()
        self.state, self.journal = self.outer / "private", self.outer / "journal"
        self.state.mkdir(mode=0o700)
        self.identity = {"task_sha256": "a" * 64, "task_nonce": "nonce-1", "producer_run_id": "producer-1",
                         "deadline_utc": "2026-10-08T10:00:00+00:00"}
        self.config = self.state / "config.json"
        self.config.write_bytes(b"original\x00\xff")
        self.config.chmod(0o600)
        self.initial_mtime = self.config.stat().st_mtime_ns

    def recorder(self, paths=None, **kwargs) -> StateJournal:
        return StateJournal(self.identity, self.journal, self.state, paths or ["config.json"], **kwargs)

    def rows(self) -> list[dict]:
        return [json.loads(line) for line in (self.journal / "journal.jsonl").read_bytes().splitlines()]

    def change(self, recorder: StateJournal, data=b"changed", operation="edit-1") -> None:
        recorder.record_intent(operation, {"config.json": file_state(data, mode=0o640)})
        recorder.apply_intent(operation)

    def test_real_file_restores_bytes_permissions_and_mtime_with_independent_after(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            before = (self.journal / "state-before.bin").read_bytes()
            self.change(recorder)
            self.assertEqual(self.config.read_bytes(), b"changed")
            with mock.patch.object(recorder, "_snapshot", wraps=recorder._snapshot) as measured:
                result = recorder.restore()
                self.assertGreaterEqual(measured.call_count, 4)
            self.assertEqual(result["status"], "restored")
            self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
            self.assertEqual(self.config.stat().st_mtime_ns, self.initial_mtime)
            self.assertEqual((self.journal / "state-after.bin").read_bytes(), before)
            self.assertEqual((self.journal / "state-before.bin").read_bytes(), before)
            self.assertFalse(result["formal_ready"])
            self.assertFalse(result["launch_allowed"])
            self.assertEqual(result["source_scope"], "internal_state_only")
            self.assertNotEqual(result["files"]["state-before.bin"]["path"], result["files"]["state-after.bin"]["path"])

    def test_same_nonce_recovery_is_idempotent_and_does_not_refresh_deadline(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            before = (self.journal / "state-before.bin").read_bytes()
            self.change(recorder)
        with self.recorder() as recovered:
            self.assertEqual(recovered.recover()["status"], "restored")
            first_wal = (self.journal / "journal.jsonl").read_bytes()
            self.assertEqual(recovered.recover()["status"], "restored")
            self.assertEqual((self.journal / "journal.jsonl").read_bytes(), first_wal)
            self.assertEqual(recovered.identity["deadline_utc"], self.identity["deadline_utc"])
            self.assertEqual((self.journal / "state-before.bin").read_bytes(), before)

    def test_pending_intent_without_execution_restores_without_reapplying_change(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            recorder.record_intent("pending", {"config.json": file_state(b"must-not-run")})
        with self.recorder() as recovered:
            self.assertEqual(recovered.recover()["status"], "restored")
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.assertFalse(any(r["event"] == "operation_observed" for r in self.rows()))

    def test_wal_is_fsynced_before_target_rename(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            recorder.record_intent("edit", {"config.json": file_state(b"edited")})
            original_rename = os.rename
            observed = []
            def inspect_rename(*args, **kwargs):
                rows = self.rows()
                observed.append(rows[-1]["event"])
                self.assertTrue(any(r["event"] == "replacement_intent" for r in rows))
                return original_rename(*args, **kwargs)
            with mock.patch("os.rename", side_effect=inspect_rename):
                recorder.apply_intent("edit")
            self.assertTrue(observed)

    def test_interruption_between_rename_steps_recovers_same_identity(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            self.change(recorder)
            original_rename = os.rename
            def interrupt_after_move(src, dst, **kwargs):
                result = original_rename(src, dst, **kwargs)
                if dst == "trash":
                    raise SystemExit("模拟进程在真实移动之后退出")
                return result
            with mock.patch("os.rename", side_effect=interrupt_after_move):
                with self.assertRaises(SystemExit):
                    recorder.restore()
        self.assertFalse(self.config.exists())
        with self.recorder() as recovered:
            self.assertEqual(recovered.recover()["status"], "restored")
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_interruption_after_install_before_completion_marker_recovers(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            self.change(recorder)
            original_append = recorder._append
            def interrupted_append(event, payload):
                if event == "replacement_complete":
                    raise SystemExit("模拟安装完成后 marker 前退出")
                return original_append(event, payload)
            with mock.patch.object(recorder, "_append", side_effect=interrupted_append):
                with self.assertRaises(SystemExit):
                    recorder.restore()
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        with self.recorder() as recovered:
            self.assertEqual(recovered.recover()["status"], "restored")

    def test_interruption_after_before_write_does_not_replace_first_before(self) -> None:
        with self.recorder() as recorder:
            with mock.patch.object(recorder, "_append", side_effect=SystemExit("模拟 before 已 fsync 后退出")):
                with self.assertRaises(SystemExit):
                    recorder.prepare()
        original = (self.journal / "state-before.bin").read_bytes()
        with self.recorder() as recovered:
            self.assertEqual(recovered.recover()["status"], "restored")
        self.assertEqual((self.journal / "state-before.bin").read_bytes(), original)

    def test_before_without_prepared_event_and_changed_state_is_unknown(self) -> None:
        with self.recorder() as recorder:
            with mock.patch.object(recorder, "_append", side_effect=SystemExit()):
                with self.assertRaises(SystemExit):
                    recorder.prepare()
        original = (self.journal / "state-before.bin").read_bytes()
        self.config.write_bytes(b"unknown")
        with self.recorder() as recovered:
            with self.assertRaisesRegex(StateJournalError, "不能猜测"):
                recovered.recover()
        self.assertEqual(self.config.read_bytes(), b"unknown")
        self.assertEqual((self.journal / "state-before.bin").read_bytes(), original)

    def test_unknown_external_write_preserves_all_paths_and_failure(self) -> None:
        other = self.state / "other"
        other.write_bytes(b"initial")
        other.chmod(0o600)
        with self.recorder(["config.json", "other"]) as recorder:
            recorder.prepare()
            self.change(recorder)
            other.write_bytes(b"external")
            before_wal = (self.journal / "journal.jsonl").read_bytes()
            with self.assertRaises(StateConflict):
                recorder.restore()
            self.assertEqual(self.config.read_bytes(), b"changed")
            self.assertEqual(other.read_bytes(), b"external")
            self.assertTrue((self.journal / "journal.jsonl").read_bytes().startswith(before_wal))
            result = recorder.evidence()
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(len(result["failures"]), 1)
            self.assertFalse((self.journal / "state-after.bin").exists())

    def test_unknown_write_cannot_be_laundered_into_a_new_intent(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            self.config.write_bytes(b"external")
            with self.assertRaises(StateConflict):
                recorder.record_intent("new", {"config.json": file_state(b"overwrite")})
            self.assertEqual(self.config.read_bytes(), b"external")

    def test_unknown_permissions_do_not_get_overwritten(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            self.config.chmod(0o640)
            with self.assertRaises(StateConflict):
                recorder.restore()
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)

    def test_operation_confirmation_requires_actual_filesystem_result(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            recorder.record_intent("declared", {"config.json": file_state(b"never-written")})
            with self.assertRaises(StateConflict):
                recorder.confirm_intent("declared")
            self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_missing_file_remains_missing_after_real_creation_and_restore(self) -> None:
        with self.recorder(["absent"]) as recorder:
            recorder.prepare()
            recorder.record_intent("create", {"absent": file_state(b"temporary")})
            recorder.apply_intent("create")
            self.assertTrue((self.state / "absent").exists())
            self.assertEqual(recorder.restore()["status"], "restored")
        self.assertFalse((self.state / "absent").exists())

    def test_real_delete_is_reversed(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            recorder.record_intent("delete", {"config.json": missing_state()})
            recorder.apply_intent("delete")
            self.assertFalse(self.config.exists())
            recorder.restore()
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_directory_tree_restores_nested_content_type_mode_and_times(self) -> None:
        tree = self.state / "settings"
        tree.mkdir(mode=0o700)
        child = tree / "inner"
        child.mkdir(mode=0o700)
        nested = child / "state"
        nested.write_bytes(b"nested")
        nested.chmod(0o600)
        initial = {p.relative_to(tree).as_posix(): (p.stat().st_mode, p.stat().st_mtime_ns) for p in (child, nested)}
        with self.recorder(["settings"]) as recorder:
            recorder.prepare()
            recorder.record_intent("type-change", {"settings": file_state(b"directory became file")})
            recorder.apply_intent("type-change")
            self.assertTrue(tree.is_file())
            recorder.restore()
        self.assertTrue(tree.is_dir())
        self.assertEqual(nested.read_bytes(), b"nested")
        for p in (child, nested):
            self.assertEqual((p.stat().st_mode, p.stat().st_mtime_ns), initial[p.relative_to(tree).as_posix()])

    def test_controlled_leaf_symlink_restores_without_touching_target(self) -> None:
        leaf = self.state / "link"
        leaf.symlink_to("config.json")
        with self.recorder(["link"], symlink_targets={"link": "config.json"}) as recorder:
            recorder.prepare()
            recorder.record_intent("remove-link", {"link": missing_state()})
            recorder.apply_intent("remove-link")
            recorder.restore()
        self.assertTrue(leaf.is_symlink())
        self.assertEqual(leaf.readlink().as_posix(), "config.json")
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_outside_and_unlisted_symlinks_are_rejected(self) -> None:
        (self.state / "link").symlink_to("../outside")
        with self.assertRaisesRegex(StateJournalError, "越出"):
            self.recorder(["link"], symlink_targets={"link": "../outside"})
        with self.recorder(["link"]) as recorder:
            with self.assertRaises(StateJournalError):
                recorder.prepare()

    def test_parent_symlink_never_reads_or_modifies_outside_state(self) -> None:
        outside = self.outer / "outside"
        outside.mkdir(mode=0o700)
        secret = outside / "unchanged"
        secret.write_bytes(b"unchanged")
        (self.state / "parent").symlink_to(outside)
        with self.recorder(["parent/unchanged"]) as recorder:
            with self.assertRaises(OSError):
                recorder.prepare()
        self.assertEqual(secret.read_bytes(), b"unchanged")

    def test_special_file_and_hardlink_are_rejected_without_blocking(self) -> None:
        os.mkfifo(self.state / "fifo")
        with self.recorder(["fifo"]) as recorder:
            with self.assertRaisesRegex(StateJournalError, "特殊"):
                recorder.prepare()
        # 前一根的锁身份仍相同，改用独立 journal／私有根验证硬链接。
        linked_state = self.outer / "linked-private"
        linked_state.mkdir(mode=0o700)
        os.link(self.config, linked_state / "hard")
        with StateJournal(self.identity, self.outer / "linked-journal", linked_state, ["hard"]) as recorder:
            with self.assertRaisesRegex(StateJournalError, "硬链接"):
                recorder.prepare()

    def test_exclusive_lock_rejects_second_holder(self) -> None:
        with self.recorder() as first:
            first.prepare()
            with self.assertRaises(StateLockBusy):
                with self.recorder():
                    self.fail("第二持锁者不应进入")

    def test_lock_hardlink_added_while_held_rejects_change_without_overwriting_state_or_wal(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            recorder.record_intent("edit", {"config.json": file_state(b"must-not-write")})
            original_state = self.config.read_bytes()
            original_stat = self.config.stat()
            original_wal = (self.journal / "journal.jsonl").read_bytes()
            original_before = (self.journal / "state-before.bin").read_bytes()
            lock_path = self.state / ".claude-private-state.lock"
            # 真正增加持锁 inode 的链接数，不能只模拟 stat 返回值。
            alias = self.outer / "lock-alias"
            os.link(lock_path, alias)
            self.assertEqual(lock_path.stat().st_nlink, 2)
            with self.assertRaisesRegex(StateJournalError, "私有普通单链接"):
                recorder.apply_intent("edit")
            with self.assertRaisesRegex(StateJournalError, "私有普通单链接"):
                recorder.record_intent("blocked", {"config.json": file_state(b"must-not-record")})
            self.assertEqual(self.config.read_bytes(), original_state)
            current_stat = self.config.stat()
            self.assertEqual((current_stat.st_mode, current_stat.st_mtime_ns),
                             (original_stat.st_mode, original_stat.st_mtime_ns))
            self.assertEqual((self.journal / "journal.jsonl").read_bytes(), original_wal)
            self.assertEqual((self.journal / "state-before.bin").read_bytes(), original_before)
            self.assertFalse((self.journal / "state-after.bin").exists())
            self.assertEqual(alias.stat().st_ino, lock_path.stat().st_ino)

    def test_task_nonce_producer_and_deadline_mismatch_preserve_originals(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
        before = (self.journal / "state-before.bin").read_bytes()
        for key in self.identity:
            with self.subTest(key=key):
                identity = dict(self.identity)
                identity[key] = "b" * 64 if key == "task_sha256" else "changed" if key != "deadline_utc" else "2026-10-09T10:00:00Z"
                with self.assertRaises(StateConflict):
                    with StateJournal(identity, self.journal, self.state, ["config.json"]):
                        self.fail("身份不符不能持有该状态根")
        self.assertEqual((self.journal / "state-before.bin").read_bytes(), before)

    def test_changed_manifest_paths_or_limits_are_rejected(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
        for recorder in [self.recorder(["different"]), self.recorder(limits=SnapshotLimits(max_entries=10))]:
            with recorder:
                with self.assertRaisesRegex(StateJournalError, "首次冻结"):
                    recorder.prepare()

    def test_bounded_paths_nesting_content_and_entries(self) -> None:
        for paths in [["../escape"], ["/absolute"], ["settings", "settings/child"], ["x"] * 2,
                      ["/".join(["deep"] * 9)], ["."], ["a//b"]]:
            with self.subTest(paths=paths):
                with self.assertRaises(StateJournalError):
                    self.recorder(paths)
        with self.recorder(limits=SnapshotLimits(max_file_bytes=4)) as recorder:
            with self.assertRaisesRegex(StateJournalError, "单文件"):
                recorder.prepare()

    def test_before_and_wal_tampering_are_rejected_while_lock_held(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            before = self.journal / "state-before.bin"
            before.write_bytes(before.read_bytes() + b" ")
            with self.assertRaisesRegex(StateJournalError, "before"):
                recorder.restore()
        # 无论 before 是否仍为可解析 JSON，都不能按新值恢复。
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_truncated_wal_is_preserved_and_not_repaired_as_success(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
        wal = self.journal / "journal.jsonl"
        raw = wal.read_bytes() + b'{"sequence":'
        wal.write_bytes(raw)
        with self.recorder() as recorder:
            with self.assertRaisesRegex(StateJournalError, "截断"):
                recorder.recover()
        self.assertEqual(wal.read_bytes(), raw)
        self.assertFalse((self.journal / "state-after.bin").exists())

    def test_copied_before_is_not_accepted_as_after_provenance(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            after = self.journal / "state-after.bin"
            after.write_bytes((self.journal / "state-before.bin").read_bytes())
            after.chmod(0o600)
            with self.assertRaisesRegex(StateJournalError, "独立读取产物"):
                recorder.read_after()
            self.assertFalse(any(r["event"] == "after_observed" for r in self.rows()))

    def test_restore_failure_is_raised_and_first_before_and_wal_retained(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            self.change(recorder)
            original_before = (self.journal / "state-before.bin").read_bytes()
            prior_wal = (self.journal / "journal.jsonl").read_bytes()
            with mock.patch("os.rename", side_effect=PermissionError("模拟恢复权限失败")):
                with self.assertRaises(PermissionError):
                    recorder.restore()
            self.assertEqual(self.config.read_bytes(), b"changed")
            self.assertEqual((self.journal / "state-before.bin").read_bytes(), original_before)
            self.assertTrue((self.journal / "journal.jsonl").read_bytes().startswith(prior_wal))
            self.assertEqual(recorder.evidence()["status"], "failed")
            self.assertEqual(len(recorder.evidence()["failures"]), 1)
        # 权限故障解除后，同 nonce 继续真实文件恢复；失败历史仍在。
        with self.recorder() as recovered:
            result = recovered.recover()
            self.assertEqual(result["status"], "restored")
            self.assertEqual(len(result["failures"]), 1)

    def test_no_changes_after_success_and_same_operation_id_cannot_change(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            desired = file_state(b"planned")
            recorder.record_intent("one", {"config.json": desired})
            self.assertEqual(recorder.record_intent("one", {"config.json": desired}), "one")
            with self.assertRaisesRegex(StateJournalError, "冻结意图"):
                recorder.record_intent("one", {"config.json": file_state(b"different")})
            recorder.restore()
            with self.assertRaisesRegex(StateJournalError, "重新执行"):
                recorder.record_intent("later", {"config.json": file_state(b"later")})
            with self.assertRaisesRegex(StateJournalError, "重新执行"):
                recorder.apply_intent("one")
            self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_lock_is_exclusive_across_real_python_processes(self) -> None:
        script = """import os, fcntl, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(73)
sys.exit(1)
"""
        with self.recorder() as recorder:
            recorder.prepare()
            result = subprocess.run([sys.executable, "-B", "-c", script, str(self.state / ".claude-private-state.lock")],
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 73, result.stderr.decode())

    def test_active_wal_and_frozen_manifest_tampering_do_not_change_state(self) -> None:
        for name in ("journal.jsonl", "frozen-state.json"):
            with self.subTest(name=name):
                isolated = self.outer / name.replace(".", "-")
                isolated.mkdir(mode=0o700)
                private, journal = isolated / "private", isolated / "journal"
                private.mkdir(mode=0o700)
                path = private / "state"
                path.write_bytes(b"original")
                path.chmod(0o600)
                with StateJournal(self.identity, journal, private, ["state"]) as recorder:
                    recorder.prepare()
                    target = journal / name
                    if name == "journal.jsonl":
                        target.write_bytes(target.read_bytes() + b"{}\n")
                    else:
                        value = json.loads(target.read_bytes())
                        value["paths"] = ["invented"]
                        target.write_text(json.dumps(value))
                    with self.assertRaises(StateJournalError):
                        recorder.record_intent("blocked", {"state": file_state(b"must-not-write")})
                    self.assertEqual(path.read_bytes(), b"original")

    def test_independent_after_is_immutable_after_observation(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            recorder.restore()
            after = self.journal / "state-after.bin"
            corrupted = after.read_bytes() + b" "
            after.write_bytes(corrupted)
            with self.assertRaisesRegex(StateJournalError, "after 原件"):
                recorder.read_after()
            self.assertEqual(after.read_bytes(), corrupted)

    def test_overlapping_journal_root_is_rejected_before_creation(self) -> None:
        nested = self.state / "journal"
        with self.assertRaisesRegex(StateJournalError, "须分离"):
            with StateJournal(self.identity, nested, self.state, ["config.json"]):
                self.fail("重叠根不能进入")
        self.assertFalse(nested.exists())

    def test_total_bytes_entries_and_wal_have_explicit_upper_bounds(self) -> None:
        for kwargs in ({"max_total_bytes": 4}, {"max_entries": 1}):
            with self.subTest(kwargs=kwargs):
                recorder_root = self.outer / ("limits-" + next(iter(kwargs)))
                private, journal = recorder_root / "private", recorder_root / "journal"
                recorder_root.mkdir(mode=0o700)
                private.mkdir(mode=0o700)
                for name in ("one", "two"):
                    (private / name).write_bytes(b"content")
                    (private / name).chmod(0o600)
                with StateJournal(self.identity, journal, private, ["one", "two"], limits=SnapshotLimits(**kwargs)) as recorder:
                    with self.assertRaises(StateJournalError):
                        recorder.prepare()
        with self.assertRaises(StateJournalError):
            self.recorder(limits=SnapshotLimits(max_wal_bytes=129 * 1024 * 1024))

    def test_leaf_target_cannot_escape_through_another_parent_symlink(self) -> None:
        outside = self.outer / "outside-target"
        outside.mkdir(mode=0o700)
        (outside / "value").write_bytes(b"outside unchanged")
        (self.state / "alias-parent").symlink_to(outside)
        (self.state / "leaf").symlink_to("alias-parent/value")
        with self.recorder(["leaf"], symlink_targets={"leaf": "alias-parent/value"}) as recorder:
            with self.assertRaisesRegex(StateJournalError, "目标经链接"):
                recorder.prepare()
        self.assertEqual((outside / "value").read_bytes(), b"outside unchanged")

    def test_changed_state_type_is_unknown_and_not_overwritten(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            self.config.unlink()
            self.config.symlink_to("../outside")
            with self.assertRaises(StateConflict):
                recorder.restore()
            self.assertTrue(self.config.is_symlink())
            self.assertEqual(self.config.readlink().as_posix(), "../outside")
            self.assertEqual(recorder.evidence()["status"], "unknown")

    def test_after_observation_prevents_later_mutations(self) -> None:
        with self.recorder() as recorder:
            recorder.prepare()
            recorder.read_after()
            with self.assertRaisesRegex(StateJournalError, "after 观测"):
                recorder.record_intent("too-late", {"config.json": file_state(b"must-not-write")})
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")


if __name__ == "__main__":
    unittest.main()
