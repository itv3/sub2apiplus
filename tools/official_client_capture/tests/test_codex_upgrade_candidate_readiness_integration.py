#!/usr/bin/env python3
"""Candidate readiness 与抓包预约主流程的最小集成合同。"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import json
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_candidate_readiness as readiness
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
IMAGE_ID = "sha256:" + "d" * 64


class _Deadline:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def check(self, operation: str) -> None:
        self.events.append(f"deadline:{operation}")


class _Admission:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.ledger_events: list[dict[str, object]] = []

    @property
    def head_sequence(self) -> int:
        return len(self.ledger_events)

    @property
    def head_sha256(self) -> str:
        if not self.ledger_events:
            return SHA_A
        return str(self.ledger_events[-1]["event_sha256"])

    @property
    def remaining_live_requests(self) -> int:
        return 100 - len(self.ledger_events)

    def account_candidate_probe(self, **kwargs: object) -> dict[str, object]:
        if kwargs.get("expected_head_sha256") != self.head_sha256:
            raise AssertionError("probe 入账使用了陈旧 head")
        dispatch_id = str(kwargs["dispatch_id"])
        operation_id = (
            "candidate-probe:"
            + hashlib.sha256(dispatch_id.encode("utf-8")).hexdigest()[:32]
        )
        for event in self.ledger_events:
            if event["operation_id"] == operation_id:
                return {
                    "operation_id": operation_id,
                    "status": "duplicate",
                    "head_sequence": self.head_sequence,
                    "head_sha256": self.head_sha256,
                    "remaining_live_requests": self.remaining_live_requests,
                }
        payload = {
            "campaign_id": kwargs["campaign_id"],
            "candidate_id": kwargs["candidate_id"],
            "dispatch_id": dispatch_id,
            "accounting_category": readiness.ACCOUNTING_CATEGORY,
            "response_status": kwargs["response_status"],
            "receipt_sha256": kwargs["receipt_sha256"],
            "request": {
                "status": "resolved",
                "identity_keys": [kwargs["identity_key"]],
                "estimated_delta": 0,
            },
        }
        event: dict[str, object] = {
            "schema_version": "upgrade-project-ledger-event/v1",
            "sequence": self.head_sequence + 1,
            "operation_id": operation_id,
            "recorded_at_utc": "2026-09-17T00:00:05Z",
            "event_type": "candidate_probe_accounted",
            "payload": payload,
            "payload_sha256": readiness._project_ledger_digest(payload),
            "source_batch_sha256": None,
            "previous_event_sha256": (
                self.ledger_events[-1]["event_sha256"]
                if self.ledger_events
                else None
            ),
        }
        event["event_sha256"] = readiness._project_ledger_digest(event)
        self.ledger_events.append(event)
        return {
            "operation_id": operation_id,
            "status": "appended",
            "head_sequence": self.head_sequence,
            "head_sha256": self.head_sha256,
            "remaining_live_requests": self.remaining_live_requests,
        }

    def reservation_cas(
        self,
        *,
        expected_sequence: int,
        expected_head_sha256: str,
    ) -> dict[str, object]:
        self.events.append("project-cas")
        if (expected_sequence, expected_head_sha256) != (
            self.head_sequence,
            self.head_sha256,
        ):
            raise AssertionError("reservation CAS 未使用 probe 入账后的 head")
        return {"sequence": self.head_sequence, "head_sha256": self.head_sha256}


class _ProbeRuntime:
    def __init__(self) -> None:
        self.restart_count = 0

    def runner(
        self, arguments: list[str] | tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        values = list(arguments)
        if values[:2] == ["docker", "inspect"] and values[-1] == "postgres":
            output = json.dumps(
                [{"Config": {"Env": ["POSTGRES_USER=u", "POSTGRES_DB=d"]}}]
            )
        elif values[:3] == ["docker", "exec", "postgres"]:
            output = "secret-api-key\n"
        elif values == ["docker", "port", "service", "8080/tcp"]:
            output = "127.0.0.1:49152\n"
        else:
            raise AssertionError(f"未预期命令：{values}")
        return subprocess.CompletedProcess(values, 0, output, "")

    def restart(self, container: str) -> dict[str, object]:
        if container != "service":
            raise AssertionError("服务容器不一致")
        self.restart_count += 1
        return {
            "container_id": "container-1",
            "image_id": IMAGE_ID,
            "started_at_utc": f"2026-09-17T00:00:0{self.restart_count}Z",
            "health": "healthy",
        }


def _static_checks() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for check_id, failure_code in readiness.STATIC_CHECK_FAILURE_CODES.items():
        identity: dict[str, object] = {"check_id": check_id}
        if check_id == "candidate-readiness.image":
            identity.update(
                {
                    "candidate_id": "candidate-a",
                    "image_id": IMAGE_ID,
                    "build_receipt_sha256": SHA_C,
                }
            )
        rows.append(
            {
                "check_id": check_id,
                "failure_code": failure_code,
                "status": "passed",
                "error_type": None,
                "evidence": {"identity": identity},
            }
        )
    return rows


class CandidateReadinessIntegrationTests(unittest.TestCase):
    def _probe_bundle_fixture(self, root: Path) -> dict[str, object]:
        campaign = root / "campaign"
        _static_path, static_receipt = readiness.write_static_receipt(
            campaign
            / "control"
            / "candidate-readiness"
            / "candidate-a"
            / "static-receipts",
            campaign_id="campaign-a",
            candidate_id="candidate-a",
            image_id=IMAGE_ID,
            build_receipt_sha256=SHA_C,
            checks=_static_checks(),
            observed_at_utc="2026-09-17T00:00:00.000Z",
        )
        events: list[str] = []
        admission = _Admission(events)
        runtime = _ProbeRuntime()
        configuration = {
            "service_container": "service",
            "postgres_container": "postgres",
            "codex_account_id": 22,
            "api_key_id": 9,
        }
        receipt = readiness.ensure_models_probe(
            campaign,
            campaign_id="campaign-a",
            candidate_id="candidate-a",
            image_id=IMAGE_ID,
            build_receipt_sha256=SHA_C,
            static_receipt_digest=str(static_receipt["receipt_digest"]),
            target_version="0.154.0",
            configuration=configuration,
            admission=admission,
            runner=runtime.runner,
            dispatch=lambda *_args: (
                200,
                json.dumps({"models": [{"slug": "gpt-test"}]}).encode("utf-8"),
            ),
            restart=runtime.restart,
            now=datetime(2026, 9, 17, 0, 0, 10, tzinfo=timezone.utc),
        )
        session_root = (
            campaign
            / "control"
            / "candidate-readiness"
            / "candidate-a"
            / "models-probes"
            / f"session-{receipt['session_id']}"
        )
        history = {
            "plan_sha256": SHA_A,
            "events": admission.ledger_events,
            "head_sequence": admission.head_sequence,
            "head_sha256": admission.head_sha256,
        }
        return {
            "campaign": campaign,
            "session_root": session_root,
            "receipt": receipt,
            "history": history,
            "head_sequence": admission.head_sequence,
            "head_sha256": admission.head_sha256,
        }

    def _replay_probe_fixture(self, fixture: dict[str, object]) -> dict[str, object]:
        session_root = fixture["session_root"]
        history = fixture["history"]
        if not isinstance(session_root, Path) or not isinstance(history, dict):
            raise AssertionError("probe fixture 类型非法")
        return readiness.replay_models_probe_bundle(
            session_root,
            service_container="service",
            image_id=IMAGE_ID,
            project_ledger_history=history,
            reservation_head_sequence=int(fixture["head_sequence"]),
            reservation_head_sha256=str(fixture["head_sha256"]),
            current_time=datetime(2026, 9, 17, 0, 0, 11, tzinfo=timezone.utc),
        )

    def test_probe_bundle_replay_rejects_deleted_dispatch_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._probe_bundle_fixture(Path(directory))
            session_root = fixture["session_root"]
            assert isinstance(session_root, Path)
            before = {
                path.name: (
                    path.stat().st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in session_root.iterdir()
            }
            replayed = self._replay_probe_fixture(fixture)
            self.assertEqual(replayed["receipt"], fixture["receipt"])
            after = {
                path.name: (
                    path.stat().st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in session_root.iterdir()
            }
            self.assertEqual(after, before)
            self.assertEqual(
                replayed["cache_epoch_after"],
                fixture["receipt"]["cache_isolation"]["cache_epoch_after"],
            )
            (session_root / "dispatch-01.result.json").unlink()
            with self.assertRaisesRegex(ValueError, "dispatch 文件闭集"):
                self._replay_probe_fixture(fixture)

    def test_probe_bundle_replays_real_project_ledger_event_digests(self) -> None:
        """read-only snapshot 必须兼容项目总账真实的 canonical 摘要算法。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = root / project_ledger.LEDGER_DIR_NAME
            deadline = (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            project_ledger.create_project_ledger(
                ledger_root,
                project_id="candidate-readiness-integration",
                absolute_deadline_utc=deadline,
                deadline_approved_by="测试批准人",
                estimation_policy="none",
                estimation_policy_approved_by="测试批准人",
                fixture_only=False,
                live_request_budget=10,
                initial_identity_keys=[],
            )
            campaign = root / "evidence" / "campaigns" / "campaign-a"
            campaign.mkdir(parents=True, mode=0o700)
            for parent in (
                root / "evidence",
                root / "evidence" / "campaigns",
                campaign,
            ):
                parent.chmod(0o700)
            campaign_manifest = campaign / "campaign.json"
            campaign_manifest.write_text(
                json.dumps(
                    {
                        "campaign_id": "campaign-a",
                        "campaign_mode": "formal",
                        "target_version": "0.154.0",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            campaign_manifest.chmod(0o600)
            with project_ledger.admission_scope(
                campaign,
                campaign_id="campaign-a",
                campaign_mode="formal",
                target_version="0.154.0",
                require=True,
            ) as admission:
                if admission is None:
                    raise AssertionError("项目总账注册 admission 缺失")
                admission.register(
                    campaign,
                    campaign_id="campaign-a",
                    campaign_mode="formal",
                    target_version="0.154.0",
                    deadline_at_utc=None,
                )

            static_path, static_receipt = readiness.write_static_receipt(
                campaign
                / "control"
                / "candidate-readiness"
                / "candidate-a"
                / "static-receipts",
                campaign_id="campaign-a",
                candidate_id="candidate-a",
                image_id=IMAGE_ID,
                build_receipt_sha256=SHA_C,
                checks=_static_checks(),
            )
            runtime = _ProbeRuntime()
            with project_ledger.runtime_admission_scope(
                campaign,
                require=True,
            ) as runtime_admission:
                if runtime_admission is None:
                    raise AssertionError("项目总账运行 admission 缺失")
                receipt = readiness.ensure_models_probe(
                    campaign,
                    campaign_id="campaign-a",
                    candidate_id="candidate-a",
                    image_id=IMAGE_ID,
                    build_receipt_sha256=SHA_C,
                    static_receipt_digest=str(static_receipt["receipt_digest"]),
                    target_version="0.154.0",
                    configuration={
                        "service_container": "service",
                        "postgres_container": "postgres",
                        "codex_account_id": 22,
                        "api_key_id": 9,
                    },
                    admission=runtime_admission,
                    runner=runtime.runner,
                    dispatch=lambda *_args: (
                        200,
                        json.dumps(
                            {"models": [{"slug": "gpt-test"}]}
                        ).encode("utf-8"),
                    ),
                    restart=runtime.restart,
                    now=datetime.now(timezone.utc),
                )
                head_sequence = runtime_admission.head_sequence
                head_sha256 = runtime_admission.head_sha256
                history = project_ledger.read_project_history(ledger_root)

            session_root = (
                campaign
                / "control"
                / "candidate-readiness"
                / "candidate-a"
                / "models-probes"
                / f"session-{receipt['session_id']}"
            )
            replayed = readiness.replay_models_probe_bundle(
                session_root,
                service_container="service",
                image_id=IMAGE_ID,
                project_ledger_history=history,
                reservation_head_sequence=head_sequence,
                reservation_head_sha256=head_sha256,
                current_time=datetime.now(timezone.utc),
            )
            self.assertEqual(replayed["receipt"], receipt)
            probe_path = session_root / "receipt.json"
            binding = {
                "schema_version": codex_upgrade.CANDIDATE_READINESS_BINDING_SCHEMA,
                "image_id": IMAGE_ID,
                "build_receipt_sha256": SHA_C,
                "static_receipt": codex_upgrade._candidate_readiness_file_binding(
                    campaign,
                    static_path,
                    static_receipt,
                ),
                "models_probe_receipt": (
                    codex_upgrade._candidate_readiness_file_binding(
                        campaign,
                        probe_path,
                        receipt,
                    )
                ),
                "project_head_sequence": head_sequence,
                "project_head_sha256": head_sha256,
            }
            manifest = {
                "campaign_id": "campaign-a",
                "target_version": "0.154.0",
                "configuration": {
                    "service_container": "service",
                    "postgres_container": "postgres",
                    "codex_account_id": 22,
                    "api_key_id": 9,
                },
            }
            validated = codex_upgrade._validate_candidate_readiness_binding(
                campaign,
                manifest,
                "candidate-a",
                binding,
            )
            self.assertEqual(validated, binding)
            (session_root / "dispatch-01.result.json").unlink()
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "bundle 的 dispatch 文件闭集",
            ):
                codex_upgrade._validate_candidate_readiness_binding(
                    campaign,
                    manifest,
                    "candidate-a",
                    binding,
                )

    def test_probe_bundle_replay_rejects_missing_or_tampered_cache_epoch(self) -> None:
        for role in ("before", "after"):
            for mutation in ("missing", "tampered"):
                with self.subTest(
                    role=role,
                    mutation=mutation,
                ), tempfile.TemporaryDirectory() as directory:
                    fixture = self._probe_bundle_fixture(Path(directory))
                    session_root = fixture["session_root"]
                    assert isinstance(session_root, Path)
                    cache_path = session_root / f"cache-epoch-{role}.json"
                    if mutation == "missing":
                        cache_path.unlink()
                    else:
                        payload = json.loads(cache_path.read_text(encoding="utf-8"))
                        payload["epoch"]["health"] = "tampered"
                        cache_path.write_text(
                            json.dumps(payload, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                    with self.assertRaises(ValueError):
                        self._replay_probe_fixture(fixture)

    def test_probe_bundle_replay_rejects_forged_operation_or_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._probe_bundle_fixture(Path(directory))
            session_root = fixture["session_root"]
            assert isinstance(session_root, Path)
            receipt_path = session_root / "receipt.json"
            forged_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            forged_receipt["accounting_operation_ids"] = [
                "candidate-probe:forged"
            ]
            unsigned = dict(forged_receipt)
            unsigned.pop("receipt_digest")
            forged_receipt["receipt_digest"] = readiness._digest(unsigned)
            receipt_path.write_bytes(readiness._canonical(forged_receipt))
            with self.assertRaisesRegex(ValueError, "账务 operation"):
                self._replay_probe_fixture(fixture)

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._probe_bundle_fixture(Path(directory))
            fixture["head_sha256"] = SHA_B
            with self.assertRaisesRegex(ValueError, "head 祖先链"):
                self._replay_probe_fixture(fixture)

    def test_each_job_rechecks_fresh_state_before_dispatch(self) -> None:
        """每个实际执行 Job 都必须先保存新观察，再进入带重试的派发边界。"""

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(codex_upgrade._run_capture_attempt))
        )
        job_loops = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "job"
            and isinstance(node.iter, ast.Name)
            and node.iter.id == "jobs"
        ]
        self.assertEqual(len(job_loops), 1)
        loop = job_loops[0]
        calls = [node for node in ast.walk(loop) if isinstance(node, ast.Call)]

        def call_name(call: ast.Call) -> str | None:
            if isinstance(call.func, ast.Name):
                return call.func.id
            if isinstance(call.func, ast.Attribute):
                return call.func.attr
            return None

        fresh = [call for call in calls if call_name(call) == "_candidate_readiness_checks"]
        writes = [call for call in calls if call_name(call) == "write_job_recheck"]
        dispatches = [call for call in calls if call_name(call) == "_run_job_with_retry"]
        self.assertEqual((len(fresh), len(writes), len(dispatches)), (1, 1, 1))
        self.assertLess(fresh[0].lineno, writes[0].lineno)
        self.assertLess(writes[0].lineno, dispatches[0].lineno)
        write_source = ast.unparse(writes[0])
        self.assertIn("candidate-readiness-rechecks", write_source)
        self.assertIn("initial=candidate_readiness_static_receipt", write_source)
        self.assertIn("fresh_checks=fresh_checks", write_source)

    def test_project_lock_probe_cas_then_reservation_order(self) -> None:
        """主流程必须在同一项目锁内完成静态门禁、probe、CAS 与预约。"""

        events: list[str] = []
        admission = _Admission(events)
        static_receipt = {"receipt_digest": SHA_B}
        probe_receipt = {"session_id": "probe-a"}

        @contextlib.contextmanager
        def admission_scope(*_args: object, **_kwargs: object):
            events.append("project-lock-enter")
            try:
                yield admission
            finally:
                events.append("project-lock-exit")

        def ensure_probe(*_args: object, **kwargs: object) -> dict[str, object]:
            events.append("probe")
            self.assertEqual(kwargs["static_receipt_digest"], SHA_B)
            self.assertIs(kwargs["admission"], admission)
            return probe_receipt

        def reserve(*_args: object, **kwargs: object):
            events.append("campaign-reservation")
            self.assertEqual(
                kwargs["candidate_readiness"]["project_head_sequence"],
                admission.head_sequence,
            )
            return Path("/tmp/attempt-a"), {"schema_version": "fixture"}

        manifest = {
            "campaign_id": "campaign-a",
            "campaign_mode": "formal",
            "campaign_purpose": "validation_only",
            "target_version": "0.154.0",
            "configuration": {
                "codex_account_id": 22,
                "api_key_id": 9,
                "service_container": "candidate-service",
            },
        }
        identity = {"image_id": IMAGE_ID}
        binding = {
            "path": "control/candidate-readiness/candidate-a/fixture.json",
            "sha256": SHA_C,
            "bytes": 1,
            "receipt_digest": SHA_B,
        }

        with (
            mock.patch.object(
                codex_upgrade.codex_upgrade_project_ledger,
                "runtime_admission_scope",
                side_effect=admission_scope,
            ),
            mock.patch.object(
                codex_upgrade,
                "_project_ledger_required",
                return_value=True,
            ),
            mock.patch.object(
                codex_upgrade,
                "_failed_capture_attempts",
                return_value=[],
            ),
            mock.patch.object(
                codex_upgrade,
                "_candidate_readiness_checks",
                side_effect=lambda **_kwargs: events.append("static-checks") or [],
            ),
            mock.patch.object(
                readiness,
                "write_static_receipt",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("static-write")
                    or (Path("/tmp/static.json"), static_receipt)
                ),
            ),
            mock.patch.object(
                readiness,
                "validate_static_receipt",
                side_effect=lambda payload, **_kwargs: (
                    events.append("static-validate") or payload
                ),
            ),
            mock.patch.object(
                readiness,
                "assert_static_passed",
                side_effect=lambda *_args, **_kwargs: events.append("static-pass"),
            ),
            mock.patch.object(
                readiness,
                "ensure_models_probe",
                autospec=True,
                side_effect=ensure_probe,
            ),
            mock.patch.object(
                codex_upgrade,
                "_candidate_readiness_file_binding",
                return_value=binding,
            ),
            mock.patch.object(
                codex_upgrade,
                "_validate_candidate_readiness_binding",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("binding-validated") or _args[-1]
                ),
            ),
            mock.patch.object(
                codex_upgrade,
                "_reserve_capture_attempt",
                side_effect=reserve,
            ),
        ):
            codex_upgrade._reserve_candidate_capture_attempt(
                Path("/tmp/campaign-a"),
                manifest=manifest,
                candidate_id="candidate-a",
                identity=identity,
                jobs=[mock.Mock()],
                build_receipt_binding={"sha256": SHA_C},
                allow_failed_rerun=False,
                deadline=_Deadline(events),
                lease=None,
            )

        ordered = [
            "project-lock-enter",
            "static-checks",
            "static-write",
            "static-validate",
            "static-pass",
            "probe",
            "project-cas",
            "binding-validated",
            "campaign-reservation",
            "project-lock-exit",
        ]
        cursor = 0
        for event in events:
            if cursor < len(ordered) and event == ordered[cursor]:
                cursor += 1
        self.assertEqual(cursor, len(ordered), events)

    def test_loader_accepts_historical_v2_and_requires_v3_readiness(self) -> None:
        """历史 v2 保持只读兼容，新 Candidate v3 必须携带 readiness。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            attempt_root = (
                campaign_dir
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "attempt-a"
            )
            attempt_root.mkdir(parents=True)
            (campaign_dir / "campaign.json").write_text("{}\n", encoding="utf-8")
            manifest = {
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
            }
            reservation = {
                "schema_version": codex_upgrade.LEGACY_CAPTURE_RESERVATION_SCHEMA,
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign_dir / "campaign.json"
                ),
                "phase": "candidate",
                "candidate_id": "candidate-a",
                "candidate_purpose": "validation_only",
                "attempt_id": "attempt-a",
                "run_nonce": SHA_A,
                "started_at_utc": "2026-09-17T00:00:00Z",
                "identity_sha256": SHA_B,
                "planned_jobs": [
                    {"id": "job-a", "required": True, "execution_sha256": SHA_C}
                ],
            }
            reservation["reservation_digest"] = codex_upgrade._fingerprint(
                reservation
            )
            path = attempt_root / "reservation.json"
            path.write_text(
                json.dumps(reservation, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            loaded = codex_upgrade._load_capture_reservation(
                campaign_dir,
                attempt_root,
                phase="candidate",
                candidate_id="candidate-a",
                _manifest=manifest,
            )
            self.assertEqual(
                loaded["schema_version"],
                codex_upgrade.LEGACY_CAPTURE_RESERVATION_SCHEMA,
            )

            reservation["schema_version"] = codex_upgrade.CAPTURE_RESERVATION_SCHEMA
            reservation.pop("reservation_digest")
            reservation["reservation_digest"] = codex_upgrade._fingerprint(
                reservation
            )
            path.write_text(
                json.dumps(reservation, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "就绪绑定字段不闭合",
            ):
                codex_upgrade._load_capture_reservation(
                    campaign_dir,
                    attempt_root,
                    phase="candidate",
                    candidate_id="candidate-a",
                    _manifest=manifest,
                )

    def test_v3_probe_identity_must_match_frozen_account_key_and_version(self) -> None:
        """probe 自身合法也不能跨 Campaign 的冻结账号、Key 或版本复用。"""

        static = {"receipt_digest": SHA_B}
        valid_probe = {
            "schema_version": readiness.PROBE_RECEIPT_SCHEMA,
            "campaign_id": "campaign-a",
            "candidate_id": "candidate-a",
            "image_id": IMAGE_ID,
            "build_receipt_sha256": SHA_C,
            "static_receipt_digest": SHA_B,
            "target_version": "0.154.0",
            "codex_account_id": 22,
            "api_key_id": 9,
            "status": "passed",
            "accounting_category": readiness.ACCOUNTING_CATEGORY,
            "failure_observations": [],
            "project_head_sequence": 7,
            "project_head_sha256": SHA_A,
            "cache_isolation": {"restart_after_probe": True},
        }
        binding = {
            "schema_version": codex_upgrade.CANDIDATE_READINESS_BINDING_SCHEMA,
            "image_id": IMAGE_ID,
            "build_receipt_sha256": SHA_C,
            "static_receipt": {},
            "models_probe_receipt": {},
            "project_head_sequence": 7,
            "project_head_sha256": SHA_A,
        }
        manifest = {
            "campaign_id": "campaign-a",
            "target_version": "0.154.0",
            "configuration": {
                "codex_account_id": 22,
                "api_key_id": 9,
                "service_container": "candidate-service",
            },
        }
        mismatches = {
            "target_version": "0.153.0",
            "codex_account_id": 999,
            "api_key_id": 998,
        }
        for field, wrong_value in mismatches.items():
            with self.subTest(field=field):
                probe = {**valid_probe, field: wrong_value}
                session = {
                    "target_version": probe["target_version"],
                    "codex_account_id": probe["codex_account_id"],
                    "api_key_id": probe["api_key_id"],
                }
                bound_files = [
                    (Path("/tmp/static.json"), static),
                    (Path("/tmp/session-probe-a/receipt.json"), probe),
                ]
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_candidate_readiness_bound_file",
                        side_effect=bound_files,
                    ),
                    mock.patch.object(
                        readiness,
                        "validate_static_receipt",
                        side_effect=lambda payload, **_kwargs: payload,
                    ),
                    mock.patch.object(readiness, "assert_static_passed"),
                    mock.patch.object(
                        codex_upgrade.codex_upgrade_project_ledger,
                        "find_project_ledger",
                        return_value=Path("/tmp/project-ledger"),
                    ),
                    mock.patch.object(
                        codex_upgrade.codex_upgrade_project_ledger,
                        "read_project_history_snapshot",
                        return_value={},
                    ),
                    mock.patch.object(
                        readiness,
                        "replay_models_probe_bundle",
                        return_value={"session": session, "receipt": probe},
                    ),
                ):
                    with self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "身份或缓存隔离非法",
                    ):
                        codex_upgrade._validate_candidate_readiness_binding(
                            Path("/tmp/campaign-a"),
                            manifest,
                            "candidate-a",
                            binding,
                        )


if __name__ == "__main__":
    unittest.main()
