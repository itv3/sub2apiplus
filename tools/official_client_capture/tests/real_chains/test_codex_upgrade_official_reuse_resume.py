"""R6：真实发布、预约、注册和事件写入后的 SIGKILL 续作；合成证据、零上游请求。"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import tempfile
import traceback
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests import test_codex_upgrade as helpers


class OfficialReuseResumeTests(unittest.TestCase):
    def setUp(self):
        self.case = helpers.CodexUpgradeTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def _sealed_fixture(self, root):
        project_ledger_fixture.install_fixture_ledger(root)
        arguments = self.case._campaign_arguments(
            root / "predecessor", campaign_id="r6-source",
            baseline_version="0.151.0", target_version="0.154.0",
            model="gpt-5.5", lite_model="gpt-6-astra",
        )
        manifest = upgrade.create_campaign(arguments)
        self.case._seal_official_stage(root / "predecessor", arguments.campaign_dir, manifest)
        target = root / "successor"
        argv = [
            "reuse-official-evidence", "--predecessor-campaign-dir", str(arguments.campaign_dir),
            "--campaign-dir", str(target), "--campaign-id", "r6-successor", "--codex-account-id", "93",
        ]
        ledger = Path(manifest["control_receipts"]["upgrade_timing"]["ledger_dir"])
        return arguments.campaign_dir, target, ledger, argv

    def _run(self, argv):
        code, stdout, stderr = self.case._run_main(argv)
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def _kill_at(self, argv, target, cut):
        """只在指定真实写入完成后杀子进程，父进程使用原目录和原命令续作。"""

        error_path = target.parent / "child-error.txt"

        def child():
            try:
                with ExitStack() as stack:
                    if cut == "rename":
                        original = upgrade.os.rename

                        def rename(source, destination, *args, **kwargs):
                            result = original(source, destination, *args, **kwargs)
                            if Path(destination) == target:
                                os.kill(os.getpid(), signal.SIGKILL)
                            return result

                        stack.enter_context(mock.patch.object(upgrade.os, "rename", side_effect=rename))
                    elif cut.startswith("event-"):
                        limit = int(cut.rsplit("-", 1)[1])
                        if limit == 0:
                            stack.enter_context(mock.patch.object(
                                upgrade, "_align_official_reuse_timing",
                                side_effect=lambda *args: os.kill(os.getpid(), signal.SIGKILL),
                            ))
                        else:
                            original = timing.append_event
                            count = 0

                            def append(*args, **kwargs):
                                nonlocal count
                                result = original(*args, **kwargs)
                                if kwargs["event_id"].startswith("recovery-import-"):
                                    count += 1
                                    if count == limit:
                                        os.kill(os.getpid(), signal.SIGKILL)
                                return result

                            stack.enter_context(mock.patch.object(timing, "append_event", side_effect=append))
                    else:
                        owner, name = {
                            "registered": (project.Admission, "register"),
                            "reserved": (upgrade, "_reserve_capture_attempt"),
                            "permission": (upgrade, "_close_official_reuse_evidence_permissions"),
                            "attempt-written": (upgrade, "_write_capture_attempt"),
                            "materialized": (upgrade, "_materialize_official_attempt_import"),
                        }[cut]
                        original = getattr(owner, name)

                        def after(*args, **kwargs):
                            original(*args, **kwargs)
                            os.kill(os.getpid(), signal.SIGKILL)

                        stack.enter_context(mock.patch.object(owner, name, new=after))
                    result = self.case._run_main(argv)
                    error_path.write_text(repr(result), encoding="utf-8")
            except BaseException:
                error_path.write_text(traceback.format_exc(), encoding="utf-8")

        process = multiprocessing.get_context("fork").Process(target=child)
        process.start()
        process.join(30)
        if process.is_alive():
            process.kill()
            process.join(5)
            self.fail(f"中断点 {cut} 超时，可能存在锁重入。")
        self.assertEqual(process.exitcode, -signal.SIGKILL,
                         error_path.read_text() if error_path.exists() else cut)
        self.assertTrue(target.is_dir(), "发布后的目录不得删除")

    def _assert_aligned(self, target, ledger):
        state = timing.phase_ledger_state(ledger)
        self.assertEqual(state["status"], "active")
        self.assertIsNone(state["active_phase"])
        self.assertEqual(state["completed_phases"], ["VC-0", "VC-1"])
        events = [event for event, _ in timing._load_events(ledger)
                  if event["event_id"].startswith("recovery-import-")]
        self.assertEqual(len(events), 3)
        self.assertEqual(sum(event["live_request_count"] for event in events), 0)
        manifest = upgrade.load_campaign_manifest(target)
        plan = upgrade._vc_campaign_plan(target, manifest)
        _, pending = upgrade._timing_ledger_batch_events(
            target, manifest, plan, phase="VC-2", ledger_dir=ledger,
        )
        self.assertEqual(pending, [("stage_started", "VC-2")])
        admitted = project.assert_campaign_admitted(target, command="seal", require=True)
        self.assertEqual(admitted["campaign_id"], manifest["campaign_id"])

    def test_sealed_import_recovers_every_published_transaction_boundary(self):
        for cut in ("rename", "registered", "event-0", "event-1", "event-2", "event-3"):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as directory:
                source, target, ledger, argv = self._sealed_fixture(Path(directory).resolve())
                before = self.case._tree_digests(source)
                self._kill_at(argv, target, cut)
                published = self.case._tree_digests(target)
                result = self._run(argv)
                self.assertEqual((result["executed_job_count"], result["live_request_count"]), (0, 0))
                after = self.case._tree_digests(target)
                self.assertEqual({key: after[key] for key in published}, published)
                self.assertEqual(self.case._tree_digests(source), before)
                self._assert_aligned(target, ledger)
                events = self.case._tree_digests(ledger)
                self._run(argv)
                self.assertEqual(self.case._tree_digests(target), after)
                self.assertEqual(self.case._tree_digests(ledger), events)
                print(f"R6 已封存中断点 {cut}：同目录恢复；事件总数 3；执行 0；请求 0", flush=True)

    def test_attempt_import_recovers_same_reservation_and_materialized_bytes(self):
        for cut in ("rename", "reserved", "permission", "attempt-written", "materialized", "registered"):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as directory:
                fixture = self.case._official_attempt_import_fixture(Path(directory).resolve())
                target = fixture["campaigns"] / "upgrade-0154-official-reuse"
                argv = self.case._official_attempt_import_argv(fixture, target)
                before = self.case._tree_digests(fixture["predecessor_dir"])
                evidence_before = self.case._tree_digests(fixture["evidence_root"])
                with (
                    mock.patch.object(upgrade, "_close_official_reuse_evidence_permissions",
                                      side_effect=self.case._fake_permission_closeout),
                    mock.patch.object(upgrade, "_replay_attempt_evidence_permissions", return_value={}),
                ):
                    self._kill_at(argv, target, cut)
                    published = self.case._tree_digests(target)
                    result = self._run(argv)
                    self.assertEqual(result["status"], "official_awaiting_receipts")
                    self.assertFalse(result["official_sealed"])
                    self.assertEqual(result["live_request_count"], 0)
                    attempts = list((target / "official/attempts").iterdir())
                    self.assertEqual([path.name for path in attempts], [result["official_attempt_id"]])
                    after = self.case._tree_digests(target)
                    self.assertEqual({key: after[key] for key in published}, published)
                    self._run(argv)
                    self.assertEqual(self.case._tree_digests(target), after)
                    manifest = upgrade.load_campaign_manifest(target)
                    ledger = upgrade._campaign_timing_ledger_dir(target, manifest)
                    state = timing.phase_ledger_state(ledger)
                    self.assertEqual(state["active_phase"], "VC-0")
                    self.assertNotIn("VC-1", state["completed_phases"])
                    self.assertEqual(self.case._tree_digests(fixture["predecessor_dir"]), before)
                    self.assertEqual(self.case._tree_digests(fixture["evidence_root"]), evidence_before)
                print(f"R6 attempt 中断点 {cut}：预约总数 1；执行 0；请求 0；原证据不变", flush=True)

    def test_reentry_rejects_parameter_or_artifact_drift_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, target, ledger, argv = self._sealed_fixture(root)
            self._run(argv)
            before = self.case._tree_digests(target)
            events = self.case._tree_digests(ledger)
            other = root / "other"
            other.mkdir()
            receipt = other / "receipt.json"
            receipt.write_text("{}\n")
            for option, value in (
                ("--campaign-id", "different"), ("--codex-account-id", "94"),
                ("--recovery-timing-ledger-dir", str(other)),
                ("--recovery-arm64-environment-receipt", str(receipt)),
                ("--job-rehearsal-receipt", str(receipt)),
            ):
                changed = list(argv)
                if option in changed:
                    changed[changed.index(option) + 1] = value
                else:
                    changed.extend([option, value])
                code, _, stderr = self.case._run_main(changed)
                self.assertNotEqual(code, 0, option)
                self.assertIn("不一致", stderr)
                self.assertEqual(self.case._tree_digests(target), before)
                self.assertEqual(self.case._tree_digests(ledger), events)
            imported = target / "predecessor-import.json"
            imported.write_bytes(imported.read_bytes() + b" ")
            tampered = self.case._tree_digests(target)
            code, _, stderr = self.case._run_main(argv)
            self.assertNotEqual(code, 0)
            self.assertIn("漂移", stderr)
            self.assertEqual(self.case._tree_digests(target), tampered)
            self.assertEqual(self.case._tree_digests(ledger), events)

    def _project_head(self, root):
        """总账的事件序号、头摘要与已注册 Campaign，用于证明没有发生注册。"""

        head = project.replay_head(root / project.LEDGER_DIR_NAME)
        return head["sequence"], head["head_sha256"], sorted(head["registered_campaigns"])

    def test_deadline_pause_prevents_publish_and_event_writes(self):
        """R8 之后预算到期是可延期的 deadline_paused；导入仍在发布与任何写入之前拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, target, ledger, argv = self._sealed_fixture(root)
            before = self.case._tree_digests(ledger)
            project_before = self._project_head(root)
            deadline = timing.inspect_ledger(ledger)["total_deadline_at_utc"]
            expired = (datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                       + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            with mock.patch.object(timing, "_utc_now", return_value=expired):
                code, _, stderr = self.case._run_main(argv)
            self.assertNotEqual(code, 0)
            self.assertIn("deadline_paused", stderr)
            self.assertNotIn("stop_required", stderr)
            self.assertFalse(target.exists())
            self.assertEqual(self.case._tree_digests(ledger), before)
            self.assertEqual(self._project_head(root), project_before)

    def test_stop_required_ledger_prevents_publish_and_event_writes(self):
        """同一根因失败达到上限的 stop_required 账本仍在发布与任何写入之前拒绝导入。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, target, ledger, argv = self._sealed_fixture(root)
            for index in (1, 2):
                timing.append_event(
                    ledger, event_id=f"r6-stop-attempt-{index}-started", phase="VC-0",
                    event_type="attempt_started", attempt_id=f"r6-stop-attempt-{index}",
                    root_cause_id="r6-same-cause" if index == 2 else None,
                )
                timing.append_event(
                    ledger, event_id=f"r6-stop-attempt-{index}-failed", phase="VC-0",
                    event_type="attempt_failed", attempt_id=f"r6-stop-attempt-{index}",
                    root_cause_id="r6-same-cause",
                )
            self.assertEqual(timing.phase_ledger_state(ledger)["status"], "stop_required")
            with upgrade.codex_upgrade_supervisor._timing_closeout_lock(ledger):
                pass
            before = self.case._tree_digests(ledger)
            project_before = self._project_head(root)
            code, _, stderr = self.case._run_main(argv)
            self.assertNotEqual(code, 0)
            self.assertIn("stop_required", stderr)
            self.assertFalse(target.exists())
            self.assertEqual(self.case._tree_digests(ledger), before)
            self.assertEqual(self._project_head(root), project_before)

    def test_materialized_attempt_missing_rejects_instead_of_regenerating(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.case._official_attempt_import_fixture(Path(directory).resolve())
            target = fixture["campaigns"] / "upgrade-0154-official-reuse"
            argv = self.case._official_attempt_import_argv(fixture, target)
            with (
                mock.patch.object(upgrade, "_close_official_reuse_evidence_permissions",
                                  side_effect=self.case._fake_permission_closeout),
                mock.patch.object(upgrade, "_replay_attempt_evidence_permissions", return_value={}),
            ):
                result = self._run(argv)
                attempt_path = target / "official/attempts" / result["official_attempt_id"] / "attempt.json"
                attempt_path.unlink()
                before = self.case._tree_digests(target)
                code, _, stderr = self.case._run_main(argv)
                self.assertNotEqual(code, 0)
                self.assertIn("原 attempt 丢失", stderr)
                self.assertEqual(self.case._tree_digests(target), before)

    def test_history_without_resume_binding_remains_readable_but_cannot_be_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            _, target, ledger, argv = self._sealed_fixture(Path(directory).resolve())
            self._run(argv)
            (target / "control/official-reuse-resume.json").unlink()
            before = self.case._tree_digests(target)
            events = self.case._tree_digests(ledger)
            self.assertEqual(upgrade.campaign_status(target)["status"], "official_sealed")
            code, _, stderr = self.case._run_main(argv)
            self.assertNotEqual(code, 0)
            self.assertIn("缺少可信", stderr)
            self.assertEqual(self.case._tree_digests(target), before)
            self.assertEqual(self.case._tree_digests(ledger), events)

    def test_fixed_event_id_conflict_is_rejected_before_publish_or_registration(self):
        """固定事件编号已被不同内容占用时，在目录发布与总账注册之前拒绝，不留下永远无法续作的目录。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, target, ledger, argv = self._sealed_fixture(root)
            timing.append_event(
                ledger, event_id="recovery-import-vc0-completed", phase="VC-0",
                event_type="stage_completed", next_action="冲突内容",
            )
            # 时间账本锁的空文件不是事件；先按正式接口创建，再比较全部原字节。
            with upgrade.codex_upgrade_supervisor._timing_closeout_lock(ledger):
                pass
            before = self.case._tree_digests(ledger)
            project_before = self._project_head(root)
            for _ in range(2):
                code, _, stderr = self.case._run_main(argv)
                self.assertNotEqual(code, 0)
                self.assertIn("内容冲突", stderr)
                self.assertFalse(target.exists(), "冲突时不得发布目标目录")
                self.assertEqual(sorted(path.name for path in target.parent.iterdir()
                                        if path.name.startswith(target.name)), [])
                self.assertEqual(self.case._tree_digests(ledger), before)
                self.assertEqual(self._project_head(root), project_before)

    def test_conflict_written_after_publish_is_caught_by_locked_recheck_and_resume_writes_nothing(self):
        """预检之后、补齐事件之前被并发写入冲突事件时，锁内复检拒绝；续作在物化与注册之前即拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, target, ledger, argv = self._sealed_fixture(root)
            original = upgrade.os.rename

            def rename(source, destination, *args, **kwargs):
                result = original(source, destination, *args, **kwargs)
                if Path(destination) == target:
                    # 账本此时处于 VC-0 进行中，同编号的 VC-0 完成事件是状态机允许的并发写入。
                    timing.append_event(
                        ledger, event_id="recovery-import-vc0-completed", phase="VC-0",
                        event_type="stage_completed", next_action="并发写入的冲突内容",
                    )
                return result

            with mock.patch.object(upgrade.os, "rename", side_effect=rename):
                code, _, stderr = self.case._run_main(argv)
            self.assertNotEqual(code, 0)
            self.assertIn("内容冲突", stderr)
            self.assertTrue(target.is_dir(), "锁内复检只能在发布后拒绝，已发布目录保留供审计")
            with upgrade.codex_upgrade_supervisor._timing_closeout_lock(ledger):
                pass
            target_before = self.case._tree_digests(target)
            ledger_before = self.case._tree_digests(ledger)
            project_before = self._project_head(root)
            code, _, stderr = self.case._run_main(argv)
            self.assertNotEqual(code, 0)
            self.assertIn("内容冲突", stderr)
            self.assertEqual(self.case._tree_digests(target), target_before)
            self.assertEqual(self.case._tree_digests(ledger), ledger_before)
            self.assertEqual(self._project_head(root), project_before)


if __name__ == "__main__":
    unittest.main()
