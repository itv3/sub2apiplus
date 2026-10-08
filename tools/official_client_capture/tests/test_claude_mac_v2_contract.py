"""Mac v2 关键合同与真实读侧/账务回归；不启动产品或连接网络。"""

from __future__ import annotations

import copy
import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_recorder as recorder
from tools.official_client_capture import claude_upgrade_client_receipts as importer
from tools.official_client_capture import claude_upgrade_mac_client_capture as control
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture.tests import claude_client_receipt_fixture as f


class _SealBarrierBackend:
    """仅验证收口顺序的测试记录器，不产生真实产品来源。"""

    fixture_only = True

    def __init__(self, calls, *, launch_error=None, seal_error=None):
        self.calls = calls
        self.launch_error = launch_error
        self.seal_error = seal_error

    def prepare_state(self, task, root): self.calls.append("prepare")
    def start_monitoring(self, task, root): self.calls.append("monitor")
    def recheck_dispatch(self, task): self.calls.append("recheck")
    def launch_desktop(self, task, recipe):
        self.calls.append("launch")
        if self.launch_error is not None:
            raise self.launch_error
    def drive_scenario(self, task, scenario): self.calls.append("drive")
    def stop_product(self, task): self.calls.append("stop")
    def restore_state(self, task, root): self.calls.append("restore")
    def seal_sources(self, task, root, *, status):
        self.calls.append("seal:" + status)
        if self.seal_error is not None:
            raise self.seal_error


class MacV2ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.campaign = self.root / "staging/data/evidence/campaigns/campaign-1"
        self.attempt = self.campaign / "candidates/candidate-1/attempts/attempt-1"
        self.values = {"campaign_id": "campaign-1", "candidate_id": "candidate-1", "attempt_id": "attempt-1", "run_nonce": "nonce-1"}
        f.write(self.attempt / "attempt.json", self.values)
        f.write(self.campaign / "campaign.json", {"campaign_id": "campaign-1", "client": "claude"})
        self.patch = f.source_patch()
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.task_path, self.bundle, self.capture = f.material(self.attempt, "claude-desktop", self.values)
        self.task = c.read_json(self.task_path)

    def verify(self) -> dict:
        return c.verify_client_bundle(self.task, self.bundle, self.capture)

    def inventory(self) -> None:
        manifest = c.read_json(self.bundle / "source-manifest.json")
        manifest["files"] = [c.binding(p, self.bundle) for p in sorted(self.bundle.rglob("*")) if p.is_file() and p.name != "source-manifest.json"]
        f.write(self.bundle / "source-manifest.json", manifest)

    def rewrite(self, name: str, value: dict) -> None:
        f.write(self.bundle / name, value)
        self.inventory()

    def proof(self, root: Path, row: dict) -> None:
        path = root / row["observation"]["path"]
        f.write(path, {k: v for k, v in row.items() if k != "observation"})
        row["observation"] = c.binding(path, root)

    def capture_binding(self, key: str, name: str) -> None:
        manifest = c.read_json(self.capture / "capture-manifest.json")
        manifest[key] = c.binding(self.capture / name, self.capture)
        f.write(self.capture / "capture-manifest.json", manifest)

    def test_app_bundle_executable_and_shared_modules_are_distinct(self) -> None:
        self.assertEqual(self.verify()["status"], "verified")
        artifacts = {a["role"]: a for a in self.task["client_artifacts"]}
        self.assertNotEqual(artifacts["application"]["content"]["sha256"], artifacts["application_executable"]["content"]["sha256"])
        path, bundle, capture = f.material(self.attempt, "claude-vscode", self.values)
        session = c.read_json(bundle / "process-session.json")
        self.assertEqual(len(session["loaded_modules"]), 2)
        self.assertEqual(len({m["host_process_instance_id"] for m in session["loaded_modules"]}), 1)
        self.assertEqual(c.verify_client_bundle(c.read_json(path), bundle, capture)["status"], "verified")

    def test_application_directory_cannot_stand_in_for_executable(self) -> None:
        session = c.read_json(self.bundle / "process-session.json")
        session["processes"][0]["executable_artifact_id"] = "application"
        session["processes"][0]["executable_sha256"] = "1" * 64
        self.proof(self.bundle, session["processes"][0])
        self.rewrite("process-session.json", session)
        with self.assertRaisesRegex(ValueError, "不能代替宿主 executable"):
            self.verify()

    def test_pid_reuse_with_changed_boot_or_start_is_rejected(self) -> None:
        original = c.read_json(self.bundle / "process-session.json")
        for field in ("boot_id", "start_marker"):
            with self.subTest(field=field):
                session = copy.deepcopy(original)
                session["processes"][0][field] = "changed-instance"
                self.proof(self.bundle, session["processes"][0])
                self.rewrite("process-session.json", session)
                with self.assertRaisesRegex(ValueError, "boot/start 实例不符"):
                    self.verify()

    def test_missing_module_and_activation_after_socket_are_rejected(self) -> None:
        original = c.read_json(self.bundle / "process-session.json")
        missing = copy.deepcopy(original)
        missing["loaded_modules"] = []
        self.rewrite("process-session.json", missing)
        with self.assertRaisesRegex(ValueError, "真实内置 CLI"):
            self.verify()
        late = copy.deepcopy(original)
        late["loaded_modules"][0]["activated_at_utc"] = "2026-10-08T00:00:06Z"
        self.proof(self.bundle, late["loaded_modules"][0])
        self.rewrite("process-session.json", late)
        with self.assertRaisesRegex(ValueError, "模块调用／激活关联"):
            self.verify()

    def test_native_server_cannot_claim_a_mac_pid(self) -> None:
        row = json.loads((self.capture / "requests.jsonl").read_bytes())
        row["process_pid"] = 100
        f.write(self.capture / "requests.jsonl", row)
        self.capture_binding("requests", "requests.jsonl")
        with self.assertRaisesRegex(ValueError, "服务端原生请求 字段闭集"):
            self.verify()

    def test_proxy_nat_must_close_on_independent_connection_original(self) -> None:
        mapping = json.loads((self.capture / "transport-map.jsonl").read_bytes())
        connection = json.loads((self.capture / "connections.jsonl").read_bytes())
        initial = {k: connection[k] for k in ("client_endpoint", "server_endpoint")}
        connection["client_endpoint"] = "10.0.0.2:9010"
        connection["server_endpoint"] = "10.0.0.3:8080"
        mapping["hops"] = [{"before": initial, "after": {k: connection[k] for k in initial}}]
        self.proof(self.capture, mapping)
        self.proof(self.capture, connection)
        f.write(self.capture / "transport-map.jsonl", mapping)
        f.write(self.capture / "connections.jsonl", connection)
        self.capture_binding("transport_map", "transport-map.jsonl")
        self.capture_binding("connections", "connections.jsonl")
        self.assertEqual(self.verify()["status"], "verified")
        mapping["hops"][0]["before"]["client_endpoint"] = "10.2.3.4:5000"
        self.proof(self.capture, mapping)
        f.write(self.capture / "transport-map.jsonl", mapping)
        self.capture_binding("transport_map", "transport-map.jsonl")
        with self.assertRaisesRegex(ValueError, "映射链有缺口"):
            self.verify()

    def test_over_budget_keeps_all_native_requests(self) -> None:
        first = json.loads((self.capture / "requests.jsonl").read_bytes())
        rows = [{**first, "request_ordinal": n} for n in range(1, 12)]
        (self.capture / "requests.jsonl").write_bytes(b"".join(c.canonical(row)+b"\n" for row in rows))
        self.capture_binding("requests", "requests.jsonl")
        call = json.loads((self.bundle / "socket-calls.jsonl").read_bytes())
        call["request_ordinals"] = list(range(1, 12))
        self.proof(self.bundle, call)
        self.rewrite("socket-calls.jsonl", call)
        verified = self.verify()
        self.assertEqual((verified["status"], verified["accounting_status"], len(verified["requests"])), ("failed", "resolved", 11))
        self.assertEqual(len({r["identity_key"] for r in verified["requests"]}), 11)

    def test_restore_failed_prevents_success_and_keeps_requests(self) -> None:
        restoration = c.read_json(self.bundle / "state-restoration.json")
        restoration["status"] = "failed"
        journal = source._json_lines(self.bundle / "recorder-journal.jsonl", "夹具 journal")
        journal[-2]["event"] = "state_restore_failed"
        self.proof(self.bundle, journal[-2])
        (self.bundle / "recorder-journal.jsonl").write_bytes(b"".join(c.canonical(row)+b"\n" for row in journal))
        restoration["journal"] = c.binding(self.bundle / "recorder-journal.jsonl", self.bundle)
        execution = c.read_json(self.bundle / "recorder-execution.json")
        execution["journal"] = restoration["journal"]
        f.write(self.bundle / "recorder-execution.json", execution)
        self.rewrite("state-restoration.json", restoration)
        verified = self.verify()
        self.assertEqual((verified["status"], len(verified["requests"])), ("failed", 1))

    def test_restore_claim_cannot_disagree_with_original_journal(self) -> None:
        restoration = c.read_json(self.bundle / "state-restoration.json")
        restoration["status"] = "failed"
        self.rewrite("state-restoration.json", restoration)
        with self.assertRaisesRegex(ValueError, "journal 终态冲突"):
            self.verify()

    def test_monitoring_must_precede_product_launch(self) -> None:
        execution = c.read_json(self.bundle / "recorder-execution.json")
        execution["monitor_started_at_utc"] = "2026-10-08T00:00:03Z"
        self.rewrite("recorder-execution.json", execution)
        with self.assertRaisesRegex(ValueError, "进程监测晚于产品启动"):
            self.verify()

    def test_current_receipt_rejects_v1_and_mixed_imported_run(self) -> None:
        path = f.write_receipt(self.attempt, "claude-desktop", self.values)
        self.assertEqual(c.verify_client_receipt(self.attempt, "claude-desktop", path)["status"], "verified")
        receipt = c.read_json(path)
        receipt["schema_version"] = c.HISTORICAL_RECEIPT_SCHEMA
        f.write(path, receipt)
        with self.assertRaisesRegex(ValueError, "完整 v2"):
            c.verify_client_receipt(self.attempt, "claude-desktop", path)
        receipt["schema_version"] = c.RECEIPT_SCHEMA
        run_path = c.bound_file(self.attempt, receipt["imported_run"])
        run = c.read_json(run_path)
        run["schema_version"] = importer.HISTORICAL_RUN_SCHEMA
        f.write(run_path, run)
        receipt["imported_run"] = c.binding(run_path, self.attempt)
        f.write(path, receipt)
        with self.assertRaisesRegex(ValueError, "完整 v2 imported-run"):
            c.verify_client_receipt(self.attempt, "claude-desktop", path)

    def test_current_task_rejects_old_capture_even_with_fresh_digest(self) -> None:
        manifest = c.read_json(self.capture / "capture-manifest.json")
        manifest["schema_version"] = c.HISTORICAL_CAPTURE_SCHEMA
        f.write(self.capture / "capture-manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "旧 server capture"):
            self.verify()

    def test_unimported_reservation_and_missing_capture_are_unresolved(self) -> None:
        rows = importer.collect_campaign_runs(self.campaign)
        self.assertEqual((len(rows), rows[0]["status"], rows[0]["precise_count"]), (1, "unresolved", 1))
        keys = [r["identity_key"] for r in rows[0]["requests"]]
        self.assertEqual(keys, [r["identity_key"] for r in self.verify()["requests"]])
        shutil.rmtree(self.capture)
        rows = importer.collect_campaign_runs(self.campaign)
        self.assertEqual((len(rows), rows[0]["status"]), (1, "unresolved"))
        self.assertIn("证据", rows[0]["reason"])

    def test_reservation_index_cannot_disappear_with_the_local_task(self) -> None:
        self.task_path.unlink()
        rows = importer.collect_campaign_runs(self.campaign)
        self.assertEqual((len(rows), rows[0]["status"]), (1, "unresolved"))
        self.assertIn("缺失", rows[0]["reason"])

    def test_only_registered_independent_zero_proof_can_close_undispatched(self) -> None:
        shutil.rmtree(self.bundle)
        (self.capture / "requests.jsonl").write_bytes(b"")
        self.capture_binding("requests", "requests.jsonl")
        proof = f.write(self.capture / "undispatched-attestation.json", {"fixture_only": True, "undispatched": True, "task_sha256": self.task["task_sha256"]})
        f.write(self.capture / "undispatched.json", {"schema_version": "claude-upgrade-client-undispatched/v2", "task_sha256": self.task["task_sha256"], "producer_run_id": self.task["execution"]["producer_run_id"], "capture_id": self.task["execution"]["capture_id"], "status": "undispatched", "request_count": 0, "requests": c.binding(self.capture / "requests.jsonl", self.capture), "attestation": c.binding(proof, self.capture)})
        self.assertEqual(importer.collect_campaign_runs(self.campaign)[0]["status"], "resolved")
        with mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True):
            self.assertEqual(importer.collect_campaign_runs(self.campaign)[0]["status"], "unresolved")

    def test_independent_unimported_keys_use_existing_exact_once_accounting(self) -> None:
        row = importer.collect_campaign_runs(self.campaign)[0]
        project = f.fixture_ledger(self.attempt)
        provenance = {"requests": row["requests"], "jobs": [row], "unresolved_job_ids": [row["job_id"]]}
        result = reconciler.request_accounting(provenance, head=ledger.replay_head(project), initial_keys=set(), campaign_id="campaign-1")
        self.assertEqual((len(result["new_keys"]), result["unresolved_job_ids"]), (1, [row["job_id"]]))
        ledger.append_project_event(project, operation_id="fixture-independent-capture", event_type="reconciliation_committed", payload={"campaign_id":"campaign-1", "request":{"status":"unresolved", "identity_keys":result["new_keys"], "estimated_delta":0}}, source_batch_sha256=None)
        repeated = reconciler.request_accounting(provenance, head=ledger.replay_head(project), initial_keys=set(), campaign_id="campaign-1")
        self.assertEqual(repeated["new_keys"], [])

    def test_v1_only_flows_through_managed_historical_accounting_discovery(self) -> None:
        historical = self.attempt.parent / "historical-attempt"
        values = {**self.values, "attempt_id": "historical-attempt", "run_nonce": "historical-nonce"}
        path = f.write_receipt_v1(historical, "claude-desktop", values)
        with self.assertRaisesRegex(ValueError, "完整 v2"):
            c.verify_client_receipt(historical, "claude-desktop", path)
        rows = importer.collect_campaign_runs(self.campaign)
        historical_rows = [row for row in rows if row.get("conclusion_scope") == "historical_accounting_only"]
        self.assertEqual((len(historical_rows), historical_rows[0]["status"], historical_rows[0]["precise_count"]), (1, "resolved", 1))
        head = ledger.replay_head(f.fixture_ledger(self.attempt))
        self.assertEqual(head["precise_total"], 1)
        importer.collect_campaign_runs(self.campaign)
        self.assertEqual(ledger.replay_head(f.fixture_ledger(self.attempt))["head_sha256"], head["head_sha256"])

    def test_formal_reader_really_calls_orchestrator_wrapper(self) -> None:
        from tools.official_client_capture import claude_upgrade
        path = f.write_receipt(self.attempt, "claude-desktop", self.values)
        claude_upgrade.verify_mac_source_v2.reset_mock()
        c.verify_client_receipt(self.attempt, "claude-desktop", path)
        self.assertEqual(claude_upgrade.verify_mac_source_v2.call_count, 1)
        importer.verify_import_inputs(self.attempt, "claude-desktop", self.task_path, self.bundle)
        self.assertEqual(claude_upgrade.verify_mac_source_v2.call_count, 2)
        with mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True):
            with self.assertRaises(ValueError):
                c.verify_client_receipt(self.attempt, "claude-desktop", path)

    def test_real_product_stays_closed_without_isolation_material(self) -> None:
        with mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "真实产品隔离方法尚未验收"):
                recorder.isolation_recipe(self.task, self.bundle)

    def test_unknown_dispatch_never_restarts_product_or_reserves_budget_again(self) -> None:
        run_root = self.bundle.parent
        f.write(run_root / "dispatch-intent.json", {"task_sha256": self.task["task_sha256"]})
        services = mock.Mock(fixture_only=True)
        result = control.run_client(self.task_path, self.attempt, services=services)
        self.assertEqual(result["status"], "accounting_unresolved")
        services.reserve.assert_not_called()
        services.recorder.assert_not_called()

    def test_ready_failure_keeps_discoverable_index_and_closes_capture(self) -> None:
        services = mock.Mock(fixture_only=True)
        index_path = self.attempt / "client-evidence/reservations" / (self.task["execution"]["producer_run_id"] + ".json")
        index_path.unlink()
        services.wait_capture_ready.side_effect = RuntimeError("独立捕获未就绪")
        with self.assertRaisesRegex(RuntimeError, "未就绪"):
            control.run_client(self.task_path, self.attempt, services=services)
        services.close_capture.assert_called_once_with(self.task)
        services.recorder.assert_not_called()
        self.assertTrue(index_path.is_file())
        self.assertEqual(importer.collect_campaign_runs(self.campaign)[0]["status"], "unresolved")

    def test_reservation_is_indexed_before_ready_exists(self) -> None:
        services = mock.Mock(fixture_only=True)
        index_path = self.attempt / "client-evidence/reservations" / (self.task["execution"]["producer_run_id"] + ".json")
        index_path.unlink()
        (self.capture / "ready.json").unlink()
        services.wait_capture_ready.side_effect = RuntimeError("真实 ready 尚未发布")
        with self.assertRaisesRegex(RuntimeError, "尚未发布"):
            control.run_client(self.task_path, self.attempt, services=services)
        self.assertTrue(index_path.is_file())
        services.close_capture.assert_called_once_with(self.task)
        services.recorder.assert_not_called()
        self.assertEqual(importer.collect_campaign_runs(self.campaign)[0]["status"], "unresolved")
        # 预约登记不授予成功权，完整消费继续严格要求真实 ready。
        with self.assertRaises(ValueError):
            source.verify_reservation_v2(self.task, self.capture)

    def test_registration_failure_still_closes_original_capture(self) -> None:
        services = mock.Mock(fixture_only=True)
        index_path = self.attempt / "client-evidence/reservations" / (self.task["execution"]["producer_run_id"] + ".json")
        index_path.unlink()
        adapter = c.SOURCE_ADAPTERS[self.task["execution"]["recorder_id"]]
        with mock.patch.object(adapter, "verify_capture_origin", side_effect=RuntimeError("预约来源暂不可核")):
            with self.assertRaisesRegex(RuntimeError, "暂不可核"):
                control.run_client(self.task_path, self.attempt, services=services)
        services.close_capture.assert_called_once_with(self.task)
        services.wait_capture_ready.assert_not_called()
        services.recorder.assert_not_called()
        self.assertFalse(index_path.exists())
        self.assertEqual(importer.collect_campaign_runs(self.campaign)[0]["status"], "unresolved")

    def test_close_failure_preserves_ready_error_and_discoverable_reservation(self) -> None:
        services = mock.Mock(fixture_only=True)
        services.wait_capture_ready.side_effect = RuntimeError("独立 ready 故障")
        services.close_capture.side_effect = OSError("关闭通道中断")
        with self.assertRaisesRegex(RuntimeError, "ready 故障") as caught:
            control.run_client(self.task_path, self.attempt, services=services)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        services.recorder.assert_not_called()
        self.assertEqual(importer.collect_campaign_runs(self.campaign)[0]["status"], "unresolved")

    def test_recorder_factory_failure_cannot_publish_preexisting_success_material(self) -> None:
        services = mock.Mock(fixture_only=True)
        services.recorder.side_effect = RuntimeError("录制后端缺失")
        with mock.patch.object(importer, "import_bundle") as publish:
            result = control.run_client(self.task_path, self.attempt, services=services)
        self.assertEqual(result["status"], "accounting_unresolved")
        services.close_capture.assert_called_once_with(self.task)
        publish.assert_not_called()
        self.assertTrue((self.bundle.parent / "client-run-failure.json").is_file())

    def test_current_reader_rejects_success_when_original_client_execution_failed(self) -> None:
        receipt = f.write_receipt(self.attempt, "claude-desktop", self.values)
        f.write(self.bundle.parent / "client-run-failure.json", {
            "schema_version": "claude-upgrade-client-run-failure/v2", "task_sha256": self.task["task_sha256"],
            "producer_run_id": self.task["execution"]["producer_run_id"], "error_type": "RuntimeError",
        })
        with self.assertRaisesRegex(ValueError, "原客户端执行失败"):
            c.verify_client_receipt(self.attempt, "claude-desktop", receipt)

    def test_measurement_distinguishes_directory_tree_and_host_binary(self) -> None:
        bundle = self.root / "fixture-app"
        bundle.mkdir()
        executable = bundle / "host"
        executable.write_bytes(b"fixture executable")
        app, host = recorder.measure_artifact(bundle), recorder.measure_artifact(executable)
        self.assertEqual((app["kind"], host["kind"]), ("directory", "file"))
        self.assertNotEqual(app["sha256"], host["sha256"])

    def test_invalid_model_payload_is_still_an_issued_billable_post(self) -> None:
        row = json.loads((self.capture / "requests.jsonl").read_bytes())
        row["request_body_base64"] = base64.b64encode(b"invalid request JSON").decode()
        f.write(self.capture / "requests.jsonl", row)
        self.capture_binding("requests", "requests.jsonl")
        verified = self.verify()
        self.assertEqual((verified["status"], verified["accounting_status"], len(verified["requests"])), ("failed", "resolved", 1))
        observed = source.collect_independent_requests_v2(self.task, self.capture)
        self.assertEqual([r["identity_key"] for r in observed["requests"]], [r["identity_key"] for r in verified["requests"]])

    def test_restore_cannot_reuse_before_binding_as_after(self) -> None:
        restoration = c.read_json(self.bundle / "state-restoration.json")
        restoration["after"] = restoration["before"]
        self.rewrite("state-restoration.json", restoration)
        with self.assertRaisesRegex(ValueError, "before 冒充 after"):
            self.verify()

    def test_product_exception_still_stops_restores_and_seals_failed_source(self) -> None:
        calls = []
        class Backend:
            fixture_only = True
            def prepare_state(self, task, root): calls.append("prepare")
            def start_monitoring(self, task, root): calls.append("monitor")
            def recheck_dispatch(self, task): calls.append("recheck")
            def launch_desktop(self, task, recipe):
                calls.append("launch")
                raise RuntimeError("模拟真实产品启动失败")
            def stop_product(self, task): calls.append("stop")
            def restore_state(self, task, root): calls.append("restore")
            def seal_sources(self, task, root, *, status): calls.append("seal:" + status)
        with self.assertRaisesRegex(RuntimeError, "启动失败"):
            recorder.record_product(self.task, self.bundle, recorder=Backend())
        self.assertEqual(calls, ["prepare", "monitor", "recheck", "launch", "stop", "restore", "seal:failed"])

    def test_independent_capture_drains_before_single_mac_seal(self) -> None:
        calls = []
        result = recorder.record_product(
            self.task, self.bundle, recorder=_SealBarrierBackend(calls),
            before_seal=lambda: calls.append("capture-close-sync"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(calls[-4:], ["stop", "restore", "capture-close-sync", "seal:complete"])
        self.assertEqual(calls.count("capture-close-sync"), 1)
        self.assertEqual(calls.count("seal:complete"), 1)

    def test_capture_close_failure_seals_only_truncated_material(self) -> None:
        calls = []
        def interrupted_close():
            calls.append("capture-close-sync")
            raise OSError("独立捕获关闭同步中断")
        with self.assertRaisesRegex(OSError, "关闭同步中断"):
            recorder.record_product(self.task, self.bundle, recorder=_SealBarrierBackend(calls),
                                    before_seal=interrupted_close)
        self.assertEqual(calls[-4:], ["stop", "restore", "capture-close-sync", "seal:truncated"])
        self.assertNotIn("seal:complete", calls)

    def test_original_product_failure_survives_close_and_seal_failures(self) -> None:
        calls = []
        original = RuntimeError("原产品启动失败")
        def interrupted_close():
            calls.append("capture-close-sync")
            raise OSError("关闭失败")
        backend = _SealBarrierBackend(calls, launch_error=original, seal_error=OSError("封存失败"))
        with self.assertRaises(RuntimeError) as caught:
            recorder.record_product(self.task, self.bundle, recorder=backend,
                                    before_seal=interrupted_close)
        self.assertIs(caught.exception, original)
        self.assertEqual(calls[-4:], ["stop", "restore", "capture-close-sync", "seal:truncated"])

    def test_client_run_close_barrier_failure_is_not_retried_or_imported(self) -> None:
        calls = []
        services = mock.Mock(fixture_only=True)
        services.recorder.return_value = _SealBarrierBackend(calls)
        def interrupted_close(task):
            calls.append("capture-close-sync")
            raise OSError("独立同步失败")
        services.close_capture.side_effect = interrupted_close
        with mock.patch.object(importer, "import_bundle") as publish:
            result = control.run_client(self.task_path, self.attempt, services=services)
        self.assertEqual(result["status"], "accounting_unresolved")
        services.close_capture.assert_called_once_with(self.task)
        publish.assert_not_called()
        self.assertEqual(calls[-4:], ["stop", "restore", "capture-close-sync", "seal:truncated"])
        self.assertTrue((self.bundle.parent / "client-run-failure.json").is_file())

    def test_auxiliary_oauth_native_request_remains_separate_from_model_budget(self) -> None:
        first = json.loads((self.capture / "requests.jsonl").read_bytes())
        oauth = {**first, "request_ordinal": 2, "host": "platform.claude.com", "path": "/v1/oauth/token", "request_body_base64": base64.b64encode(b"{}").decode()}
        (self.capture / "requests.jsonl").write_bytes(c.canonical(first)+b"\n"+c.canonical(oauth)+b"\n")
        self.capture_binding("requests", "requests.jsonl")
        call = json.loads((self.bundle / "socket-calls.jsonl").read_bytes())
        call["request_ordinals"] = [1, 2]
        self.proof(self.bundle, call)
        self.rewrite("socket-calls.jsonl", call)
        verified = self.verify()
        self.assertEqual((len(verified["requests"]), len(verified["auxiliary_requests"]), verified["status"]), (1, 1, "verified"))
        observed = source.collect_independent_requests_v2(self.task, self.capture)
        self.assertEqual([r["identity_key"] for r in verified["auxiliary_requests"]], [r["identity_key"] for r in observed["auxiliary_requests"]])


if __name__ == "__main__":
    unittest.main()
