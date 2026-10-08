"""原生投影离线专项：真实私有 FS、严格负例、拒绝无授权启动。

所有事件帧明确是 regression_fixture，不调用 ES、产品、网络或信号。
编译单独保留真实物料收据，不在单元测试中生成内核完整窗口结论。
"""

from __future__ import annotations

import copy
import hashlib
import os
import struct
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from tools.official_client_capture import claude_mac_process_events as source
from tools.official_client_capture import claude_mac_process_events_contract as contract
from tools.official_client_capture import claude_mac_process_events_build as build


def material(path: Path, raw: bytes) -> dict:
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def process(pid: int, version: int = 1, parent: int | None = None) -> dict:
    return {"boot_session_uuid": "11111111-2222-3333-4444-555555555555", "pid": pid, "pidversion": version,
        "fork_start_time": {"seconds": 1000 + pid, "microseconds": 12},
        "parent_audit_identity": {"pid": parent, "pidversion": 1} if parent else None,
        "executable": {"path": "/fixture/probe", "path_truncated": False, "device": 1, "inode": pid,
            "size": 123, "mtime": {"seconds": 900, "nanoseconds": 1}, "cdhash": "a" * 40,
            "codesigning_flags": 0, "signing_id": "", "team_id": ""}}


class NativeProcessEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-native-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.context = contract.NativeContext("a" * 64, "nonce", "producer", "collector", "deployment", "b" * 64,
            "c" * 64, "internal_native_probe", "fork-exec-exit-v1",
            contract.NativeLimits(16384, 1048576, 128, 16, 16, "2026-10-08T00:00:00Z"))
        self.frames = self._frames()

    def _base(self, kind: str, payload: dict, **values) -> dict:
        return {"schema_version": contract.SCHEMA, "record_type": kind, "collector_run_id": self.context.collector_run_id,
            "context_sha256": contract.context_digest(self.context), "message_version": None, "global_seq_num": None,
            "event_type": None, "event_seq_num": None, "kernel_mach_time": None, "kernel_time": None,
            "payload": payload, **values}

    def _write(self, path: Path, raw: bytes) -> dict:
        with path.open("xb") as stream:
            stream.write(raw)
        path.chmod(0o600)
        return material(path, raw)

    def _frames(self) -> list[dict]:
        instance = {"schema_version": "claude-mac-native-collector-instance/internal-v1",
            "collector_run_id": self.context.collector_run_id, "context_sha256": contract.context_digest(self.context),
            "origin": "regression_fixture", "process_events_complete": False, "executed_image_verified": False,
            "formal_ready": False, "launch_allowed": False, "registration_verified": False}
        self.instance = self._write(self.root / "instance.json", contract.canonical_json(instance) + b"\n")
        frames, per_type = [], {}

        def event(name, payload, kind="process_event"):
            seq = per_type.get(name, {"notify_fork": 20, "notify_exec": 30, "notify_exit": 40}[name]) + 1
            per_type[name] = seq
            frames.append(self._base(kind, payload, message_version=4, global_seq_num=100 + sum(
                f["record_type"] in {"process_event", "counter_only"} for f in frames), event_type=name,
                event_seq_num=seq, kernel_mach_time=123, kernel_time={"seconds": 2000, "nanoseconds": 1}))

        for name in contract.EVENT_TYPES:
            event(name, {}, "counter_only")
        frames.append(self._base("barrier", {"phase": "ready", "type_watermarks": {"fork": 21, "exec": 31, "exit": 41},
            "collector_instance": self.instance, "event_prefix": material(self.root / "events.jsonl", b"")}, global_seq_num=102))
        event("notify_fork", {"actor": process(100), "target": process(101, parent=100), "exit_stat": None})
        event("notify_exec", {"actor": process(101, parent=100), "target": process(101, 2, 100), "exit_stat": None})
        event("notify_exec", {"actor": process(101, 2, 100), "target": process(101, 3, 100), "exit_stat": None})
        event("notify_exit", {"actor": process(101, 3, 100), "target": None, "exit_stat": 0})
        event("notify_exit", {"actor": process(100), "target": None, "exit_stat": 0})
        watermarks = {"fork": 22, "exec": 33, "exit": 43}
        frames.append(self._base("barrier", {"phase": "closing", "type_watermarks": watermarks,
            "collector_instance": self.instance, "event_prefix": material(self.root / "events.jsonl", b"")}, global_seq_num=107))
        frames.append(self._base("closed", {"type_watermarks": watermarks, "unclosed_instances": 0,
            "drain_status": "verified", "event_material": material(self.root / "events.jsonl", b"")}, global_seq_num=107))
        return frames

    def _render(self, frames: list[dict] | None = None, refresh: bool = True) -> bytes:
        raw = b""
        for original in self.frames if frames is None else frames:
            value = copy.deepcopy(original)
            if refresh and value["record_type"] in {"barrier", "closed"}:
                field = "event_prefix" if value["record_type"] == "barrier" else "event_material"
                value["payload"][field] = material(self.root / "events.jsonl", raw)
            raw += contract.canonical_json(value) + b"\n"
        return raw

    def _bundle(self) -> contract.MaterialBinding:
        raw = self._render()
        frames = contract.validate_native_projection(self.context, raw)
        ready = next(f for f in frames if f["record_type"] == "barrier" and f["payload"]["phase"] == "ready")
        closed = frames[-1]
        files = {"collector_instance": self.instance,
            "projection": self._write(self.root / "events.jsonl", raw),
            "ready": self._write(self.root / "ready.json", contract.canonical_json(ready) + b"\n"),
            "closed": self._write(self.root / "closed.json", contract.canonical_json(closed) + b"\n")}
        manifest = {"schema_version": contract.CAPTURE_SCHEMA, "context": asdict(self.context),
            "context_sha256": contract.context_digest(self.context), "build_identity_sha256": self.context.build_identity_sha256,
            "origin": "regression_fixture", "files": files, "process_events_complete": False,
            "executed_image_verified": False, "formal_ready": False, "launch_allowed": False, "registration_verified": False}
        return contract.MaterialBinding(**self._write(self.root / "manifest.json", contract.canonical_json(manifest) + b"\n"))

    def _reject(self, mutate) -> None:
        frames = copy.deepcopy(self.frames)
        mutate(frames)
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.validate_native_projection(self.context, self._render(frames))

    def test_valid_multiexec_window_is_only_frozen_structure(self) -> None:
        frames = contract.validate_native_projection(self.context, self._render())
        self.assertEqual(len(frames), 11)
        with self.assertRaises(TypeError):
            frames[4]["payload"]["actor"]["pid"] = 999

    def test_real_fs_replay_keeps_all_authority_false(self) -> None:
        receipt = self._bundle()
        result = source.replay_native_capture(self.context, self.root, receipt)
        self.assertEqual((result.status, result.origin), ("structurally_valid_fixture", "regression_fixture"))
        for name in ("process_events_complete", "executed_image_verified", "formal_ready", "launch_allowed", "registration_verified"):
            self.assertIs(getattr(result, name), False)

    def test_global_gap_before_filtering_is_rejected(self) -> None:
        self._reject(lambda f: f[1].update(global_seq_num=104))

    def test_type_sequence_gap_is_rejected(self) -> None:
        self._reject(lambda f: f[5].update(event_seq_num=99))

    def test_message_versions_are_bounded(self) -> None:
        for value in (3, 9, True):
            with self.subTest(version=value):
                self._reject(lambda f: f[0].update(message_version=value))

    def test_unknown_record_and_nested_fields_are_rejected(self) -> None:
        for mutate in (lambda f: f[0].update(record_type="sample"), lambda f: f[0].update(allowed=True),
                       lambda f: f[4]["payload"]["actor"]["executable"].update(argv=[])):
            self._reject(mutate)

    def test_outside_scope_counter_does_not_hold_paths(self) -> None:
        self._reject(lambda f: f[0]["payload"].update(path="/private/other"))

    def test_boot_change_or_missing_is_rejected(self) -> None:
        for value in (None, "nonce", "00000000-0000-0000-0000-000000000000", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
            with self.subTest(boot=value):
                self._reject(lambda f: f[5]["payload"]["target"].update(boot_session_uuid=value))

    def test_truncated_path_and_boolean_pid_are_rejected(self) -> None:
        self._reject(lambda f: f[4]["payload"]["actor"]["executable"].update(path_truncated=True))
        self._reject(lambda f: f[4]["payload"]["actor"].update(pid=True))

    def test_fork_parent_is_exact_execution_identity(self) -> None:
        self._reject(lambda f: f[4]["payload"]["target"]["parent_audit_identity"].update(pidversion=2))

    def test_exec_generation_and_exit_are_not_guessed(self) -> None:
        self._reject(lambda f: f[6]["payload"]["target"].update(pidversion=2))
        self._reject(lambda f: f[7]["payload"]["actor"].update(pidversion=2))
        self._reject(lambda f: f[4]["payload"]["target"].update(pid=100))

    def test_closing_requires_all_instances_naturally_exited(self) -> None:
        self._reject(lambda f: f[8].update(record_type="counter_only", payload={}))

    def test_closed_cannot_replace_closing_barrier(self) -> None:
        self._reject(lambda f: f.pop(9))

    def test_incomplete_cannot_be_cleared_by_successful_closed(self) -> None:
        self._reject(lambda f: f.insert(9, self._base("incomplete", {"reason": "queue_overflow", "first_error_sequence": 107})))

    def test_retained_incomplete_window_stays_incomplete(self) -> None:
        frames = copy.deepcopy(self.frames)
        frames.insert(9, self._base("incomplete", {"reason": "queue_overflow", "first_error_sequence": 107}))
        frames[-1]["payload"]["drain_status"] = "unknown"
        result = contract.validate_native_projection(self.context, self._render(frames))
        self.assertEqual(result[9]["payload"]["reason"], "queue_overflow")
        self.assertEqual(result[-1]["payload"]["drain_status"], "unknown")

    def test_after_closing_probe_events_are_rejected(self) -> None:
        def mutate(frames):
            frame = copy.deepcopy(frames[4])
            frame.update(global_seq_num=108, event_seq_num=23)
            frames.insert(10, frame)
        self._reject(mutate)

    def test_bounds_and_context_never_accept_permission_or_boolean_overrides(self) -> None:
        with self.assertRaises(contract.NativeProcessSourceError):
            replace(self.context.limits, max_queue_records=True)
        with self.assertRaises(contract.NativeProcessSourceError):
            replace(self.context, source_scope="real_product")
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.validate_native_projection(self.context, self._render() * 1024)

    def test_bad_prefix_binding_is_rejected(self) -> None:
        frames = copy.deepcopy(self.frames)
        frames[3]["payload"]["event_prefix"]["sha256"] = "0" * 64
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.validate_native_projection(self.context, self._render(frames, refresh=False))

    def test_half_record_duplicate_keys_and_noncanonical_json_rejected(self) -> None:
        raw = self._render()
        for invalid in (raw[:-1], raw[:-11], raw.replace(b'"message_version":4', b'"message_version":4,"message_version":4', 1), raw.replace(b'{', b'{ ', 1)):
            with self.subTest(size=len(invalid)):
                with self.assertRaises(contract.NativeProcessSourceError):
                    contract.validate_native_projection(self.context, invalid)

    def test_record_and_byte_limits_reject_without_writing(self) -> None:
        raw = self._render()
        for limits in (replace(self.context.limits, max_records=2), replace(self.context.limits, max_record_bytes=16)):
            with self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_native_projection(replace(self.context, limits=limits), raw)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"instance.json"})

    def test_diagnostic_never_substitutes_kernel_zero_or_time(self) -> None:
        frame = self._base("diagnostic", {"native_result": "native_entry_disabled", "missing_prerequisites": ["boot_representation_unqualified"]})
        self.assertEqual(len(contract.validate_native_projection(self.context, contract.canonical_json(frame) + b"\n")), 1)
        frame["global_seq_num"] = 0
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.validate_native_projection(self.context, contract.canonical_json(frame) + b"\n")

    def test_reader_rejects_symlink_hardlink_permission_and_extra_files(self) -> None:
        receipt = self._bundle()
        event_path = self.root / "events.jsonl"
        event_path.chmod(0o644)
        with self.assertRaises(contract.NativeProcessSourceError):
            source.replay_native_capture(self.context, self.root, receipt)
        event_path.chmod(0o600)
        os.link(event_path, self.root / "second-link")
        with self.assertRaises(contract.NativeProcessSourceError):
            source.read_bound(contract.MaterialBinding(**material(event_path, event_path.read_bytes())), private=True)
        (self.root / "second-link").unlink()
        (self.root / "alias").symlink_to(event_path)
        with self.assertRaises(contract.NativeProcessSourceError):
            source.read_bound(contract.MaterialBinding(**material(self.root / "alias", event_path.read_bytes())))
        with self.assertRaises(contract.NativeProcessSourceError):
            source.replay_native_capture(self.context, self.root, receipt)

    def test_reader_rejects_tamper_and_different_context(self) -> None:
        receipt = self._bundle()
        with self.assertRaises(contract.NativeProcessSourceError):
            source.replay_native_capture(replace(self.context, task_nonce="other"), self.root, receipt)
        (self.root / "ready.json").write_bytes(b"{}\n")
        with self.assertRaises(contract.NativeProcessSourceError):
            source.replay_native_capture(self.context, self.root, receipt)

    def test_manifest_cannot_promote_generated_frames(self) -> None:
        receipt = self._bundle()
        path = Path(receipt.path)
        original = contract.strict_json(path.read_bytes())
        for key, value in (("origin", "actual_native_owned_probe"), ("process_events_complete", True), ("launch_allowed", True)):
            manifest = copy.deepcopy(original)
            manifest[key] = value
            raw = contract.canonical_json(manifest) + b"\n"
            path.write_bytes(raw)
            with self.assertRaises(contract.NativeProcessSourceError):
                source.replay_native_capture(self.context, self.root, contract.MaterialBinding(**material(path, raw)))

    def test_live_entry_refuses_before_any_file_or_process(self) -> None:
        binding = contract.MaterialBinding(**self.instance)
        identity = contract.NativeBuildIdentity(binding, "a" * 64, "b" * 64, binding, 0o755, "arm64", binding,
            0o755, "c" * 64, "d" * 64, "e" * 64, "unsigned")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        target = self.root / "not-created"
        for operation in (lambda: source.inspect_native_readiness(identity, diagnostic_root=target),
                          lambda: source.NativeProcessEventCollector.open_owned_probe(self.context, identity=identity, bundle_root=target),
                          source.NativeProcessEventCollector):
            with self.assertRaises(contract.NativeProcessSourceUnavailable):
                operation()
        self.assertFalse(target.exists())
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_unqualified_identity_cannot_accept_external_signing_flag(self) -> None:
        binding = contract.MaterialBinding(**self.instance)
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.NativeBuildIdentity(binding, "a" * 64, "b" * 64, binding, 0o755, "arm64", binding,
                0o755, "c" * 64, "d" * 64, "e" * 64, "qualified_es_external")

    def test_build_target_and_macho_are_measured_not_self_reported(self) -> None:
        raw = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0) + struct.pack("<6I", 0x32, 24, 1, 0xF0000, 0xF0500, 0)
        self.assertFalse(source._macho_signature(raw))
        for invalid in (raw[:20], raw[:4] + struct.pack("<I", 0x01000007) + raw[8:], raw[:44] + struct.pack("<I", 0xE0000) + raw[48:]):
            with self.assertRaises(contract.NativeProcessSourceError):
                source._macho_signature(invalid)

    def test_build_path_refuses_existing_or_nonprivate_root(self) -> None:
        for target in (self.root, Path("/opt/claude-native-forbidden")):
            with self.assertRaises(contract.NativeProcessSourceError):
                build.build_native(target)


if __name__ == "__main__":
    unittest.main()
