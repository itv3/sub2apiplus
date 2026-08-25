"""升级抓包 run／恢复／seal 前置生命周期门禁测试。"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import io
import json
import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade


def _reserve_capture_attempt_in_child(
    campaign_dir: str,
    job: codex_upgrade.Job,
    manifest: dict[str, object],
    start_event: object,
    sender: object,
) -> None:
    """在独立子进程中同时竞争 Campaign 预约锁。"""

    try:
        if not start_event.wait(timeout=10):
            sender.send(("error", "等待并发起点超时"))
            return
        with mock.patch.object(
            codex_upgrade,
            "load_campaign_manifest",
            return_value=manifest,
        ):
            try:
                codex_upgrade._reserve_capture_attempt(
                    Path(campaign_dir),
                    phase="official",
                    candidate_id=None,
                    identity={"version": "0.146.0"},
                    jobs=[job],
                )
            except codex_upgrade.ConfigurationError as error:
                sender.send(("blocked", str(error)))
            else:
                sender.send(("reserved", ""))
    except BaseException as error:
        sender.send(("error", f"{type(error).__name__}: {error}"))
    finally:
        sender.close()


class CaptureLifecycleTest(unittest.TestCase):
    def _fixture(
        self, root: Path
    ) -> tuple[Path, Path, codex_upgrade.Job, dict[str, object]]:
        campaign_dir = root / "campaign"
        campaign_dir.mkdir(mode=0o700)
        campaign_path = campaign_dir / "campaign.json"
        campaign_path.write_text("{}\n", encoding="utf-8")
        campaign_path.chmod(0o600)

        job_evidence = root / "job-evidence"
        job_evidence.mkdir(mode=0o700)
        artifact = job_evidence / "capture.json"
        artifact.write_text('{"records":[]}\n', encoding="utf-8")
        artifact.chmod(0o600)
        job = codex_upgrade.Job(
            job_id="official-job",
            phase="official",
            suites=("full",),
            description="官方生命周期测试",
            steps=(
                {
                    "argv": ["true"],
                    "environment": {},
                    "timeout": 60,
                },
            ),
            evidence_roots=(str(job_evidence),),
            covers=(),
            required=True,
        )
        manifest: dict[str, object] = {
            "campaign_id": "capture-lifecycle-test",
            "campaign_mode": "formal",
            "campaign_purpose": "production_replacement",
            "official_identity": {"version": "0.146.0"},
            "configuration": {
                "service_container": "sub2apiplus",
                "keeper_container": "sub2apiplus-keeper",
                "postgres_container": "sub2apiplus-postgres",
                "redis_container": "sub2apiplus-redis",
                "capture_container": "capture-cli",
                "codex_account_id": 90,
                "api_key_id": 1,
            },
        }
        return campaign_dir, job_evidence, job, manifest

    @staticmethod
    def _arguments(campaign_dir: Path) -> argparse.Namespace:
        return argparse.Namespace(
            campaign_dir=campaign_dir,
            acknowledge_live_requests=True,
            rerun_failed=False,
            attempt_id=None,
            capture_manifest=None,
            assertion_evidence_root=None,
            restoration_report=None,
            evidence_root=[],
            approve_seal_sha256=None,
        )

    @staticmethod
    def _probe(order: list[str], fail_after: bool = False):
        def run(
            manifest: dict[str, object], output_dir: Path, phase: str
        ) -> dict[str, object]:
            del manifest
            order.append(f"probe-{phase}")
            if phase == "after" and fail_after:
                raise codex_upgrade.EnvironmentProbeError("after 探针失败")
            output_dir.mkdir(parents=True, mode=0o700)
            output_dir.chmod(0o700)
            path = output_dir / "probe-manifest.json"
            path.write_text(
                json.dumps({"phase": phase}) + "\n", encoding="utf-8"
            )
            path.chmod(0o600)
            return {"phase": phase}

        return run

    @staticmethod
    def _finalizer(order: list[str]):
        def run(
            evidence_root: Path,
            *,
            phase: str,
            candidate_id: str | None,
            **kwargs: object,
        ) -> tuple[Path, dict[str, object]]:
            del kwargs
            order.append("finalize-restoration")
            receipt_root = evidence_root / "receipts"
            receipt_root.mkdir(mode=0o700)
            path = receipt_root / "restoration-report.json"
            path.write_text(
                json.dumps(
                    {
                        "phase": phase,
                        "candidate_id": candidate_id,
                        "status": "restored",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            return path, {"status": "restored"}

        return run

    def _patch_runtime(
        self,
        manifest: dict[str, object],
        job: codex_upgrade.Job,
        order: list[str],
        *,
        run_job: object,
        fail_after: bool = False,
    ) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(codex_upgrade, "load_campaign_manifest", return_value=manifest)
        )
        stack.enter_context(mock.patch.object(codex_upgrade, "_verify_plan_identity"))
        stack.enter_context(
            mock.patch.object(
                codex_upgrade,
                "_load_stage_result",
                side_effect=codex_upgrade.ConfigurationError(
                    "阶段尚未封存：capture-official"
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(codex_upgrade, "_campaign_jobs", return_value=[job])
        )
        stack.enter_context(
            mock.patch.object(
                codex_upgrade,
                "_verify_official_binaries",
                return_value={"passed": True},
            )
        )
        stack.enter_context(
            mock.patch.object(
                codex_upgrade,
                "_probe_capture_environment",
                side_effect=self._probe(order, fail_after=fail_after),
            )
        )
        stack.enter_context(
            mock.patch.object(
                codex_upgrade,
                "_finalize_attempt_restoration",
                side_effect=self._finalizer(order),
            )
        )
        stack.enter_context(mock.patch.object(codex_upgrade, "run_job", side_effect=run_job))
        return stack

    def test_run_only_writes_awaiting_attempt_after_machine_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, job_evidence, job, manifest = self._fixture(root)
            order: list[str] = []

            def complete_job(
                current: codex_upgrade.Job,
                log_root: Path,
                attempt_index: int = 1,
                scenario_context: object | None = None,
            ) -> dict[str, object]:
                del log_root
                order.append("job")
                return {
                    "id": current.job_id,
                    "phase": current.phase,
                    "required": True,
                    "execution_sha256": codex_upgrade._job_execution_sha256(current),
                    "status": "complete",
                    "steps": [],
                    "evidence_roots": [str(job_evidence)],
                }

            with self._patch_runtime(
                manifest, job, order, run_job=complete_job
            ):
                result = codex_upgrade._run_capture_attempt(
                    self._arguments(campaign_dir), "official"
                )

            self.assertEqual(result["status"], "awaiting_receipts")
            self.assertEqual(
                order,
                ["probe-before", "job", "probe-after", "finalize-restoration"],
            )
            attempt = json.loads(
                (Path(result["attempt"]) / "attempt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(attempt["status"], "awaiting_receipts")
            self.assertIsNotNone(attempt["environment"]["restoration_report"])
            self.assertFalse((campaign_dir / "official" / "result.json").exists())

    def test_keyboard_interrupt_still_runs_after_probe_and_persists_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, _, job, manifest = self._fixture(root)
            order: list[str] = []

            def interrupt_job(
                current: codex_upgrade.Job,
                log_root: Path,
                attempt_index: int = 1,
                scenario_context: object | None = None,
            ) -> dict[str, object]:
                del current, log_root
                order.append("job")
                raise KeyboardInterrupt

            with self._patch_runtime(
                manifest, job, order, run_job=interrupt_job
            ):
                with self.assertRaises(KeyboardInterrupt):
                    codex_upgrade._run_capture_attempt(
                        self._arguments(campaign_dir), "official"
                    )

            self.assertEqual(
                order,
                ["probe-before", "job", "probe-after", "finalize-restoration"],
            )
            attempts = sorted((campaign_dir / "official" / "attempts").iterdir())
            attempt = json.loads(
                (attempts[-1] / "attempt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["status"], "failed")
            self.assertEqual(attempt["execution_error"]["type"], "KeyboardInterrupt")

    def test_after_probe_failure_marks_campaign_contaminated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, job_evidence, job, manifest = self._fixture(root)
            order: list[str] = []

            def complete_job(
                current: codex_upgrade.Job,
                log_root: Path,
                attempt_index: int = 1,
                scenario_context: object | None = None,
            ) -> dict[str, object]:
                del log_root
                order.append("job")
                return {
                    "id": current.job_id,
                    "phase": current.phase,
                    "required": True,
                    "execution_sha256": codex_upgrade._job_execution_sha256(current),
                    "status": "complete",
                    "steps": [],
                    "evidence_roots": [str(job_evidence)],
                }

            with self._patch_runtime(
                manifest,
                job,
                order,
                run_job=complete_job,
                fail_after=True,
            ):
                with self.assertRaises(RuntimeError):
                    codex_upgrade._run_capture_attempt(
                        self._arguments(campaign_dir), "official"
                    )

            self.assertEqual(order, ["probe-before", "job", "probe-after"])
            marker = json.loads(
                (campaign_dir / "environment-contaminated.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marker["phase"], "official")
            attempts = sorted((campaign_dir / "official" / "attempts").iterdir())
            attempt = json.loads(
                (attempts[-1] / "attempt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["status"], "environment_contaminated")

    def test_seal_preview_requires_exact_machine_fact_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir = Path(temporary) / "campaign"
            campaign_dir.mkdir(mode=0o700)
            attempt_root = campaign_dir / "official" / "attempts" / "attempt-a"
            attempt_root.mkdir(parents=True, mode=0o700)
            attempt = {
                "campaign_id": "capture-lifecycle-test",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "candidate_purpose": None,
                "attempt_id": "attempt-a",
                "attempt_digest": "1" * 64,
            }
            stage_payload = {
                "evidence_inventory": {"digest": "2" * 64},
                "assertion_context": {
                    "capture_manifest": {"sha256": "3" * 64}
                },
                "restoration": {"report": {"sha256": "4" * 64}},
            }

            preview, approved = codex_upgrade._seal_preview(
                campaign_dir,
                attempt_root,
                phase="official",
                candidate_id=None,
                attempt=attempt,
                stage_payload=stage_payload,
                approve_sha256=None,
            )
            self.assertFalse(approved)
            self.assertEqual(preview["status"], "approval_required")

            replayed, approved = codex_upgrade._seal_preview(
                campaign_dir,
                attempt_root,
                phase="official",
                candidate_id=None,
                attempt=attempt,
                stage_payload=stage_payload,
                approve_sha256=preview["review_sha256"],
            )
            self.assertTrue(approved)
            self.assertEqual(replayed, preview)

            changed = json.loads(json.dumps(stage_payload))
            changed["restoration"]["report"]["sha256"] = "5" * 64
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade._seal_preview(
                    campaign_dir,
                    attempt_root,
                    phase="official",
                    candidate_id=None,
                    attempt=attempt,
                    stage_payload=changed,
                    approve_sha256=preview["review_sha256"],
                )

    def test_run_rejects_seal_only_receipt_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir, _, _, manifest = self._fixture(Path(temporary))
            arguments = self._arguments(campaign_dir)
            arguments.capture_manifest = Path("capture-manifest.json")
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=manifest,
                ),
                self.assertRaises(codex_upgrade.ConfigurationError) as caught,
            ):
                codex_upgrade._run_capture_attempt(arguments, "official")
            self.assertIn("run 不读取 seal 收据参数", str(caught.exception))

    def test_all_only_starts_candidate_run_without_removed_seal_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir = Path(temporary) / "campaign"
            arguments = [
                "all",
                "--campaign-dir",
                str(campaign_dir),
                "--candidate-id",
                "candidate-a",
                "--runtime-image",
                f"sub2apiplus@sha256:{'1' * 64}",
                "--build-id",
                "build-a",
                "--deployed-version",
                "release-a",
                "--profile-id",
                "codex-0.146.0",
                "--profile-digest",
                "2" * 64,
                "--candidate-purpose",
                "production_replacement",
            ]
            output = io.StringIO()
            with (
                mock.patch.object(
                    codex_upgrade,
                    "campaign_status",
                    return_value={"status": "profile_approved"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_run_capture_attempt",
                    return_value={"status": "awaiting_receipts"},
                ) as run_capture,
                contextlib.redirect_stdout(output),
            ):
                return_code = codex_upgrade.main(arguments)

            self.assertEqual(return_code, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "awaiting_receipts")
            run_capture.assert_called_once()

    def test_all_rejects_removed_assertions_argument(self) -> None:
        parser = codex_upgrade._build_parser()
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            parser.parse_args(
                [
                    "all",
                    "--campaign-dir",
                    "/tmp/campaign",
                    "--candidate-id",
                    "candidate-a",
                    "--runtime-image",
                    f"sub2apiplus@sha256:{'1' * 64}",
                    "--build-id",
                    "build-a",
                    "--deployed-version",
                    "release-a",
                    "--profile-id",
                    "codex-0.146.0",
                    "--profile-digest",
                    "2" * 64,
                    "--candidate-purpose",
                    "production_replacement",
                    "--assertions",
                    "/tmp/results.json",
                ]
            )

    def test_resume_failed_attempt_really_starts_rerun(self) -> None:
        arguments = argparse.Namespace(
            campaign_dir=Path("/tmp/campaign"),
            candidate_id=None,
            rerun_failed=True,
        )
        with (
            mock.patch.object(
                codex_upgrade,
                "_require_formal_campaign",
                return_value={
                    "campaign_mode": "formal",
                    "campaign_purpose": "production_replacement",
                },
            ),
            mock.patch.object(
                codex_upgrade,
                "campaign_status",
                return_value={"status": "official_capture_failed"},
            ),
            mock.patch.object(
                codex_upgrade,
                "_run_capture_attempt",
                return_value={"status": "awaiting_receipts"},
            ) as rerun,
        ):
            result, return_code = codex_upgrade._resume_campaign(arguments)

        self.assertEqual(return_code, 2)
        self.assertEqual(result["status"], "awaiting_receipts")
        rerun.assert_called_once_with(arguments, "official")

    def test_failed_attempt_results_cannot_cross_identity_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, _, job, manifest = self._fixture(root)
            identity = {"version": "0.146.0", "binary": "a"}
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value=manifest,
            ):
                attempt_root, _ = codex_upgrade._reserve_capture_attempt(
                    campaign_dir,
                    phase="official",
                    candidate_id=None,
                    identity=identity,
                    jobs=[job],
                )
                codex_upgrade._write_capture_attempt(
                    campaign_dir,
                    attempt_root,
                    {
                        "campaign_id": manifest["campaign_id"],
                        "phase": "official",
                        "candidate_id": None,
                        "status": "failed",
                        "identity": identity,
                        "results": [
                            {
                                "id": job.job_id,
                                "status": "complete",
                                "execution_sha256": codex_upgrade._job_execution_sha256(job),
                            }
                        ],
                    },
                )
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    codex_upgrade._prior_complete_results(
                        campaign_dir,
                        Path("official"),
                        [job],
                        phase="official",
                        candidate_id=None,
                        identity={"version": "0.146.0", "binary": "b"},
                    )

    def test_parallel_awaiting_attempt_blocks_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, _, job, manifest = self._fixture(root)
            order: list[str] = []
            with (
                self._patch_runtime(
                    manifest,
                    job,
                    order,
                    run_job=mock.Mock(),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_active_unsealed_attempts",
                    return_value=["official:attempt-a"],
                ),
            ):
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    codex_upgrade._run_capture_attempt(
                        self._arguments(campaign_dir), "official"
                    )
            self.assertEqual(order, [])

    def test_campaign_lock_allows_only_one_atomic_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, _, job, manifest = self._fixture(root)
            identity = {"version": "0.146.0"}
            barrier = threading.Barrier(2)

            def reserve() -> str:
                barrier.wait(timeout=5)
                try:
                    codex_upgrade._reserve_capture_attempt(
                        campaign_dir,
                        phase="official",
                        candidate_id=None,
                        identity=identity,
                        jobs=[job],
                    )
                except codex_upgrade.ConfigurationError:
                    return "blocked"
                return "reserved"

            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value=manifest,
            ):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: reserve(), range(2)))

            self.assertEqual(sorted(results), ["blocked", "reserved"])
            attempts = [
                path
                for path in (campaign_dir / "official" / "attempts").iterdir()
                if path.name[0] != "."
            ]
            self.assertEqual(len(attempts), 1)
            self.assertTrue((attempts[0] / "reservation.json").is_file())

    def test_campaign_lock_allows_only_one_atomic_reservation_across_processes(
        self,
    ) -> None:
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("该回归测试要求 macOS／Linux 的 POSIX fork。")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, _, job, manifest = self._fixture(root)
            context = multiprocessing.get_context("fork")
            start_event = context.Event()
            processes: list[multiprocessing.Process] = []
            receivers: list[object] = []
            senders: list[object] = []
            try:
                for _ in range(2):
                    receiver, sender = context.Pipe(duplex=False)
                    process = context.Process(
                        target=_reserve_capture_attempt_in_child,
                        args=(
                            str(campaign_dir),
                            job,
                            manifest,
                            start_event,
                            sender,
                        ),
                    )
                    process.start()
                    sender.close()
                    processes.append(process)
                    receivers.append(receiver)
                    senders.append(sender)

                start_event.set()
                for process in processes:
                    process.join(timeout=15)
                hanging = [process for process in processes if process.is_alive()]
                self.assertEqual(hanging, [], "预约竞争子进程未按期退出。")
                self.assertTrue(
                    all(process.exitcode == 0 for process in processes),
                    [process.exitcode for process in processes],
                )

                messages: list[tuple[str, str]] = []
                for receiver in receivers:
                    self.assertTrue(receiver.poll(2), "子进程没有返回预约结果。")
                    messages.append(receiver.recv())
                self.assertEqual(
                    sorted(status for status, _ in messages),
                    ["blocked", "reserved"],
                    messages,
                )
                blocked_message = next(
                    detail
                    for status, detail in messages
                    if status == "blocked"
                )
                self.assertIn("未封存预约或 attempt", blocked_message)
            finally:
                start_event.set()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                for receiver in receivers:
                    receiver.close()
                for sender in senders:
                    sender.close()

            attempts = [
                path
                for path in (campaign_dir / "official" / "attempts").iterdir()
                if not path.name.startswith(".")
            ]
            self.assertEqual(len(attempts), 1)
            self.assertTrue((attempts[0] / "reservation.json").is_file())

    def test_stage_seal_rejects_evidence_drift_inside_campaign_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            campaign_path = campaign_dir / "campaign.json"
            campaign_path.write_text("{}\n", encoding="utf-8")
            campaign_path.chmod(0o600)

            evidence_root = root / "evidence"
            evidence_root.mkdir(mode=0o700)
            artifact = evidence_root / "capture.log"
            artifact.write_text("审批时内容\n", encoding="utf-8")
            artifact.chmod(0o600)
            payload = {
                "status": "complete",
                "evidence_roots": [str(evidence_root)],
                "evidence_inventory": codex_upgrade._evidence_inventory(
                    [evidence_root]
                ),
                "security": {
                    "raw_evidence_private": True,
                    **codex_upgrade._evidence_security([evidence_root]),
                },
            }
            original_campaign_lock = codex_upgrade._campaign_lock

            @contextlib.contextmanager
            def mutate_when_locking(current_campaign_dir: Path):
                with original_campaign_lock(current_campaign_dir):
                    artifact.write_text("封存锁内发生漂移\n", encoding="utf-8")
                    artifact.chmod(0o600)
                    yield

            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value={
                        "campaign_id": "seal-drift-test",
                        "campaign_mode": "formal",
                        "campaign_purpose": "production_replacement",
                    },
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_lock",
                    side_effect=mutate_when_locking,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_reject_contaminated_campaign",
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_stage_contract",
                ),
            ):
                with self.assertRaises(
                    codex_upgrade.ConfigurationError
                ) as caught:
                    codex_upgrade.save_stage_result(
                        campaign_dir,
                        "capture-official",
                        payload,
                    )

            self.assertIn("证据在封存审批后发生变化", str(caught.exception))
            self.assertFalse(
                (campaign_dir / "official" / "result.json").exists()
            )

    def test_latest_attempt_uses_reservation_time_before_random_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir = Path(temporary) / "campaign"
            attempts_root = campaign_dir / "official" / "attempts"
            attempts_root.mkdir(parents=True, mode=0o700)
            older_id = "20260731T000000Z-ffffffffffffffff"
            newer_id = "20260731T000000Z-0000000000000000"
            for attempt_id in (older_id, newer_id):
                attempt_root = attempts_root / attempt_id
                attempt_root.mkdir(mode=0o700)
                attempt_path = attempt_root / "attempt.json"
                attempt_path.write_text("{}\n", encoding="utf-8")
                attempt_path.chmod(0o600)

            reservations = {
                older_id: {
                    "run_nonce": "1" * 64,
                    "started_at_utc": "2026-07-31T00:00:00.100000Z",
                },
                newer_id: {
                    "run_nonce": "2" * 64,
                    "started_at_utc": "2026-07-31T00:00:00.200000Z",
                },
            }

            def load_reservation(
                _campaign_dir: Path,
                attempt_root: Path,
                *,
                phase: str,
                candidate_id: str | None,
            ) -> dict[str, str]:
                self.assertEqual(phase, "official")
                self.assertIsNone(candidate_id)
                return reservations[attempt_root.name]

            def load_attempt(
                _campaign_dir: Path,
                phase: str,
                candidate_id: str | None,
                attempt_id: str,
            ) -> tuple[Path, dict[str, object]]:
                self.assertEqual(phase, "official")
                self.assertIsNone(candidate_id)
                reservation = reservations[attempt_id]
                return attempts_root / attempt_id, {
                    "status": "awaiting_receipts" if attempt_id == newer_id else "failed",
                    "run_nonce": reservation["run_nonce"],
                    "started_at_utc": reservation["started_at_utc"],
                    "environment": {},
                }

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    side_effect=load_reservation,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    side_effect=load_attempt,
                ),
            ):
                summary = codex_upgrade._latest_attempt_summary(
                    campaign_dir,
                    "official",
                    None,
                )

            self.assertIsNotNone(summary)
            self.assertEqual(summary["attempt_id"], newer_id)
            self.assertEqual(summary["status"], "awaiting_receipts")

    def test_interrupted_reservation_is_fail_closed_without_empty_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, _, job, manifest = self._fixture(root)
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value=manifest,
            ):
                attempt_root, _ = codex_upgrade._reserve_capture_attempt(
                    campaign_dir,
                    phase="official",
                    candidate_id=None,
                    identity={"version": "0.146.0"},
                    jobs=[job],
                )
                self.assertTrue((attempt_root / "reservation.json").is_file())
                self.assertFalse((attempt_root / "attempt.json").exists())
                active = codex_upgrade._active_unsealed_attempts(
                    campaign_dir, "official"
                )
                self.assertIn("reserved_or_interrupted", active[0])
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    codex_upgrade._reserve_capture_attempt(
                        campaign_dir,
                        phase="official",
                        candidate_id=None,
                        identity={"version": "0.146.0"},
                        jobs=[job],
                    )

    def test_contaminated_attempt_blocks_even_when_marker_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir, job_evidence, job, manifest = self._fixture(root)
            order: list[str] = []

            def complete_job(
                current: codex_upgrade.Job,
                log_root: Path,
                attempt_index: int = 1,
                scenario_context: object | None = None,
            ) -> dict[str, object]:
                del log_root
                order.append("job")
                return {
                    "id": current.job_id,
                    "phase": current.phase,
                    "required": True,
                    "execution_sha256": codex_upgrade._job_execution_sha256(current),
                    "status": "complete",
                    "steps": [],
                    "evidence_roots": [str(job_evidence)],
                }

            original_write = codex_upgrade._secure_write_json_once

            def fail_marker(path: Path, payload: dict[str, object]) -> None:
                if path == campaign_dir / "environment-contaminated.json":
                    raise codex_upgrade.ConfigurationError("模拟 marker 写失败")
                original_write(path, payload)

            with (
                self._patch_runtime(
                    manifest,
                    job,
                    order,
                    run_job=complete_job,
                    fail_after=True,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_secure_write_json_once",
                    side_effect=fail_marker,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    codex_upgrade._run_capture_attempt(
                        self._arguments(campaign_dir), "official"
                    )

            self.assertFalse(
                (campaign_dir / "environment-contaminated.json").exists()
            )
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value=manifest,
            ):
                records = codex_upgrade._campaign_contamination_records(campaign_dir)
                self.assertTrue(any(value.endswith(":attempt") for value in records))
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    codex_upgrade._reject_contaminated_campaign(campaign_dir)

    def test_contamination_marker_blocks_explicit_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir = Path(temporary) / "campaign"
            campaign_dir.mkdir(mode=0o700)
            marker = campaign_dir / "environment-contaminated.json"
            marker.write_text("{}\n", encoding="utf-8")
            marker.chmod(0o600)
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade._run_capture_attempt(
                    self._arguments(campaign_dir), "official"
                )

    def test_first_candidate_seal_returns_nonce_bound_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            campaign_path = campaign_dir / "campaign.json"
            campaign_path.write_text("{}\n", encoding="utf-8")
            campaign_path.chmod(0o600)
            attempt_root = (
                campaign_dir
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "20260731T000000Z-1111111111111111"
            )
            evidence_root = attempt_root / "evidence"
            evidence_root.mkdir(parents=True, mode=0o700)
            restoration_path = evidence_root / "restoration.json"
            restoration_path.write_text("{}\n", encoding="utf-8")
            restoration_path.chmod(0o600)
            identity = {
                "profile_id": "profile-a",
                "profile_digest": "1" * 64,
                "image_reference": f"sub2api@sha256:{'2' * 64}",
                "image_id": f"sha256:{'3' * 64}",
                "source_tree_sha256": "4" * 64,
                "build_id": "build-a",
                "deployed_version": "release-a",
                "source_root": str(root),
                "candidate_purpose": "production_replacement",
            }
            attempt = {
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "candidate_purpose": "production_replacement",
                "campaign_manifest_sha256": "5" * 64,
                "attempt_id": attempt_root.name,
                "attempt_digest": "6" * 64,
                "phase": "candidate",
                "candidate_id": "candidate-a",
                "status": "awaiting_receipts",
                "run_nonce": "7" * 64,
                "started_at_utc": "2026-07-31T00:00:00Z",
                "completed_at_utc": "2026-07-31T00:01:00Z",
                "identity": identity,
                "results": [],
                "evidence_roots": [str(evidence_root)],
                "environment": {
                    "evidence_root": str(evidence_root),
                    "restoration_report": {
                        "path": restoration_path.name,
                        "sha256": codex_upgrade.file_sha256(restoration_path),
                        "bytes": restoration_path.stat().st_size,
                    },
                },
            }
            arguments = argparse.Namespace(
                campaign_dir=campaign_dir,
                candidate_id="candidate-a",
                attempt_id=attempt_root.name,
                runtime_image=None,
                candidate_image_id=None,
                candidate_source=None,
                build_id=None,
                deployed_version=None,
                profile_id=None,
                profile_digest=None,
                candidate_purpose="production_replacement",
                evidence_root=[],
                restoration_report=None,
                capture_manifest=None,
                assertion_evidence_root=None,
                observed_profile_receipt=None,
                client_evidence=[],
                approve_seal_sha256=None,
            )
            manifest = {
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "target_version": "0.146.0",
                "configuration": {"model": "gpt-5.6-luna"},
            }
            with (
                mock.patch.object(
                    codex_upgrade, "load_campaign_manifest", return_value=manifest
                ),
                mock.patch.object(codex_upgrade, "_verify_plan_identity"),
                mock.patch.object(codex_upgrade, "_reject_contaminated_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_stage_result",
                    return_value={"status": "complete"},
                ),
                mock.patch.object(codex_upgrade, "_verify_candidate_attempt_identity"),
                mock.patch.object(
                    codex_upgrade,
                    "_profile_binding_from_manifest",
                    return_value=("profile-a", "1" * 64),
                ),
                mock.patch.object(codex_upgrade, "_campaign_jobs", return_value=[]),
                mock.patch.object(codex_upgrade, "_validate_capture_job_results"),
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_lock",
                    return_value=contextlib.nullcontext(),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_candidate_post_client_restoration",
                    return_value=(
                        evidence_root / "client-restoration.json",
                        {},
                        "2026-07-31T00:02:00Z",
                        True,
                    ),
                ),
            ):
                result = codex_upgrade._seal_capture_attempt(arguments, "candidate")

            self.assertEqual(result["status"], "client_checkpoint_created")
            self.assertEqual(result["run_nonce"], attempt["run_nonce"])
            self.assertEqual(
                result["client_checkpoint_at_utc"], "2026-07-31T00:02:00Z"
            )

    def test_container_repo_digest_and_image_id_are_verified_separately(self) -> None:
        image_id = f"sha256:{'1' * 64}"
        image_reference = f"registry/sub2api@sha256:{'2' * 64}"
        with (
            mock.patch.object(
                codex_upgrade,
                "_container_image_id",
                return_value=image_id,
            ),
            mock.patch.object(
                codex_upgrade,
                "_image_repo_digests",
                return_value={image_reference},
            ),
        ):
            self.assertEqual(
                codex_upgrade._verify_container_image_reference(
                    "sub2apiplus",
                    image_reference,
                    image_id,
                ),
                image_id,
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade._verify_container_image_reference(
                    "sub2apiplus",
                    f"registry/sub2api@sha256:{'3' * 64}",
                    image_id,
                )


if __name__ == "__main__":
    unittest.main()
