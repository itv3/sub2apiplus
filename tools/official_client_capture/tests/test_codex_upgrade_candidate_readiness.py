from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_candidate_readiness as readiness


IMAGE_ID = "sha256:" + "1" * 64
BUILD_SHA256 = "2" * 64
STATIC_DIGEST = "4" * 64


class FakeAdmission:
    def __init__(self, *, budget: int | None = 100) -> None:
        self._sequence = 10
        self._sha256 = "3" * 64
        self.budget = budget
        self.calls: list[dict[str, object]] = []
        self.operations: dict[str, dict[str, object]] = {}

    @property
    def head_sequence(self) -> int:
        return self._sequence

    @property
    def head_sha256(self) -> str:
        return self._sha256

    @property
    def remaining_live_requests(self) -> int | None:
        if self.budget is None:
            return None
        return max(self.budget - len(self.operations), 0)

    def account_candidate_probe(self, **kwargs: object) -> dict[str, object]:
        dispatch_id = str(kwargs["dispatch_id"])
        existing = self.operations.get(dispatch_id)
        self.calls.append(dict(kwargs))
        if existing is not None:
            result = dict(existing)
            result["status"] = "duplicate"
            result["head_sequence"] = self._sequence
            result["head_sha256"] = self._sha256
            result["remaining_live_requests"] = self.remaining_live_requests
            return result
        if self.remaining_live_requests == 0:
            raise AssertionError("测试 admission 检测到超预算入账")
        if kwargs.get("expected_head_sha256") != self._sha256:
            raise AssertionError("测试 admission 收到陈旧 head")
        self._sequence += 1
        self._sha256 = f"{self._sequence:064x}"
        result: dict[str, object] = {
            "operation_id": f"candidate-probe:{dispatch_id}",
            "status": "appended",
            "head_sequence": self._sequence,
            "head_sha256": self._sha256,
        }
        self.operations[dispatch_id] = dict(result)
        result["remaining_live_requests"] = self.remaining_live_requests
        self.operations[dispatch_id] = dict(result)
        return result


class FakeRuntime:
    def __init__(self, *, api_key: str = "secret-api-key") -> None:
        self.restart_count = 0
        self.api_key = api_key

    def runner(
        self, arguments: list[str] | tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        values = list(arguments)
        if values[:2] == ["docker", "inspect"] and values[-1] == "postgres":
            output = json.dumps(
                [{"Config": {"Env": ["POSTGRES_USER=u", "POSTGRES_DB=d"]}}]
            )
        elif values[:3] == ["docker", "exec", "postgres"]:
            output = self.api_key + "\n"
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
            "started_at_utc": f"2026-09-17T00:00:{self.restart_count:02d}Z",
            "health": "healthy",
        }


def configuration() -> dict[str, object]:
    return {
        "service_container": "service",
        "postgres_container": "postgres",
        "codex_account_id": 9,
        "api_key_id": 4,
    }


def models_body() -> bytes:
    return json.dumps({"models": [{"slug": "gpt-test"}]}).encode("utf-8")


def static_checks() -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for check_id, failure_code in readiness.STATIC_CHECK_FAILURE_CODES.items():
        identity: dict[str, object] = {"check_id": check_id}
        if check_id == "candidate-readiness.image":
            identity.update(
                {
                    "candidate_id": "candidate-a",
                    "image_id": IMAGE_ID,
                    "build_receipt_sha256": BUILD_SHA256,
                }
            )
        checks.append(
            {
                "check_id": check_id,
                "failure_code": failure_code,
                "status": "passed",
                "error_type": None,
                "evidence": {"identity": identity},
            }
        )
    return checks


def probe_kwargs(
    runtime: FakeRuntime,
    admission: FakeAdmission,
    dispatch: readiness.Dispatch,
) -> dict[str, object]:
    return {
        "campaign_id": "campaign-a",
        "candidate_id": "candidate-a",
        "image_id": IMAGE_ID,
        "build_receipt_sha256": BUILD_SHA256,
        "static_receipt_digest": STATIC_DIGEST,
        "target_version": "0.154.0",
        "configuration": configuration(),
        "admission": admission,
        "runner": runtime.runner,
        "dispatch": dispatch,
        "restart": runtime.restart,
    }


class CandidateReadinessTests(unittest.TestCase):
    def test_static_failure_receipt_is_zero_request_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checks = static_checks()
            checks[0] = {
                **checks[0],
                "status": "failed",
                "error_type": "RuntimeError",
                "evidence": {},
            }
            path, receipt = readiness.write_static_receipt(
                Path(directory),
                campaign_id="campaign-a",
                candidate_id="candidate-a",
                image_id=IMAGE_ID,
                build_receipt_sha256=BUILD_SHA256,
                checks=checks,
            )
            self.assertEqual(receipt["live_request_count"], 0)
            self.assertEqual(receipt["status"], "failed")
            validated = readiness.validate_static_receipt(
                receipt,
                campaign_id="campaign-a",
                candidate_id="candidate-a",
                image_id=IMAGE_ID,
                build_receipt_sha256=BUILD_SHA256,
            )
            with self.assertRaises(readiness.CandidateReadinessError) as caught:
                readiness.assert_static_passed(path, validated)
            self.assertEqual(caught.exception.failure_class, "environment-prerequisite")
            self.assertEqual(
                caught.exception.failure_observations,
                [
                    {
                        "check_id": "candidate-readiness.storage",
                        "failure_code": "host-or-container-path-not-writable",
                    }
                ],
            )

    def test_static_receipt_and_checks_require_fixed_field_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, receipt = readiness.write_static_receipt(
                Path(directory),
                campaign_id="campaign-a",
                candidate_id="candidate-a",
                image_id=IMAGE_ID,
                build_receipt_sha256=BUILD_SHA256,
                checks=static_checks(),
            )
            extra = dict(receipt)
            extra["unexpected"] = True
            unsigned = dict(extra)
            unsigned.pop("receipt_digest")
            extra["receipt_digest"] = readiness._digest(unsigned)
            with self.assertRaisesRegex(ValueError, "字段闭集"):
                readiness.validate_static_receipt(
                    extra,
                    campaign_id="campaign-a",
                    candidate_id="candidate-a",
                    image_id=IMAGE_ID,
                    build_receipt_sha256=BUILD_SHA256,
                )
            with self.assertRaisesRegex(ValueError, "数量不闭合"):
                readiness.write_static_receipt(
                    Path(directory) / "bad",
                    campaign_id="campaign-a",
                    candidate_id="candidate-a",
                    image_id=IMAGE_ID,
                    build_receipt_sha256=BUILD_SHA256,
                    checks=static_checks()[:-1],
                )

    def test_static_ttl_and_job_recheck_use_fresh_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = datetime(2026, 9, 17, tzinfo=timezone.utc)
            _, receipt = readiness.write_static_receipt(
                root / "static",
                campaign_id="campaign-a",
                candidate_id="candidate-a",
                image_id=IMAGE_ID,
                build_receipt_sha256=BUILD_SHA256,
                checks=static_checks(),
                ttl_seconds=10,
                observed_at_utc="2026-09-17T00:00:00.000Z",
            )
            with self.assertRaisesRegex(readiness.CandidateReadinessError, "已过期"):
                readiness.validate_static_receipt(
                    receipt,
                    campaign_id="campaign-a",
                    candidate_id="candidate-a",
                    image_id=IMAGE_ID,
                    build_receipt_sha256=BUILD_SHA256,
                    now=start + timedelta(seconds=11),
                )
            path, recheck = readiness.write_job_recheck(
                root / "jobs",
                initial=receipt,
                fresh_checks=static_checks(),
                job_id="candidate-core",
                now=start + timedelta(hours=1),
            )
            self.assertTrue(path.is_file())
            self.assertEqual(recheck["status"], "passed")

            drifted = static_checks()
            drifted[0] = {
                **drifted[0],
                "evidence": {"identity": {"check_id": "changed"}},
            }
            with self.assertRaises(readiness.CandidateReadinessError) as caught:
                readiness.write_job_recheck(
                    root / "jobs-drift",
                    initial=receipt,
                    fresh_checks=drifted,
                    job_id="candidate-drift",
                    now=start + timedelta(hours=1),
                )
            self.assertEqual(caught.exception.failure_code, "external-state-drift")

    def test_routing_platform_isolation_and_mapping_fail_independently(self) -> None:
        config = {"codex_account_id": 9, "api_key_id": 4}
        payload = {
            "account_id": 9,
            "account_platform": "anthropic",
            "account_type": "oauth",
            "account_status": "active",
            "account_schedulable": True,
            "parent_account_id": None,
            "token_present": True,
            "api_key_id": 4,
            "api_key_status": "active",
            "group_id": 7,
            "group_platform": "openai",
            "group_status": "active",
            "eligible_account_ids": [8, 9],
            "model_mapping_type": "object",
            "model_mapping_count": 1,
        }
        with self.assertRaisesRegex(RuntimeError, "平台"):
            readiness._routing_platform_fact(config, payload)
        with self.assertRaisesRegex(RuntimeError, "隔离"):
            readiness._account_isolation_fact(config, payload)
        with self.assertRaisesRegex(RuntimeError, "model_mapping"):
            readiness._model_mapping_fact(payload)

    def test_probe_success_replays_accounting_without_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            dispatch_count = 0

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 200, models_body()

            common = probe_kwargs(runtime, admission, dispatch)
            receipt = readiness.ensure_models_probe(Path(directory), **common)
            calls_after_first = len(admission.calls)
            again = readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(receipt, again)
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["dispatch_count"], 1)
            self.assertEqual(receipt["static_receipt_digest"], STATIC_DIGEST)
            self.assertEqual(receipt["codex_account_id"], 9)
            self.assertEqual(receipt["api_key_id"], 4)
            self.assertEqual(receipt["accounting_policy"], readiness.ACCOUNTING_POLICY)
            self.assertEqual(
                receipt["cache_isolation"]["policy"], readiness.CACHE_POLICY
            )
            observed = readiness._parse_time(receipt["observed_at_utc"], "observed")
            expires = readiness._parse_time(receipt["expires_at_utc"], "expires")
            self.assertEqual(expires - observed, timedelta(seconds=600))
            self.assertEqual(dispatch_count, 1)
            self.assertEqual(len(admission.operations), 1)
            self.assertEqual(len(admission.calls), calls_after_first + 1)
            self.assertEqual(runtime.restart_count, 2)

            # 即使总账侧缺少操作，fresh passed receipt 也必须按落盘 result 补做
            # 同一幂等操作，且不能重新发送 wire 请求。
            repaired_admission = FakeAdmission()
            repaired = dict(common)
            repaired["admission"] = repaired_admission
            replayed = readiness.ensure_models_probe(Path(directory), **repaired)
            self.assertEqual(replayed, receipt)
            self.assertEqual(len(repaired_admission.operations), 1)
            self.assertEqual(dispatch_count, 1)
            self.assertEqual(runtime.restart_count, 2)

            sessions = list(
                (
                    Path(directory)
                    / "control"
                    / "candidate-readiness"
                    / "candidate-a"
                    / "models-probes"
                ).glob("session-*")
            )
            session = readiness._load_json(sessions[0] / "session.json", "session")
            self.assertEqual(set(session), readiness.PROBE_SESSION_FIELDS)
            self.assertEqual(session["max_dispatches"], 2)
            self.assertEqual(session["ttl_seconds"], 600)
            for role in ("before", "after"):
                cache_path = sessions[0] / f"cache-epoch-{role}.json"
                self.assertTrue(cache_path.is_file())
                self.assertEqual(
                    readiness._file_sha256(cache_path),
                    receipt["cache_isolation"][
                        f"cache_epoch_{role}_receipt_sha256"
                    ],
                )
            cache_after = readiness._load_json(
                sessions[0] / "cache-epoch-after.json",
                "cache epoch after",
            )
            self.assertEqual(
                cache_after["epoch"],
                receipt["cache_isolation"]["cache_epoch_after"],
            )

    def test_probe_receipt_rejects_bad_ttl_and_unknown_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            common = probe_kwargs(
                runtime, admission, lambda *_arguments: (200, models_body())
            )
            readiness.ensure_models_probe(Path(directory), **common)
            receipt_path = next(
                (
                    Path(directory)
                    / "control"
                    / "candidate-readiness"
                    / "candidate-a"
                    / "models-probes"
                ).glob("session-*/receipt.json")
            )
            original = json.loads(receipt_path.read_text(encoding="utf-8"))
            extra = dict(original)
            extra["unexpected"] = True
            unsigned = dict(extra)
            unsigned.pop("receipt_digest")
            extra["receipt_digest"] = readiness._digest(unsigned)
            receipt_path.write_bytes(readiness._canonical(extra))
            os.chmod(receipt_path, 0o600)
            with self.assertRaisesRegex(ValueError, "字段闭集"):
                readiness.ensure_models_probe(Path(directory), **common)

            bad_time = dict(original)
            observed = readiness._parse_time(bad_time["observed_at_utc"], "observed")
            bad_time["expires_at_utc"] = readiness._utc_text(
                observed + timedelta(seconds=601)
            )
            unsigned = dict(bad_time)
            unsigned.pop("receipt_digest")
            bad_time["receipt_digest"] = readiness._digest(unsigned)
            receipt_path.write_bytes(readiness._canonical(bad_time))
            os.chmod(receipt_path, 0o600)
            with self.assertRaisesRegex(ValueError, "时间与 TTL"):
                readiness.ensure_models_probe(Path(directory), **common)

    def test_probe_retries_each_wire_and_failed_session_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            responses = [(503, b"unavailable"), (502, b"bad gateway")]

            def failing_dispatch(*_arguments: str) -> tuple[int, bytes]:
                return responses.pop(0)

            common = probe_kwargs(runtime, admission, failing_dispatch)
            with self.assertRaises(readiness.CandidateReadinessError) as caught:
                readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(caught.exception.failure_code, "no-successful-response")
            self.assertEqual(len(admission.operations), 2)

            common["dispatch"] = lambda *_arguments: (200, models_body())
            succeeded = readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(succeeded["status"], "passed")
            self.assertEqual(len(admission.operations), 3)
            sessions = list(
                (
                    Path(directory)
                    / "control"
                    / "candidate-readiness"
                    / "candidate-a"
                    / "models-probes"
                ).glob("session-*")
            )
            self.assertEqual(len(sessions), 2)

    def test_remaining_one_allows_only_one_wire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission(budget=1)
            dispatch_count = 0

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 503, b"unavailable"

            with self.assertRaises(readiness.ProbeBudgetExhaustedError) as caught:
                readiness.ensure_models_probe(
                    Path(directory), **probe_kwargs(runtime, admission, dispatch)
                )
            self.assertEqual(dispatch_count, 1)
            self.assertEqual(len(admission.operations), 1)
            self.assertEqual(caught.exception.failure_class, "request-budget-exhausted")
            self.assertEqual(
                caught.exception.failure_code, "live-request-budget-exhausted"
            )

    def test_zero_budget_writes_replayable_zero_dispatch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission(budget=0)
            dispatch_count = 0

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 200, models_body()

            common = probe_kwargs(runtime, admission, dispatch)
            with self.assertRaises(readiness.ProbeBudgetExhaustedError):
                readiness.ensure_models_probe(Path(directory), **common)
            with self.assertRaises(readiness.ProbeBudgetExhaustedError):
                readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(dispatch_count, 0)
            self.assertEqual(len(admission.operations), 0)
            receipts = list(
                (
                    Path(directory)
                    / "control"
                    / "candidate-readiness"
                    / "candidate-a"
                    / "models-probes"
                ).glob("session-*/receipt.json")
            )
            self.assertEqual(len(receipts), 2)
            for path in receipts:
                payload = readiness._load_json(path, "probe receipt")
                self.assertEqual(payload["dispatch_count"], 0)

    def test_result_then_crash_replay_does_not_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            dispatch_count = 0
            crashed = False

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 200, models_body()

            def crash_hook(point: str) -> None:
                nonlocal crashed
                if point == "after-result-before-accounting" and not crashed:
                    crashed = True
                    raise RuntimeError("模拟崩溃")

            common = probe_kwargs(runtime, admission, dispatch)
            with self.assertRaisesRegex(RuntimeError, "模拟崩溃"):
                readiness.ensure_models_probe(
                    Path(directory), crash_hook=crash_hook, **common
                )
            receipt = readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(dispatch_count, 1)
            self.assertEqual(len(admission.operations), 1)
            self.assertEqual(runtime.restart_count, 2)

    def test_accounting_then_crash_replays_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            dispatch_count = 0
            crashed = False

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 200, models_body()

            def crash_hook(point: str) -> None:
                nonlocal crashed
                if point == "after-accounting-before-receipt" and not crashed:
                    crashed = True
                    raise RuntimeError("模拟账后崩溃")

            common = probe_kwargs(runtime, admission, dispatch)
            with self.assertRaisesRegex(RuntimeError, "模拟账后崩溃"):
                readiness.ensure_models_probe(
                    Path(directory), crash_hook=crash_hook, **common
                )
            receipt = readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(dispatch_count, 1)
            self.assertEqual(len(admission.operations), 1)
            self.assertEqual(len(admission.calls), 2)

    def test_intent_without_result_is_immediately_accounting_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            dispatch_count = 0

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 200, models_body()

            def crash_hook(point: str) -> None:
                if point == "after-intent-before-dispatch":
                    raise RuntimeError("模拟 intent 后崩溃")

            common = probe_kwargs(runtime, admission, dispatch)
            with self.assertRaises(readiness.ProbeAccountingUncertainError) as caught:
                readiness.ensure_models_probe(
                    Path(directory), crash_hook=crash_hook, **common
                )
            self.assertEqual(caught.exception.failure_class, "request-accounting-uncertain")
            with self.assertRaises(readiness.ProbeAccountingUncertainError):
                readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(dispatch_count, 0)
            self.assertEqual(len(admission.operations), 0)

    def test_result_write_failure_closes_accounting_as_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            dispatch_count = 0

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 200, models_body()

            original = readiness._write_once

            def fail_result(path: Path, payload: dict[str, object]) -> None:
                if path.name.endswith(".result.json"):
                    raise OSError("模拟 result 落盘失败")
                original(path, payload)

            common = probe_kwargs(runtime, admission, dispatch)
            with mock.patch.object(readiness, "_write_once", side_effect=fail_result):
                with self.assertRaises(readiness.ProbeAccountingUncertainError):
                    readiness.ensure_models_probe(Path(directory), **common)
            with self.assertRaises(readiness.ProbeAccountingUncertainError):
                readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(dispatch_count, 1)
            self.assertEqual(len(admission.operations), 0)

    def test_receipt_write_failure_replays_without_new_wire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            dispatch_count = 0
            failed = False

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 200, models_body()

            original = readiness._write_once

            def fail_receipt(path: Path, payload: dict[str, object]) -> None:
                nonlocal failed
                if path.name == "receipt.json" and not failed:
                    failed = True
                    raise OSError("模拟 receipt 落盘失败")
                original(path, payload)

            common = probe_kwargs(runtime, admission, dispatch)
            with mock.patch.object(readiness, "_write_once", side_effect=fail_receipt):
                with self.assertRaisesRegex(OSError, "receipt"):
                    readiness.ensure_models_probe(Path(directory), **common)
            receipt = readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(dispatch_count, 1)
            self.assertEqual(len(admission.operations), 1)
            self.assertEqual(runtime.restart_count, 2)

    def test_expired_success_and_changed_identity_create_new_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            admission = FakeAdmission()
            start = datetime(2026, 9, 17, tzinfo=timezone.utc)
            common = probe_kwargs(
                runtime, admission, lambda *_arguments: (200, models_body())
            )
            common["ttl_seconds"] = 10
            readiness.ensure_models_probe(Path(directory), now=start, **common)
            readiness.ensure_models_probe(
                Path(directory), now=start + timedelta(seconds=11), **common
            )
            changed = dict(common)
            changed["static_receipt_digest"] = "5" * 64
            readiness.ensure_models_probe(
                Path(directory), now=start + timedelta(seconds=12), **changed
            )
            self.assertEqual(len(admission.operations), 3)

    def test_session_scan_detects_multiple_incomplete_and_hidden_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = readiness._probe_root(Path(directory), "candidate-a")
            base = {
                "campaign_id": "campaign-a",
                "candidate_id": "candidate-a",
                "image_id": IMAGE_ID,
                "build_receipt_sha256": BUILD_SHA256,
                "static_receipt_digest": STATIC_DIGEST,
                "target_version": "0.154.0",
                "codex_account_id": 9,
                "api_key_id": 4,
                "ttl_seconds": 600,
                "max_dispatches": 2,
            }
            first, _ = readiness._new_session(
                root, created_at_utc="2026-09-17T00:00:00.000Z", **base
            )
            readiness._new_session(
                root, created_at_utc="2026-09-17T00:00:01.000Z", **base
            )
            with self.assertRaisesRegex(ValueError, "多个未完成"):
                readiness._matching_session(root, **base)

            readiness._write_once(first / ".dispatch-01.intent.json", {"hidden": True})
            with self.assertRaisesRegex(ValueError, "非法文件"):
                readiness._probe_files(first)

    def test_matching_session_uses_created_time_not_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = readiness._probe_root(Path(directory), "candidate-a")
            base = {
                "campaign_id": "campaign-a",
                "candidate_id": "candidate-a",
                "image_id": IMAGE_ID,
                "build_receipt_sha256": BUILD_SHA256,
                "static_receipt_digest": STATIC_DIGEST,
                "target_version": "0.154.0",
                "codex_account_id": 9,
                "api_key_id": 4,
                "ttl_seconds": 600,
                "max_dispatches": 2,
            }
            with mock.patch.object(
                readiness.time, "strftime", return_value="20260917T000000Z"
            ), mock.patch.object(
                readiness.secrets,
                "token_hex",
                side_effect=["ff" * 8, "00" * 8],
            ):
                older, _ = readiness._new_session(
                    root, created_at_utc="2026-09-17T00:00:00.000Z", **base
                )
                newer, _ = readiness._new_session(
                    root, created_at_utc="2026-09-17T00:00:01.000Z", **base
                )
            readiness._write_once(older / "receipt.json", {})
            readiness._write_once(newer / "receipt.json", {})
            matched = readiness._matching_session(root, **base)
            self.assertIsNotNone(matched)
            assert matched is not None
            self.assertEqual(matched[0], newer)
            self.assertLess(newer.name, older.name)

    def test_api_key_control_character_fails_before_intent_and_can_recover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime(api_key="bad\r\nkey")
            admission = FakeAdmission()
            dispatch_count = 0

            def dispatch(*_arguments: str) -> tuple[int, bytes]:
                nonlocal dispatch_count
                dispatch_count += 1
                return 200, models_body()

            common = probe_kwargs(runtime, admission, dispatch)
            with self.assertRaisesRegex(RuntimeError, "API Key"):
                readiness.ensure_models_probe(Path(directory), **common)
            runtime.api_key = "secret-api-key"
            receipt = readiness.ensure_models_probe(Path(directory), **common)
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(dispatch_count, 1)

    def test_default_dispatch_disables_proxy_and_redirect(self) -> None:
        class Response:
            status = 302

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return b"redirect"

        class Opener:
            def __init__(self) -> None:
                self.request: urllib.request.Request | None = None

            def open(
                self, request: urllib.request.Request, *, timeout: int
            ) -> Response:
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        with mock.patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""},
            clear=False,
        ), mock.patch.object(
            readiness.urllib.request,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            status, body = readiness._default_dispatch(
                "http://127.0.0.1:49152/start",
                "secret",
                "0.154.0",
                "a" * 48,
            )
        self.assertEqual(status, 302)
        self.assertEqual(body, b"redirect")
        self.assertIsNotNone(opener.request)
        self.assertEqual(opener.request.full_url, "http://127.0.0.1:49152/start")
        handlers = build_opener.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(
            handlers[1].redirect_request(None, None, 302, "", None, "/target")
        )


if __name__ == "__main__":
    unittest.main()
