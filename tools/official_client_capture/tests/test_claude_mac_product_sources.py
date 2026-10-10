"""真实来源工程的无账号专项：fixture 正例不代表安装或 formal 资格。

实际验签调用系统 openssl；原生编译另留凭证，不运行或申请 NE/ES/UI 权限。
重点核来源伪造、PID 复用、错闭连接、缺成功激活以及单次输入/子树回收。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_product_flows as flows
from tools.official_client_capture import claude_mac_product_logs as logs
from tools.official_client_capture import claude_mac_product_modules as modules
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_product_provider as provider
from tools.official_client_capture import claude_mac_product_recipe as recipe
from tools.official_client_capture import claude_mac_product_source as product_source
from tools.official_client_capture.tests import test_claude_mac_product_materials as material_fixture


def at(second: int) -> str:
    return f"2026-10-10T01:00:{second:02d}.000Z"


def process(pid: int, executable: str, parent: int = 99) -> dict:
    return {"pid": pid, "ppid": parent, "pidversion": 1, "boot_id": "fixture-boot",
        "start_seconds": 1791594000 + pid, "start_microseconds": 0, "executable": executable,
        "cdhash": "a"*40, "device": 1, "inode": pid, "size": 100,
        "audit_token": [0, os.geteuid(), 0, 0, 0, pid, 0, 1],
        "parent_audit_token": [0, os.geteuid(), 0, 0, 0, parent, 0, 1]}


def event(kind: str, instance: dict, second: int, sequence: int) -> dict:
    return {"kind": "process", "event": kind, "process": copy.deepcopy(instance), "observed_at_utc": at(second),
            "global_sequence": sequence, "event_sequence": sequence}


def file_frame(kind: str, path: Path, writer: dict, second: int) -> dict:
    info = path.stat()
    return {"kind": kind, "process": copy.deepcopy(writer), "path": str(path), "device": info.st_dev,
        "inode": info.st_ino, "size": info.st_size, "mtime_nanoseconds": info.st_mtime_ns,
        "ctime_nanoseconds": info.st_ctime_ns, "observed_at_utc": at(second), "global_sequence": 20+second,
        "event_sequence": 20+second, **({"content_sha256": hashlib.sha256(path.read_bytes()).hexdigest()} if kind == "product_file" else {})}


class ProductSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = material_fixture.ProductMaterialsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.run.chmod(0o700)
        self.roots = self.fixture.vscode()
        out = self.roots["application_root"] / "Contents/Resources/app/out"
        material_fixture.write(out / "cli.js", b'"user-data-dir";"extensions-dir";"inspect-extensions";"trace"')
        material_fixture.write(out / "vs/workbench/api/node/extensionHostProcess.js",
            b'ExtensionService#_doActivateExtension didActivateExtension/ extensionActivationTimes "success" "failure" activateResolvedTime')
        material_fixture.write(out / "vs/code/electron-utility/sharedProcess/sharedProcessMain.js", b"telemetry/ properties measurements")
        self.root, self.material = self.fixture.freeze("claude-vscode", self.roots)
        self.task = self.fixture.task(self.root, self.material)
        self.plan = recipe.derive_material_private_launch_plan(self.task, bundle_root=self.root,
            materials_receipt=self.root / "product-materials.json")
        self.artifacts = {row["role"]: row for row in self.task["client_artifacts"]}
        self.application = process(100, self.artifacts["application_executable"]["content"]["path"])
        self.host = process(101, self.artifacts["extension_host"]["content"]["path"], 100)
        self.cli = process(102, self.artifacts["embedded_cli"]["content"]["path"], 101)
        self.frames = [event("exec", self.application, 1, 1), event("exec", self.host, 2, 2),
            event("exec", self.cli, 3, 3), event("exit", self.cli, 10, 4), event("exit", self.host, 11, 5),
            event("exit", self.application, 12, 6)]
        self.context = flows._context(self.task)
        self.identifier = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def row(self, name: str, second: int, facts: dict, sequence: int) -> dict:
        return {"schema_version": "claude-mac-ne-flow-event/v1", "event": name, "sequence": sequence,
            "task_sha256": self.task["task_sha256"], "run_id": self.context["run_id"], "observed_at_utc": at(second), "facts": facts}

    def rows(self, *, closed=True) -> list[dict]:
        facts = {"flow_id": self.identifier, "source_process_audit_token": self.cli["audit_token"],
            "source_app_audit_token": self.application["audit_token"], "local_endpoint": None, "remote_endpoint": None,
            "socket_family": 2, "socket_type": 1, "socket_protocol": 6}
        result = [self.row("session_begin", 0, {"provider_class": "NEFilterDataProvider",
            "source_token_kind": "sourceProcessAuditToken", "close_report_kind": "NEFilterReportEventFlowClosed"}, 0),
            self.row("flow_open", 4, copy.deepcopy(facts), 1)]
        facts.update(local_endpoint={"host": "127.0.0.1", "port": 40000}, remote_endpoint={"host": "127.0.0.1", "port": 8080})
        result.append(self.row("flow_endpoints", 5, copy.deepcopy(facts), 2))
        if closed:
            result.append(self.row("flow_closed", 9, {**facts, "bytes_in": 100, "bytes_out": 80, "report_event": 3}, 3))
        result.append(self.row("session_end", 13, {"first_error": None, "open_flow_ids": [] if closed else [self.identifier]}, len(result)))
        return result

    def raw(self, rows=None) -> bytes:
        return b"".join(c.canonical(row)+b"\n" for row in (rows or self.rows()))

    def parse(self, rows=None, frames=None):
        return flows.parse_owned_flows(self.raw(rows), task=self.task, process_frames=frames or self.frames, context=self.context)

    def test_actual_process_token_endpoints_and_flow_closed_are_required(self):
        result = self.parse()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["process"], self.cli)
        self.assertEqual(result[0]["client_endpoint"], "127.0.0.1:40000")
        self.assertEqual(result[0]["bytes_out"], 80)
        self.assertNotIn("request_ordinals", result[0])
        for closed in (False,):
            with self.assertRaises(flows.ProductFlowError):
                self.parse(self.rows(closed=closed))

    def test_source_app_pid_cannot_substitute_actual_process_and_pidversion(self):
        rows = self.rows()
        for row in rows[1:-1]:
            row["facts"]["source_process_audit_token"] = [0, os.geteuid(), 0, 0, 0, 102, 0, 2]
        with self.assertRaises(flows.ProductFlowError):
            self.parse(rows)
        for row in rows[1:-1]:
            row["facts"]["source_process_audit_token"] = None
        with self.assertRaises(flows.ProductFlowError):
            self.parse(rows)

    def test_unrelated_known_application_is_not_promoted_to_product(self):
        rows = self.rows()
        other = process(200, "/unexecuted/other")
        for row in rows[1:-1]:
            row["facts"].update(source_process_audit_token=other["audit_token"], source_app_audit_token=other["audit_token"])
        self.assertEqual(self.parse(rows), [])

    def test_sequence_gap_duplicate_tail_endpoint_drift_and_data_complete_are_rejected(self):
        changes = [(2, "sequence", 90), (3, "event", "data_complete"), (4, "event", "session_begin")]
        for index, field, value in changes:
            with self.subTest(field=field):
                rows = self.rows(); rows[index][field] = value
                with self.assertRaises(flows.ProductFlowError): self.parse(rows)
        rows = self.rows(); rows[3]["facts"]["remote_endpoint"]["port"] = 8081
        with self.assertRaises(flows.ProductFlowError): self.parse(rows)

    def test_real_closed_report_counters_and_process_window_are_enforced(self):
        for field, value in (("report_event", 2), ("bytes_in", True), ("bytes_out", -1)):
            rows = self.rows(); rows[3]["facts"][field] = value
            with self.subTest(field=field), self.assertRaises(flows.ProductFlowError): self.parse(rows)
        rows = self.rows(); rows[3]["observed_at_utc"] = at(11)
        with self.assertRaises(flows.ProductFlowError): self.parse(rows)

    def test_fixture_origin_signature_is_cryptographically_verified_and_tamper_rejected(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        raw = self.raw()
        payload = c.canonical({"schema_version": "claude-mac-ne-flow-attestation/v1", "context": self.context,
            "record_count": len(self.rows()), "raw_bytes": len(raw), "stream_sha256": hashlib.sha256(raw).hexdigest(), "first_error": None})
        native._new_file(root / "payload", payload)
        subprocess.run(["/usr/bin/openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(root/"fixture-key")],
                       capture_output=True, check=True, timeout=10)
        pub = subprocess.run(["/usr/bin/openssl", "ec", "-in", str(root/"fixture-key"), "-pubout", "-outform", "DER"],
                             capture_output=True, check=True, timeout=10).stdout[-65:]
        subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign", str(root/"fixture-key"), "-out", str(root/"signature"), str(root/"payload")],
                       capture_output=True, check=True, timeout=10)
        signature = (root / "signature").read_bytes()
        envelope = {"payload_base64": base64.b64encode(payload).decode(), "signature_base64": base64.b64encode(signature).decode(),
                    "public_key_sha256": hashlib.sha256(pub).hexdigest()}
        self.assertEqual(flows.verify_flow_attestation(raw, envelope, pub, self.context)["context"], self.context)
        with self.assertRaises(flows.ProductFlowError): flows.verify_flow_attestation(raw+b" ", envelope, pub, self.context)
        corrupted = {**envelope, "signature_base64": base64.b64encode(signature[:-1]+bytes([signature[-1]^1])).decode()}
        with self.assertRaises(flows.ProductFlowError): flows.verify_flow_attestation(raw, corrupted, pub, self.context)
        self.assertEqual(c.SOURCE_ADAPTERS, {})

    def activation_frames(self, *, success=True):
        log_root = Path(self.plan["plan"]["private_roots"]["user_data"]) / "logs/fixture-run"
        host_log = log_root / "window1/exthost/exthost.log"
        telemetry_log = log_root / "telemetry.log"
        material_fixture.write(host_log, b"[2026-10-10 01:00:03.000] [info] ExtensionService#_doActivateExtension anthropic.claude-code, startup: false, activationEvent: 'fixture-only'\n")
        payload = {"properties": {"id": "anthropic.claude-code", "extensionVersion": self.artifacts["extension_package"]["version"]["value"],
                   "outcome": "success" if success else "failure"}, "measurements": {"pluginHostTelemetry": 1,
                   "codeLoadingTime": 1, "activateCallTime": 1, "activateResolvedTime": 1}}
        material_fixture.write(telemetry_log, b"[2026-10-10 01:00:05.000] [trace] telemetry/extensionActivationTimes "+c.canonical(payload)+b"\n")
        for path in (host_log, telemetry_log): path.chmod(0o600)
        entry = Path(self.artifacts["extension_entry"]["content"]["path"])
        return [*self.frames, file_frame("product_module_open", entry, self.host, 4),
            file_frame("product_file", host_log, self.host, 6), file_frame("product_file", telemetry_log, self.application, 6)]

    def activate(self, frames):
        return modules.read_vscode_activation(self.task, bundle_root=self.root, plan=self.plan["plan"],
            materials_receipt=self.root / "product-materials.json", native_frames=frames, log_utc_offset_seconds=0)

    def test_actual_entry_open_and_unique_host_and_success_log_must_all_be_present(self):
        frames = self.activation_frames()
        result = self.activate(frames)
        self.assertEqual(result["module_observation"]["host_process"], self.host)
        self.assertEqual(result["module_observation"]["activated_at_utc"], "2026-10-10T01:00:05+00:00")
        with self.assertRaises(modules.ProductModuleUnavailable): self.activate([row for row in frames if row["kind"] != "product_module_open"])
        with self.assertRaises(modules.ProductModuleUnavailable): self.activate(frames[:-1])

    def test_activation_started_or_failure_never_counts_as_success(self):
        with self.assertRaises(modules.ProductModuleError): self.activate(self.activation_frames(success=False))

    def test_activation_archive_survives_restore_but_rejects_copied_byte_tamper(self):
        frames = self.activation_frames()
        actual = self.activate(frames)
        for frame in frames:
            if frame["kind"] == "product_file":
                Path(frame["path"]).unlink()
        archived = modules.replay_vscode_activation(self.task, bundle_root=self.root, plan=self.plan["plan"],
            materials_receipt=self.root/"product-materials.json", native_frames=frames, log_utc_offset_seconds=0)
        self.assertEqual(archived["module_observation"], actual["module_observation"])
        copied = self.root/actual["raw_files"][0]["path"]
        copied.write_bytes(copied.read_bytes()+b"tamper")
        with self.assertRaises(modules.ProductModuleError):
            modules.replay_vscode_activation(self.task, bundle_root=self.root, plan=self.plan["plan"],
                materials_receipt=self.root/"product-materials.json", native_frames=frames, log_utc_offset_seconds=0)

    def test_exec_replacement_closes_old_execution_without_forging_exit(self):
        forked = process(110, "/fixture/forked", 100)
        executed = copy.deepcopy(forked)
        executed["pidversion"] += 1
        executed["audit_token"][7] += 1
        executed["executable"] = self.cli["executable"]
        replacement = event("exec", executed, 3, 3)
        replacement["previous_process"] = forked
        frames = [event("exec", self.application, 1, 1), event("fork", forked, 2, 2), replacement,
            event("exit", executed, 8, 4), event("exit", self.application, 9, 5)]
        histories = native.owned_process_lifecycles(frames, self.task)
        self.assertEqual(histories[tuple(forked["audit_token"])]["end_reason"], "exec_replaced")
        self.assertEqual(histories[tuple(executed["audit_token"])]["end_reason"], "exit")
        del replacement["previous_process"]
        with self.assertRaises(native.ProductNativeError): native.owned_process_lifecycles(frames, self.task)

    def test_activation_wrong_writer_duplicate_host_or_after_close_tamper_is_rejected(self):
        frames = self.activation_frames()
        frames[-2]["process"] = self.application
        with self.assertRaises(modules.ProductModuleError): self.activate(frames)
        frames = self.activation_frames()
        frames.append(event("exec", process(103, self.host["executable"], 100), 3, 30))
        with self.assertRaises(modules.ProductModuleUnavailable): self.activate(frames)
        frames = self.activation_frames()
        path = Path(frames[-1]["path"])
        path.write_bytes(path.read_bytes().replace(b"success", b"failure"))
        with self.assertRaises(modules.ProductModuleError): self.activate(frames)

    def test_logging_recipe_is_material_bound_and_does_not_authorize_formal_launch(self):
        self.assertEqual(self.plan["plan"]["argv"][-2:], ["--log", "trace"])
        self.assertFalse(self.plan["launch_allowed"])
        malformed = copy.deepcopy(self.plan["plan"]); malformed["argv"].extend(["--inspect-extensions", "9229"])
        with self.assertRaises(recipe.ProductRecipeError): recipe.validate_material_private_launch_plan(
            self.task, malformed, bundle_root=self.root, materials_receipt=self.root/"product-materials.json")

    def test_missing_network_installation_never_starts_or_registers_source(self):
        with mock.patch.object(flows.authority, "INSTALL_ROOT", self.root / "missing-installation"), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("零启动")), \
             self.assertRaises(flows.ProductFlowUnavailable):
            flows.load_network_installation()
        self.assertEqual(c.SOURCE_ADAPTERS, {})

    def test_scenario_waits_passively_without_second_ui_input(self):
        original = object.__new__(provider.DarwinProductObservationProvider)
        task = self.task
        original._task, original._root, original._plan = task, self.root, self.plan
        original._scenarios, original._completed, original._flows = set(), {}, None
        original._process, original._instance = SimpleNamespace(pid=100, poll=lambda: None), self.application
        original._authority = SimpleNamespace(executable=Path("/fixture/unexecuted"))
        monitor = SimpleNamespace(recheck=lambda: {}, _remaining=lambda: 10, records=self.frames)
        original._monitor = monitor
        original._guard = lambda *args, **kwargs: None
        original._reply = lambda phase, facts, **kwargs: {"phase": phase, "facts": facts}
        scenario = task["scenarios"][0]
        observed = {"scenario_id": scenario["id"], "session_events": [{"kind": "completed", "text": scenario["response_marker"]}], "raw_files": []}
        original._adapter = SimpleNamespace(observe_product_scenario=mock.Mock(side_effect=[logs.ProductLogUnavailable("未齐"), observed]))
        ui = SimpleNamespace(returncode=0, stderr=b"", stdout=c.canonical({"kind":"input_observed",
            "task_sha256": task["task_sha256"], "challenge":scenario["challenge"], "completion_observed":False}))
        (self.root / "provider-observations").mkdir(mode=0o700, exist_ok=True)
        with mock.patch.object(provider.subprocess, "run", return_value=ui) as dispatch, mock.patch.object(provider.time, "sleep"):
            self.assertEqual(original.drive(task, scenario)["phase"], "drive")
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(original._adapter.observe_product_scenario.call_count, 2)

    def test_owned_children_exit_before_parent_is_signalled(self):
        original = object.__new__(provider.DarwinProductObservationProvider)
        original._task, original._root, original._instance = self.task, self.root, self.application
        original._stopped_at, original._cleanup, original._flows = None, False, None
        original._closed_observations, original._module_references, original._module_observation = None, (), None
        original._log_offset_seconds = 0
        original._plan, original._material = self.plan, self.root / "product-materials.json"
        original._process = SimpleNamespace(pid=100, poll=lambda: None, wait=lambda **kwargs: 0)
        original._authority = SimpleNamespace(executable=Path("/fixture/unexecuted"))
        observed = [event("exec", self.application, 1, 1), event("exec", self.host, 2, 2)]
        original._monitor = SimpleNamespace(records=observed, begin_cleanup=lambda: None, recheck=lambda: {}, _remaining=lambda: 10, close=lambda: {"unclosed_instances": 0,"first_error": None}, origin={"kernel_event_chain_closed": True})
        original._guard = lambda *args, **kwargs: None
        original._reply = lambda phase, facts, **kwargs: {"phase":phase,"facts":facts}
        sent = []
        def stop(_argv, **kwargs):
            import json
            instance = json.loads(kwargs["input"])["process"]
            sent.append(instance["pid"])
            observed.append(event("exit", instance, 10+len(sent), 20+len(sent)))
            return SimpleNamespace(returncode=0, stderr=b"", stdout=c.canonical({"task_sha256":self.task["task_sha256"],
                "audit_token":instance["audit_token"],"returncode":0,"stopped":False}))
        (self.root/"provider-observations").mkdir(mode=0o700,exist_ok=True)
        with mock.patch.object(provider.subprocess,"run",side_effect=stop), mock.patch.object(provider.time,"sleep"):
            self.assertTrue(original.stop_owned(self.task)["facts"]["stopped"])
        self.assertEqual(sent,[101,100])

    def test_sealed_originals_block_capture_dispatch_refresh_before_dispatch_verification(self):
        for name in ("product-origin-attestation.json", "product-preseal.json", "source-manifest.json"):
            temporary = SimpleNamespace(_task=self.task, _optional=lambda path: b"fixture-only" if path == name else None)
            with self.subTest(name=name), mock.patch.object(product_source._MAC_SOURCE, "owns_recorder", return_value=True), \
                 mock.patch.object(product_source, "_dispatch", side_effect=AssertionError("封后不得核新派发")), self.assertRaises(ValueError):
                product_source.refresh_capture_dispatch(temporary, self.task, object())

    def test_dispatch_refresh_keeps_verified_original_handle_and_frozen_context(self):
        old_handle, new_handle = object(), object()
        old = {key: "fixture-only" for key in ("task_sha256", "ready", "budget_reservation", "capture_deployment", "mac_deployment", "deadline_utc")}
        services = SimpleNamespace(_dispatch=old_handle)
        temporary = SimpleNamespace(_task=self.task, _services=services, _optional=lambda path: None)
        with mock.patch.object(product_source._MAC_SOURCE, "owns_recorder", return_value=True), \
             mock.patch.object(product_source, "_dispatch", side_effect=[old, dict(old)]):
            product_source.refresh_capture_dispatch(temporary, self.task, new_handle)
        self.assertIs(services._dispatch, new_handle)
        services._dispatch = old_handle
        with mock.patch.object(product_source._MAC_SOURCE, "owns_recorder", return_value=True), \
             mock.patch.object(product_source, "_dispatch", side_effect=[old, {**old, "deadline_utc": "changed"}]), self.assertRaises(ValueError):
            product_source.refresh_capture_dispatch(temporary, self.task, new_handle)
        self.assertIs(services._dispatch, old_handle)

    def test_recovery_rehydrates_signed_exited_originals_without_product_or_source_writes(self):
        from tools.official_client_capture.tests.test_claude_mac_product_state import ProductStateTests
        signer = object.__new__(ProductStateTests)
        signer.root, signer.task = self.root, self.task
        signer.identity = {"producer_run_id": self.task["execution"]["producer_run_id"]}
        ready = {"kind": "ready", "task_sha256": self.task["task_sha256"], "run_id": signer.identity["producer_run_id"],
            "boot_id": "fixture-boot", "observed_at_utc": at(0), "accessibility_trusted": False,
            "socket_events_complete": False, "module_activation_verified": False}
        closed = {"kind": "closed", "task_sha256": self.task["task_sha256"], "run_id": signer.identity["producer_run_id"],
            "observed_at_utc": at(13), "close_requested": True, "unsubscribe_result": 0, "delete_result": 0,
            "unclosed_instances": 0, "first_error": None, "drain_status": "observed_unqualified",
            "socket_events_complete": False, "module_activation_verified": False}
        raw, key = signer.signature([ready, *self.frames, closed])
        native._new_file(self.root/"native-events.jsonl", raw)
        directory = self.root/"provider-observations"
        directory.mkdir(mode=0o700)
        cached = {"closed": False, "native_closed": closed, "flow_attestation": None,
            "module_observation": None, "unknown_reasons": ["fixture 尚缺独立模块来源"]}
        native._new_file(directory/"closed-originals.json", c.canonical(cached))
        original = object.__new__(provider.DarwinProductObservationProvider)
        original._task, original._root, original._plan = self.task, self.root, self.plan
        original._material = self.root/"product-materials.json"
        original._authority = SimpleNamespace(origin_public_key=lambda deployment: key)
        original._monitor = SimpleNamespace(records=[], origin=None)
        original._process, original._instance = None, None
        original._guard = lambda *args, **kwargs: None
        reply = {"phase": "recovery_owned", "task_sha256": self.task["task_sha256"], "completeness": {"status": "complete"},
            "facts": {"process_instance": self.application, "stopped": True, "stopped_at_utc": at(12)}}
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        run = subprocess.run
        def only_verify(argv, *args, **kwargs):
            self.assertEqual(argv[0], "/usr/bin/openssl", "恢复不得启动产品 helper")
            return run(argv, *args, **kwargs)
        with mock.patch.object(provider.subprocess, "run", side_effect=only_verify), \
             mock.patch.object(original, "_reply", side_effect=AssertionError("纯读重建不得写新返回")):
            original.rehydrate_recovery(self.task, reply)
            original.rehydrate_recovery(self.task, reply)
        self.assertEqual(original._instance, self.application)
        self.assertEqual(original._stopped_at, at(12))
        self.assertEqual(original._monitor.records, [ready, *self.frames, closed])
        self.assertTrue(original._cleanup)
        self.assertEqual(before, {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        corrupted = copy.deepcopy(reply)
        corrupted["facts"]["process_instance"]["pidversion"] += 1
        with self.assertRaises(ValueError): original.rehydrate_recovery(self.task, corrupted)


if __name__ == "__main__":
    unittest.main()
