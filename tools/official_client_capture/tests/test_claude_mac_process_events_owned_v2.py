"""owned-v2 离线原件、真实私有文件/pipe 与包装协议专项。

本文件不执行 collector/probe_fixture，不创建 ES，不运行产品或访问账号网络。
生成帧和同进程线程替身始终是回归夹具，不能证明实际内核或正式来源。
"""

from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools.official_client_capture import claude_mac_process_events as runtime
from tools.official_client_capture import claude_mac_process_events_contract as contract


BOOT = "11111111-2222-3333-4444-555555555555"


@unittest.skipUnless(sys.platform == "darwin", "只核 Darwin 自进程管道元数据")
class DarwinPipeMetadataTests(unittest.TestCase):
    """实测 SDK 管道字段语义；不执行 collector，也不宣称已验收其运行拒绝行为。"""

    def test_same_endpoint_opposite_endpoint_distinct_pipe_and_closed_peer(self) -> None:
        class FileInfo(ctypes.Structure):
            _fields_ = [("openflags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                ("offset", ctypes.c_int64), ("type", ctypes.c_int32), ("guardflags", ctypes.c_uint32)]

        class VInfo(ctypes.Structure):
            _fields_ = [("dev", ctypes.c_uint32), ("mode", ctypes.c_uint16), ("nlink", ctypes.c_uint16),
                ("ino", ctypes.c_uint64), ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32)] + [
                (name, ctypes.c_int64) for name in ("atime", "atimensec", "mtime", "mtimensec", "ctime",
                    "ctimensec", "birthtime", "birthtimensec", "size", "blocks")] + [
                ("blksize", ctypes.c_int32), ("flags", ctypes.c_uint32), ("gen", ctypes.c_uint32),
                ("rdev", ctypes.c_uint32), ("qspare", ctypes.c_int64 * 2)]

        class PipeInfo(ctypes.Structure):
            _fields_ = [("stat", VInfo), ("handle", ctypes.c_uint64), ("peerhandle", ctypes.c_uint64),
                ("status", ctypes.c_int32), ("reserved", ctypes.c_int32)]

        class PipeFDInfo(ctypes.Structure):
            _fields_ = [("fileinfo", FileInfo), ("pipeinfo", PipeInfo)]

        query = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidfdinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        self.assertEqual(ctypes.sizeof(PipeFDInfo), 184)

        def handles(fd: int) -> tuple[int, int]:
            value = PipeFDInfo()
            count = query(os.getpid(), fd, 6, ctypes.byref(value), ctypes.sizeof(value))
            self.assertEqual(count, ctypes.sizeof(value))
            self.assertEqual(value.fileinfo.type, 6)
            self.assertNotEqual(value.pipeinfo.handle, 0)
            return value.pipeinfo.handle, value.pipeinfo.peerhandle

        first, second, third = os.pipe(), os.pipe(), os.pipe()
        duplicate = os.dup(first[0])
        fds = {*first, *second, *third, duplicate}
        try:
            read_handles, write_handles = handles(first[0]), handles(first[1])
            self.assertNotEqual(os.fstat(first[0]).st_ino, os.fstat(first[1]).st_ino)
            self.assertEqual(handles(duplicate), read_handles)
            self.assertEqual(write_handles, tuple(reversed(read_handles)))
            self.assertTrue(set(read_handles).isdisjoint(handles(second[1])))
            self.assertTrue(set(read_handles).isdisjoint(handles(third[0])))
            self.assertTrue(set(handles(second[1])).isdisjoint(handles(third[0])))
            os.close(first[1])
            fds.remove(first[1])
            self.assertEqual(handles(first[0]), (read_handles[0], 0))
            self.assertEqual(handles(duplicate), (read_handles[0], 0))
            failed = PipeFDInfo()
            self.assertNotEqual(query(os.getpid(), -1, 6, ctypes.byref(failed), ctypes.sizeof(failed)), ctypes.sizeof(failed))
        finally:
            for fd in fds:
                os.close(fd)


def boot_observation(boot: str = BOOT) -> dict:
    raw = boot.encode("ascii") + b"\0"
    return {"attempts": [{"returncode": 0, "errno": 0, "returned_bytes": len(raw),
        "raw_base64": base64.b64encode(raw).decode("ascii")}],
        "representation": "canonical_uuid_string", "boot_session_uuid": boot}


def mute_observation() -> dict:
    table = {"returncode": 0, "reported_count": 0, "examined_count": 0,
        "table_sha256": hashlib.sha256(b"[]").hexdigest(), "relevant_match_count": 0, "status": "observed_unqualified"}
    return {"processes": dict(table), "paths": dict(table), "inversion": {"process": 1, "path": 1, "target_path": 1},
        "query_status": "observed_unqualified"}


class ProjectionFixture:
    """独立合成固定树，前缀和序号取实际已生成字节，不假装内核来源。"""

    def __init__(self, plan: contract.OwnedProbePlan) -> None:
        self.plan = plan
        self.frames, self.lines, self.water = [], [], {}
        self.sequence = 40
        self.collector = self.process(100, "helper", parent=50)
        self.instance = {"collector_process": self.collector, "helper": json.loads(contract.canonical_json(plan.document["helper"])),
            "probe_fixture": json.loads(contract.canonical_json(plan.document["probe_fixture"])), "boot_observation": boot_observation()}

    @property
    def raw(self) -> bytes:
        return b"".join(self.lines)

    def process(self, pid: int, role: str, *, parent: int) -> dict:
        path = Path(self.plan.document[role]["material"]["path"])
        info = path.stat()
        return {"boot_session_uuid": BOOT, "pid": pid, "pidversion": pid + 10,
            "fork_start_time": {"seconds": 1700000000 + pid, "microseconds": 0},
            "parent_audit_identity": {"pid": parent, "pidversion": parent + 10},
            "executable": {"path": str(path), "path_truncated": False, "device": info.st_dev, "inode": info.st_ino,
                "size": info.st_size, "mtime": {"seconds": 1700000000, "nanoseconds": 0}, "cdhash": "0" * 40,
                "codesigning_flags": 0, "signing_id": "", "team_id": ""}}

    def add(self, kind: str, payload: dict, event: str | None = None) -> dict:
        frame = {"schema_version": contract.OWNED_FRAME_SCHEMA, "record_type": kind,
            "collector_run_id": self.plan.document["context"]["collector_run_id"],
            "context_sha256": self.plan.document["context_sha256"], "plan_sha256": self.plan.document["plan_sha256"],
            "message_version": None, "global_seq_num": None, "event_type": None, "event_seq_num": None,
            "kernel_mach_time": None, "kernel_time": None, "payload": payload}
        if event:
            self.sequence += 1
            self.water[event] = self.water.get(event, 7) + 1
            frame.update(message_version=4, global_seq_num=self.sequence, event_type="notify_" + event,
                event_seq_num=self.water[event], kernel_mach_time=self.sequence * 1000,
                kernel_time={"seconds": 1700000000, "nanoseconds": self.sequence})
        self.frames.append(frame)
        self.lines.append(contract.canonical_json(frame) + b"\n")
        return frame

    def barrier(self, phase: str) -> None:
        self.add("barrier", {"phase": phase, "type_watermarks": {name: self.water.get(name) for name in ("fork", "exec", "exit")},
            "collector_instance_sha256": contract.fingerprint(self.instance), "event_prefix": {
                "sha256": hashlib.sha256(self.raw).hexdigest(), "bytes": len(self.raw), "frame_count": len(self.frames)}})

    def child(self, pid: int, *, parent: dict | None = None, multiple: bool = False) -> None:
        parent = parent or self.collector
        inherited = self.process(pid, "helper" if parent == self.collector else "probe_fixture", parent=parent["pid"])
        executed = self.process(pid, "probe_fixture", parent=parent["pid"])
        self.add("process_event", {"actor": parent, "target": inherited, "exit_stat": None}, "fork")
        self.add("process_event", {"actor": inherited, "target": executed, "exit_stat": None}, "exec")
        if multiple:
            self.add("process_event", {"actor": executed, "target": executed, "exit_stat": None}, "exec")
        self.add("process_event", {"actor": executed, "target": None, "exit_stat": 0}, "exit")

    def ready(self) -> None:
        self.add("state_observation", {"phase": "before", "boot_observation": boot_observation(), "mute_observation": mute_observation(),
            "create_result": 0, "subscribe_result": 0})
        self.add("collector_instance", self.instance)
        self.child(200)
        self.barrier("ready")

    def finish(self, *, reason: str | None = None, delete_result: int = 0, boot: str = BOOT) -> None:
        if not reason:
            self.child(201)
            self.barrier("closing")
            self.add("state_observation", {"phase": "after", "boot_observation": boot_observation(boot),
                "mute_observation": mute_observation(), "create_result": 0, "subscribe_result": 0})
        if reason:
            self.add("incomplete", {"reason": reason, "first_error_sequence": self.sequence})
        self.add("closed", {"type_watermarks": {name: self.water.get(name) for name in ("fork", "exec", "exit")},
            "unclosed_instances": 0, "drain_status": "failed" if reason else "observed_unqualified",
            "event_prefix": {"sha256": hashlib.sha256(self.raw).hexdigest(), "bytes": len(self.raw), "frame_count": len(self.frames)},
            "unsubscribe_result": 0, "delete_result": delete_result})


class PipePeer:
    """Popen 的同进程 pipe 替身；没有启动任何新进程或 Mach-O。"""

    def __init__(self, plan: contract.OwnedProbePlan, argv: list[str], *, malformed_ready: bool = False) -> None:
        self.plan, self.commands, self.errors = plan, [], []
        self.context = os.dup(int(argv[argv.index("--context-fd") + 1]))
        self.events = os.dup(int(argv[argv.index("--events-fd") + 1]))
        self.control = os.dup(int(argv[argv.index("--control-fd") + 1])) if "--control-fd" in argv else None
        self.diagnose = "--probe-v2" in argv
        self.malformed_ready = malformed_ready
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def poll(self) -> int | None:
        return None if self.thread.is_alive() else 78 if self.diagnose else 0

    def run(self) -> None:
        try:
            with os.fdopen(self.context, "rb", buffering=0) as context:
                request = contract.strict_json(context.readline(65536))
            self.request = request
            fixture = ProjectionFixture(self.plan)
            if self.diagnose:
                fixture.add("diagnostic", {"native_result": "es_not_entitled", "create_result": 3, "delete_result": None,
                    "boot_observation": boot_observation(), "missing_prerequisites": sorted(contract.OWNED_MISSING)})
                os.write(self.events, fixture.raw)
                return
            fixture.ready()
            raw = fixture.raw
            if self.malformed_ready:
                frames = copy.deepcopy(fixture.frames)
                frames[-1]["context_sha256"] = "f" * 64
                raw = b"".join(contract.canonical_json(frame) + b"\n" for frame in frames)
            os.write(self.events, raw)
            with os.fdopen(self.control, "rb", buffering=0) as control:
                while True:
                    line = control.readline(65536)
                    if not line:
                        return
                    command = contract.strict_json(line)
                    self.commands.append(command)
                    if command["operation"] == "run_owned_probe":
                        at = len(fixture.raw)
                        fixture.child(203, multiple=True)
                        os.write(self.events, fixture.raw[at:])
                    elif command["operation"] == "close":
                        at = len(fixture.raw)
                        fixture.finish()
                        os.write(self.events, fixture.raw[at:])
                        return
        except BaseException as error:
            self.errors.append(error)
        finally:
            os.close(self.events)


class OwnedV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-owned-v2-offline-", dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.build, self.bundle = self.root / "build", self.root / "bundle"
        self.build.mkdir(mode=0o700)
        self.bundle.mkdir(mode=0o700)
        raw = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
        raw += struct.pack("<6I", 0x32, 24, 1, 0x000F0000, 0x000F0500, 0)
        for name in ("collector", "probe_fixture"):
            path = self.build / name
            path.write_bytes(raw)
            path.chmod(0o755)
        receipt = {name: [] for name in runtime.BUILD_FIELDS}
        receipt.update(schema_version=contract.BUILD_SCHEMA, signing={"performed": False, "qualification": "unqualified_build_only"})
        sources = []
        for name in ("claude_mac_process_events_native.m", "claude_mac_process_events_contract.py", "claude_mac_process_events.py", "claude_mac_process_events_build.py"):
            path = Path(runtime.__file__).with_name(name)
            source = path.read_bytes()
            sources.append({"path": str(path), "sha256": hashlib.sha256(source).hexdigest(), "bytes": len(source)})
        receipt["source_files"] = sources
        receipt_path = self.build / "build-receipt.json"
        receipt_path.write_bytes(contract.canonical_json(receipt) + b"\n")
        receipt_path.chmod(0o600)
        def binding(path: Path) -> contract.MaterialBinding:
            data = path.read_bytes()
            return contract.MaterialBinding(str(path), hashlib.sha256(data).hexdigest(), len(data))
        self.identity = contract.NativeBuildIdentity(binding(receipt_path), contract.fingerprint(sources),
            contract.fingerprint(contract.PROJECTION_DESCRIPTOR), binding(self.build / "collector"), 0o755, "arm64",
            binding(self.build / "probe_fixture"), 0o755, "b" * 64, "c" * 64, "d" * 64, "unsigned")
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
        self.context = contract.NativeContext("e" * 64, "nonce-1", "producer-1", "collector-1", "deployment-1", "f" * 64,
            contract.fingerprint(asdict(self.identity)), "internal_native_probe", "fork-exec-exit-v1",
            contract.NativeLimits(65536, 67108864, 65536, 4096, 256, deadline))
        with patch.object(runtime, "verify_native_build", return_value=self.identity):
            self.plan = runtime.create_owned_probe_plan(self.context, identity=self.identity, bundle_root=self.bundle)

    def fixture(self, *, finish: bool = True) -> ProjectionFixture:
        fixture = ProjectionFixture(self.plan)
        fixture.ready()
        if finish:
            fixture.finish()
        return fixture

    def replay_bundle(self, fixture: ProjectionFixture) -> contract.MaterialBinding:
        files = {role: None for role in contract.OWNED_FILE_ROLES}
        raw = {"plan": contract.canonical_json(self.plan.document) + b"\n",
            "context": contract.canonical_json(self.plan.document["context"]) + b"\n",
            "build_receipt": Path(self.identity.build_receipt.path).read_bytes(), "projection": fixture.raw,
            **runtime._owned_snapshots(contract.validate_owned_probe_projection(self.plan, fixture.raw))}
        for role, data in raw.items():
            files[role] = runtime._owned_write_once(self.bundle, role + ".json", data)
        result = runtime._owned_summary(self.plan, contract.validate_owned_probe_projection(self.plan, fixture.raw), files, "regression_fixture")
        manifest = {"schema_version": contract.OWNED_CAPTURE_SCHEMA,
            **{name: self.plan.document[name] for name in ("plan_sha256", "context_sha256", "build_identity_sha256")},
            "collector_run_id": self.context.collector_run_id, "origin": "regression_fixture", "status": result.document["status"],
            "unknown_reasons": list(result.document["unknown_reasons"]), "files": files}
        bound = runtime._owned_write_once(self.bundle, "capture-manifest.json", contract.canonical_json(manifest) + b"\n")
        return contract.MaterialBinding(str(self.bundle / bound["path"]), bound["sha256"], bound["bytes"])

    def altered(self, frames: list[dict]) -> bytes:
        return b"".join(contract.canonical_json(frame) + b"\n" for frame in frames)

    def test_plan_only_reads_and_recursively_freezes(self) -> None:
        with patch("subprocess.Popen", side_effect=AssertionError("不应启动")), patch.object(runtime, "verify_native_build", return_value=self.identity):
            plan = runtime.create_owned_probe_plan(self.context, identity=self.identity, bundle_root=self.bundle)
        self.assertEqual(list(self.bundle.iterdir()), [])
        with self.assertRaises(TypeError):
            plan.document["context"]["limits"]["max_records"] = 1

    def test_v1_descriptor_and_disabled_entry_are_preserved(self) -> None:
        self.assertEqual(contract.PROJECTION_DESCRIPTOR["schema_version"], contract.SCHEMA)
        self.assertNotEqual(contract.OWNED_PROJECTION_DESCRIPTOR["schema_version"], contract.SCHEMA)
        with patch("subprocess.Popen", side_effect=AssertionError("不应启动")):
            with self.assertRaises(contract.NativeProcessSourceUnavailable):
                runtime.inspect_native_readiness(self.identity, diagnostic_root=self.bundle)

    def test_limits_reject_boolean_and_old_relaxed_maximum(self) -> None:
        for name, value in (("max_records", True), ("max_records", 100000), ("max_record_bytes", 65537), ("max_queue_records", 4097)):
            document = json.loads(contract.canonical_json(self.plan.document))
            document["context"]["limits"][name] = value
            document["context_sha256"] = contract.fingerprint(document["context"])
            document["plan_sha256"] = contract.fingerprint({key: item for key, item in document.items() if key != "plan_sha256"})
            with self.subTest(name=name, value=value), self.assertRaises(contract.NativeProcessSourceError):
                contract.OwnedProbePlan(document)

    def test_plan_rejects_extra_scope_and_digest_tampering(self) -> None:
        for change in ({"allow_live": True}, {"plan_sha256": "0" * 64}):
            document = json.loads(contract.canonical_json(self.plan.document))
            document.update(change)
            with self.assertRaises(contract.NativeProcessSourceError):
                contract.OwnedProbePlan(document)

    def test_same_named_external_source_is_not_current_runtime_build(self) -> None:
        document = json.loads(Path(self.identity.build_receipt.path).read_bytes())
        for source in document["source_files"]:
            source["path"] = str(self.root / "unmanaged" / Path(source["path"]).name)
        path = self.build / "alien-build.json"
        raw = contract.canonical_json(document) + b"\n"
        path.write_bytes(raw)
        path.chmod(0o600)
        identity = replace(self.identity, build_receipt=contract.MaterialBinding(str(path), hashlib.sha256(raw).hexdigest(), len(raw)),
                           source_files_sha256=contract.fingerprint(document["source_files"]))
        context = replace(self.context, build_identity_sha256=contract.fingerprint(asdict(identity)))
        with patch.object(runtime, "verify_native_build", return_value=identity), patch("subprocess.Popen", side_effect=AssertionError("不得启动")):
            with self.assertRaises(contract.NativeProcessSourceError):
                runtime.create_owned_probe_plan(context, identity=identity, bundle_root=self.bundle)
        self.assertEqual(list(self.bundle.iterdir()), [])

    def test_changed_binary_is_rejected_before_plan_or_spawn(self) -> None:
        Path(self.identity.helper.path).write_bytes(b"broken binary")
        with patch.object(runtime, "verify_native_build", return_value=self.identity), patch("subprocess.Popen", side_effect=AssertionError("不得启动")):
            with self.assertRaises(contract.NativeProcessSourceError):
                runtime.create_owned_probe_plan(self.context, identity=self.identity, bundle_root=self.bundle)
        self.assertEqual(list(self.bundle.iterdir()), [])

    def test_replaced_directory_cannot_pass_actual_plan_validation(self) -> None:
        self.bundle.rmdir()
        self.bundle.mkdir(mode=0o700)
        # inode 可能复用，显式保留旧目录可避免同一设备立即复用影响负例。
        document = json.loads(contract.canonical_json(self.plan.document))
        document["bundle_instance"]["ino"] += 1
        document["plan_sha256"] = contract.fingerprint({key: value for key, value in document.items() if key != "plan_sha256"})
        with patch.object(runtime, "verify_native_build", return_value=self.identity), self.assertRaises(contract.NativeProcessSourceError):
            runtime.validate_owned_probe_plan(document, expected_context=self.context, identity=self.identity, bundle_root=self.bundle)

    def test_full_fixture_replays_without_sdk_or_spawn_and_all_flags_false(self) -> None:
        manifest = self.replay_bundle(self.fixture())
        with patch.object(runtime, "verify_native_build", side_effect=AssertionError("不得读当前 SDK")), patch("subprocess.Popen", side_effect=AssertionError("不得启动")):
            result = runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)
        self.assertEqual(result.document["status"], "structurally_valid_fixture")
        self.assertEqual(result.document["unknown_reasons"], ("drain_unverified",))
        self.assertTrue(all(result.document[name] is False for name in contract.OWNED_FLAGS))

    def test_bad_global_or_per_type_sequence_fails_before_filter(self) -> None:
        fixture = self.fixture(finish=False)
        fixture.add("counter_only", {}, "exec")
        for field in ("global_seq_num", "event_seq_num"):
            frames = copy.deepcopy(fixture.frames)
            frames[-1][field] += 1
            with self.subTest(field=field), self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_counter_does_not_expose_process_payload(self) -> None:
        fixture = self.fixture(finish=False)
        fixture.add("counter_only", {"actor": fixture.collector}, "fork")
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.validate_owned_probe_projection(self.plan, fixture.raw)

    def test_mute_inversion_or_unknown_cannot_reach_ready(self) -> None:
        for change in ("inversion", "unknown", "match"):
            frames = copy.deepcopy(self.fixture().frames)
            mute = frames[0]["payload"]["mute_observation"]
            if change == "inversion":
                mute["inversion"]["path"] = 0
            elif change == "unknown":
                mute["query_status"] = "unknown"
            else:
                mute["paths"].update(reported_count=1, examined_count=1, relevant_match_count=1)
            with self.subTest(change=change), self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_boot_after_change_is_not_silently_accepted(self) -> None:
        frames = copy.deepcopy(self.fixture().frames)
        frames[-2]["payload"]["boot_observation"] = boot_observation("22222222-3333-4444-5555-666666666666")
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_equal_boolean_watermark_cannot_impersonate_integer_one(self) -> None:
        fixture = ProjectionFixture(self.plan)
        fixture.water = {"fork": -1, "exec": -1, "exit": -1}
        fixture.ready()
        fixture.finish()
        self.assertEqual(fixture.frames[-1]["payload"]["type_watermarks"]["fork"], 1)
        frames = copy.deepcopy(fixture.frames)
        frames[-1]["payload"]["type_watermarks"]["fork"] = True
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_record_byte_and_relation_caps_reject_the_same_actual_fixture(self) -> None:
        for name, value in (("max_records", 3), ("max_record_bytes", 10), ("max_pending_relations", 1)):
            document = json.loads(contract.canonical_json(self.plan.document))
            document["context"]["limits"][name] = value
            document["context_sha256"] = contract.fingerprint(document["context"])
            document["plan_sha256"] = contract.fingerprint({key: item for key, item in document.items() if key != "plan_sha256"})
            plan = contract.OwnedProbePlan(document)
            fixture = ProjectionFixture(plan)
            fixture.ready()
            with self.subTest(name=name), self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_owned_probe_projection(plan, fixture.raw)

    def test_queue_error_retains_additional_observed_unwritten_unclosed_count(self) -> None:
        fixture = self.fixture(finish=False)
        fixture.finish(reason="queue_overflow")
        # callback 已观察到而未成功排入 WAL 的实例不能被写成零；失败记录允许保守上界。
        fixture.frames[-1]["payload"]["unclosed_instances"] = 1
        fixture.lines[-1] = contract.canonical_json(fixture.frames[-1]) + b"\n"
        manifest = self.replay_bundle(fixture)
        result = runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)
        self.assertIn("queue_overflow", result.document["unknown_reasons"])
        self.assertIn("relation_unknown", result.document["unknown_reasons"])

    def test_result_type_rejects_any_true_capability(self) -> None:
        fixture = self.fixture()
        manifest = self.replay_bundle(fixture)
        result = runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)
        for name in contract.OWNED_FLAGS:
            document = json.loads(contract.canonical_json(result.document))
            document[name] = True
            with self.subTest(name=name), self.assertRaises(contract.NativeProcessSourceError):
                contract.OwnedProbeReadResult(document)

    def test_prefix_and_watermark_tampering_are_rejected(self) -> None:
        for field in ("event_prefix", "type_watermarks"):
            frames = copy.deepcopy(self.fixture().frames)
            frames[-1]["payload"][field]["sha256" if field == "event_prefix" else "exit"] = "f" * 64 if field == "event_prefix" else 100
            with self.subTest(field=field), self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_half_line_and_frame_after_closed_rejected(self) -> None:
        raw = self.fixture().raw
        for value in (raw[:-1], raw + raw.splitlines(keepends=True)[0]):
            with self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_owned_probe_projection(self.plan, value)

    def test_false_drain_qualification_and_failed_delete_rejected(self) -> None:
        for mutation in ({"drain_status": "verified"}, {"delete_result": 1}, {"unclosed_instances": True}):
            frames = copy.deepcopy(self.fixture().frames)
            frames[-1]["payload"].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_boot_unknown_and_mute_truncation_are_not_promoted(self) -> None:
        for name in ("boot", "mute"):
            frames = copy.deepcopy(self.fixture().frames)
            if name == "boot":
                frames[0]["payload"]["boot_observation"]["representation"] = "unknown"
                frames[0]["payload"]["boot_observation"]["boot_session_uuid"] = None
            else:
                frames[0]["payload"]["mute_observation"]["paths"]["examined_count"] = 1
            with self.subTest(name=name), self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_parent_mismatch_pid_reuse_and_unrecorded_exec_rejected(self) -> None:
        for name in ("parent", "reuse", "image"):
            frames = copy.deepcopy(self.fixture().frames)
            if name == "parent":
                frames[2]["payload"]["target"]["parent_audit_identity"]["pid"] = 500
            elif name == "reuse":
                frames[3]["payload"]["target"]["fork_start_time"]["seconds"] += 1
            else:
                frames[4]["payload"]["actor"]["executable"]["path"] = "/private/tmp/unknown-image"
            with self.subTest(name=name), self.assertRaises(contract.NativeProcessSourceError):
                contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_incomplete_first_error_is_retained_and_cannot_duplicate(self) -> None:
        fixture = self.fixture(finish=False)
        fixture.finish(reason="queue_overflow")
        manifest = self.replay_bundle(fixture)
        result = runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)
        self.assertIn("queue_overflow", result.document["unknown_reasons"])
        self.assertEqual(result.document["status"], "incomplete")
        frames = copy.deepcopy(fixture.frames)
        frames.insert(-1, copy.deepcopy(frames[-2]))
        with self.assertRaises(contract.NativeProcessSourceError):
            contract.validate_owned_probe_projection(self.plan, self.altered(frames))

    def test_actual_file_permissions_hardlink_symlink_and_extra_file_rejected(self) -> None:
        manifest = self.replay_bundle(self.fixture())
        projection = self.bundle / "projection.json"
        projection.chmod(0o644)
        with self.assertRaises(contract.NativeProcessSourceError):
            runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)
        projection.chmod(0o600)
        os.link(projection, self.root / "linked")
        with self.assertRaises(contract.NativeProcessSourceError):
            runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)
        (self.root / "linked").unlink()
        (self.bundle / "extra").write_bytes("不能忽略".encode("utf-8"))
        with self.assertRaises(contract.NativeProcessSourceError):
            runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)
        (self.bundle / "extra").unlink()
        projection.rename(self.root / "real-projection")
        projection.symlink_to(self.root / "real-projection")
        with self.assertRaises(contract.NativeProcessSourceError):
            runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)

    def test_atomic_write_conflict_preserves_original_bytes(self) -> None:
        runtime._owned_write_once(self.bundle, "first_error.json", "原件".encode("utf-8"))
        with self.assertRaises(FileExistsError):
            runtime._owned_write_once(self.bundle, "first_error.json", "替换".encode("utf-8"))
        self.assertEqual((self.bundle / "first_error.json").read_bytes(), "原件".encode("utf-8"))

    def test_fixture_closes_with_missing_closing_as_incomplete(self) -> None:
        manifest = self.replay_bundle(self.fixture(finish=False))
        result = runtime.replay_owned_probe_capture(self.plan, self.bundle, manifest)
        self.assertIn("closing_barrier_missing", result.document["unknown_reasons"])

    def test_pipe_lifecycle_uses_fixed_argv_empty_environment_and_replays_close(self) -> None:
        peers = []
        def fake(argv, **options):
            self.assertEqual(options["env"], {})
            self.assertTrue(options["close_fds"])
            self.assertNotIn("shell", options)
            peer = PipePeer(self.plan, argv)
            peers.append(peer)
            return peer
        with patch.object(runtime, "_owned_revalidate"), patch("subprocess.Popen", side_effect=fake) as popen:
            instance = runtime.NativeOwnedProbeCollector.open(self.plan)
            ready = instance.await_ready()
            self.assertEqual(Path(ready.path).name, "ready.json")
            instance.run_owned_probe()
            with self.assertRaises(contract.NativeProcessSourceError):
                instance.run_owned_probe()
            result = instance.close()
            commands_before = len(peers[0].commands)
            self.assertEqual(instance.close(), result)
            self.assertEqual(len(peers[0].commands), commands_before)
            self.assertEqual(popen.call_count, 1)
        peers[0].thread.join(1)
        self.assertFalse(peers[0].thread.is_alive())
        self.assertFalse(peers[0].errors)
        self.assertEqual([command["sequence"] for command in peers[0].commands], [1, 2])
        self.assertTrue(all(result.document[name] is False for name in contract.OWNED_FLAGS))

    def test_diagnostic_peer_never_receives_subscription_or_probe_command(self) -> None:
        peers = []
        def fake(argv, **options):
            self.assertIn("--probe-v2", argv)
            self.assertNotIn("--control-fd", argv)
            peer = PipePeer(self.plan, argv)
            peers.append(peer)
            return peer
        with patch.object(runtime, "_owned_revalidate"), patch("subprocess.Popen", side_effect=fake):
            result = runtime.diagnose_owned_probe(self.plan)
        peers[0].thread.join(1)
        self.assertFalse(peers[0].commands)
        self.assertFalse(peers[0].errors)
        self.assertEqual(result.document["status"], "unavailable")

    def test_forged_ready_is_rejected_before_work_or_close_control(self) -> None:
        peers = []
        def fake(argv, **options):
            peer = PipePeer(self.plan, argv, malformed_ready=True)
            peers.append(peer)
            return peer
        with patch.object(runtime, "_owned_revalidate"), patch("subprocess.Popen", side_effect=fake):
            instance = runtime.NativeOwnedProbeCollector.open(self.plan)
            with self.assertRaises(contract.NativeProcessSourceError):
                instance.await_ready()
            instance._release()
        peers[0].thread.join(1)
        self.assertFalse(peers[0].commands)
        self.assertTrue((self.bundle / "projection.jsonl").read_bytes())

    def test_unknown_close_does_not_repeat_or_overwrite(self) -> None:
        peers = []
        def fake(argv, **options):
            peer = PipePeer(self.plan, argv)
            peers.append(peer)
            return peer
        with patch.object(runtime, "_owned_revalidate"), patch("subprocess.Popen", side_effect=fake):
            instance = runtime.NativeOwnedProbeCollector.open(self.plan)
            instance.await_ready()
            with patch.object(instance, "_pump", side_effect=contract.NativeProcessSourceError("wal_truncated")):
                with self.assertRaises(contract.NativeProcessSourceError):
                    instance.close()
            before = (self.bundle / "projection.jsonl").read_bytes()
            with self.assertRaises(contract.NativeProcessSourceError):
                instance.close()
            self.assertEqual((self.bundle / "projection.jsonl").read_bytes(), before)
        peers[0].thread.join(1)
        self.assertLessEqual(len(peers[0].commands), 1)

    def test_existing_bundle_fails_before_pipe_or_spawn(self) -> None:
        (self.bundle / "projection.jsonl").write_bytes("历史原件\n".encode("utf-8"))
        with patch.object(runtime, "_owned_revalidate"), patch("subprocess.Popen", side_effect=AssertionError("不得启动")), patch("os.pipe", side_effect=AssertionError("不得建管道")):
            with self.assertRaises(contract.NativeProcessSourceError):
                runtime.NativeOwnedProbeCollector.open(self.plan)
        self.assertEqual((self.bundle / "projection.jsonl").read_bytes(), "历史原件\n".encode("utf-8"))

    def test_native_source_has_separate_real_entry_and_no_product_or_unmute_api(self) -> None:
        native = Path(runtime.__file__).with_name("claude_mac_process_events_native.m").read_text()
        self.assertIn("static int ownedCollectorMain", native)
        self.assertIn("es_new_client(&client", native)
        self.assertIn("handleOwnedMessage(state, message)", native)
        self.assertIn("subscribeFixedEvents(client)", native)
        self.assertIn("es_delete_client(client)", native)
        self.assertIn("es_muting_inverted(client", native)
        for forbidden in ("es_unmute_", "es_invert_muting(", "es_exec_arg(", "es_exec_env(", "kill("):
            self.assertNotIn(forbidden, native)


if __name__ == "__main__":
    unittest.main()
