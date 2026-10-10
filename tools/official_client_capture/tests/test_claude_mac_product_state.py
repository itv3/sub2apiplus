"""产品状态专项：真实临时文件、真实 fixture ECDSA 和明确的无正式资格边界。

仅固定工厂的安装/实例接点在正例替身化；ES 消费、原件验签、WAL、实际状态
读取和原子恢复都运行本体。测试不会启动产品、登记 formal 或申请系统权限。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_product_broker as broker
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_product_origins as origins
from tools.official_client_capture import claude_mac_product_state as product
from tools.official_client_capture.claude_mac_state_journal import StateJournal, StateConflict, SnapshotLimits, file_state
from tools.official_client_capture.tests.test_claude_mac_product_sources import at, process, event


class ProductStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-product-state-fixture-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root/"state"
        self.state.mkdir(mode=0o700)
        self.file = self.state/"config.json"
        self.file.write_bytes(b"before\x00\xff")
        self.file.chmod(0o600)
        self.identity = {"task_sha256": "a"*64, "task_nonce": "fixture-nonce", "producer_run_id": "fixture-run",
            "deadline_utc": "2026-10-10T02:00:00Z"}
        self.task = {"task_sha256": self.identity["task_sha256"], "task_nonce": self.identity["task_nonce"], "execution": {
            "producer_run_id": self.identity["producer_run_id"],
            "recorder_id": c.MANAGED_RECORDER_ID, "request_policy": {"deadline_utc": self.identity["deadline_utc"]},
            "source_deployments": {"mac": {"deployment_id": "fixture-only", "tool_identity_sha256": "b"*64}}}}
        self.application = process(100, "/fixture/unexecuted")
        self.paths = ("config.json",)

    def journal(self, paths=None):
        return StateJournal(self.identity, self.root/"journal", self.state, paths or self.paths)

    def snapshot(self):
        with self.journal() as journal:
            return journal._snapshot()

    def frame(self, kind, second, sequence, paths=None, *, owned=True, fact=None, requested=None):
        return {"kind": "product_state", "event": kind, "paths": [str(path) for path in (paths or [self.file])],
            "file": fact, "requested": requested, "owned": owned, "process": self.application,
            "observed_at_utc": at(second), "global_sequence": sequence, "event_sequence": sequence}

    def close_fact(self, states=None):
        node = product.flatten(self.snapshot() if states is None else states, self.state)[str(self.file)]
        info = self.file.stat()
        return {**node, "device": info.st_dev, "inode": info.st_ino}

    def frames(self, actual, mutations):
        return [event("exec", self.application, 1, 1), *mutations,
            event("exit", self.application, 9, 100), {"kind": "product_state_snapshot",
                "states": product.flatten(actual, self.state), "observed_at_utc": at(10)}]

    def verify(self, before, after, frames):
        product.verify_changes(task=self.task, state_root=self.state, paths=self.paths,
            baseline=before, actual=after, frames=frames, authorized_at_utc=at(0))

    def signature(self, frames):
        raw = b"".join(c.canonical(row)+b"\n" for row in frames)
        context = {"task_sha256": self.task["task_sha256"], "run_id": self.identity["producer_run_id"],
            "user_id": os.geteuid(), "deployment": self.task["execution"]["source_deployments"]["mac"]}
        payload = c.canonical({"schema_version": "claude-mac-product-native-attestation/v1", "context": context,
            "record_count": len(frames), "raw_bytes": len(raw), "stream_sha256": hashlib.sha256(raw).hexdigest(), "first_error": None})
        key = self.root/"fixture-key"
        if not key.exists():
            subprocess.run(["/usr/bin/openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
                           check=True, capture_output=True, timeout=10)
        public = subprocess.run(["/usr/bin/openssl", "ec", "-in", str(key), "-pubout", "-outform", "DER"],
                                check=True, capture_output=True, timeout=10).stdout[-65:]
        (self.root/"payload").write_bytes(payload)
        subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign", str(key), "-out", str(self.root/"signature"),
                        str(self.root/"payload")], check=True, capture_output=True, timeout=10)
        seal = {"kind": "origin_attestation", "payload_base64": base64.b64encode(payload).decode(),
            "signature_base64": base64.b64encode((self.root/"signature").read_bytes()).decode(),
            "public_key_sha256": hashlib.sha256(public).hexdigest()}
        return raw+c.canonical(seal)+b"\n", public

    def test_real_write_close_and_final_snapshot_are_all_required(self):
        before = self.snapshot()
        self.file.write_bytes(b"real-product-fixture-change")
        after = self.snapshot()
        mutations = [self.frame("write", 2, 2), self.frame("close", 3, 3, fact=self.close_fact())]
        self.verify(before, after, self.frames(after, mutations))
        with self.assertRaises(StateConflict): self.verify(before, after, self.frames(after, mutations[:-1]))
        corrupt = self.frames(after, mutations)
        corrupt[-1]["states"][str(self.file)]["sha256"] = "f"*64
        with self.assertRaises(StateConflict): self.verify(before, after, corrupt)

    def test_external_writer_or_later_unclosed_write_is_unknown(self):
        before = self.snapshot()
        self.file.write_bytes(b"change")
        after = self.snapshot()
        for mutations in ([self.frame("close", 3, 3, owned=False, fact=self.close_fact())],
                          [self.frame("close", 3, 3, fact=self.close_fact()), self.frame("write", 4, 4)]):
            with self.subTest(mutations=mutations), self.assertRaises(StateConflict):
                self.verify(before, after, self.frames(after, mutations))

    def test_acl_xattr_hardlink_and_cross_scope_never_authorize_restore(self):
        before = self.snapshot()
        self.file.write_bytes(b"change")
        after = self.snapshot()
        for name in ("setacl", "setextattr", "deleteextattr", "setowner", "setflags", "setattrlist", "clone", "copyfile", "link", "exchangedata"):
            paths = [self.file, self.state/"other"] if name in {"clone", "copyfile", "link", "exchangedata"} else [self.file]
            with self.subTest(event=name), self.assertRaises(StateConflict):
                self.verify(before, after, self.frames(after, [self.frame(name, 2, 2, paths), self.frame("close", 3, 3, fact=self.close_fact())]))
        with self.assertRaises(StateConflict):
            self.verify(before, after, self.frames(after, [self.frame("rename", 2, 2, [self.file, self.root/"outside"], fact=self.close_fact())]))

    def test_metadata_needs_exact_native_requested_values(self):
        before = self.snapshot()
        self.file.chmod(0o640)
        after = self.snapshot()
        self.verify(before, after, self.frames(after, [self.frame("setmode", 2, 2, requested={"mode": 0o640})]))
        with self.assertRaises(StateConflict):
            self.verify(before, after, self.frames(after, [self.frame("setmode", 2, 2, requested={"mode": 0o600})]))

    def test_real_fixture_signature_detects_tamper_and_never_registers_formal(self):
        ready = {"kind": "ready", "task_sha256": self.task["task_sha256"], "run_id": self.identity["producer_run_id"],
            "boot_id": "fixture-boot", "observed_at_utc": at(0), "accessibility_trusted": False,
            "socket_events_complete": False, "module_activation_verified": False}
        closed = {"kind": "closed", "task_sha256": self.task["task_sha256"], "run_id": self.identity["producer_run_id"],
            "observed_at_utc": at(11), "close_requested": True, "unsubscribe_result": 0, "delete_result": 0,
            "unclosed_instances": 0, "first_error": None, "drain_status": "observed_unqualified",
            "socket_events_complete": False, "module_activation_verified": False}
        raw, key = self.signature([ready, event("exec", self.application, 1, 1), event("exit", self.application, 9, 2), closed])
        self.assertTrue(origins.verify_native_original(self.task, raw, key)["kernel_event_chain_closed"])
        with self.assertRaises(origins.ProductOriginError):
            origins.verify_native_original(self.task, raw.replace(b'"pid":100', b'"pid":101', 1), key)
        self.assertEqual(c.SOURCE_ADAPTERS, {})

    def test_fixed_owner_rejects_plain_objects_and_missing_broker_is_zero_start(self):
        with self.journal() as journal:
            journal.prepare()
            with self.assertRaises(StateConflict): journal.bind_product_recorder(SimpleNamespace(formal_ready=True))
        with mock.patch.object(broker.subprocess, "Popen") as popen, self.assertRaises(ValueError):
            broker.load_product_broker(SimpleNamespace())
        popen.assert_not_called()

    def test_journal_restores_real_change_and_replays_original_proof(self):
        # 只替身化正式安装/工厂接点；变化证明仍运行实际解析与真实文件系统。
        owner = SimpleNamespace()
        with self.journal() as journal:
            journal.prepare()
            before = journal._snapshot()
            scope = {"fixture_only": True, **self.identity, "authorized_at_utc": at(0),
                "before_sha256": hashlib.sha256((journal.root/"state-before.bin").read_bytes()).hexdigest()}
            def ack(digest):
                rows = [json.loads(row) for row in (journal.root/"journal.jsonl").read_bytes().splitlines()]
                self.assertEqual(rows[-1]["event"], "product_scope_intent")
                self.assertEqual(digest, c.digest(scope))
                return {"kind": "product_scope", "scope_sha256": digest, "observed_at_utc": at(0)}
            owner._provider = SimpleNamespace(_monitor=SimpleNamespace(authorize_state_scope=ack))
            frames = []
            def proof(_owner, active, frozen, baseline, actual):
                self.assertIs(_owner, owner)
                self.assertEqual(frozen, scope)
                product.verify_changes(task=self.task, state_root=self.state, paths=self.paths,
                    baseline=baseline, actual=actual, frames=frames, authorized_at_utc=at(0))
                return {"fixture_only": True, "actual_sha256": c.digest(actual)}
            with mock.patch.object(product, "scope_context", return_value=scope), mock.patch.object(product, "verify_observation", side_effect=proof):
                journal.authorize_product_scope(owner)
                self.file.write_bytes(b"real-after-authorized-scope")
                actual = journal._snapshot()
                info = self.file.stat()
                fact = {**product.flatten(actual, self.state)[str(self.file)], "device": info.st_dev, "inode": info.st_ino}
                frames.extend(self.frames(actual, [self.frame("write", 2, 2), self.frame("close", 3, 3, fact=fact)]))
                journal.observe_product_writes()
                self.assertEqual(journal.restore()["status"], "restored")
                self.assertEqual(self.file.read_bytes(), b"before\x00\xff")
                evidence = journal.evidence()
                self.assertFalse(evidence["formal_ready"])
                self.assertFalse(evidence["launch_allowed"])
        with self.journal() as recovered, mock.patch.object(product, "scope_context", return_value=scope), mock.patch.object(product, "verify_observation", side_effect=proof):
            recovered.prepare()
            recovered.bind_product_recorder(owner)
            self.assertEqual(recovered.recover()["status"], "restored")
            self.assertEqual(recovered.identity, self.identity)

    def test_scope_cannot_make_unknown_external_write_known(self):
        with self.journal() as journal:
            journal.prepare()
            owner = SimpleNamespace(_provider=SimpleNamespace(_monitor=SimpleNamespace(authorize_state_scope=lambda value:
                {"kind": "product_scope", "scope_sha256": value, "observed_at_utc": at(0)})))
            with mock.patch.object(product, "scope_context", return_value={"fixture_only": True, **self.identity, "authorized_at_utc": at(0)}):
                journal.authorize_product_scope(owner)
            self.file.write_bytes(b"external-unknown")
            with mock.patch.object(product, "verify_observation", side_effect=StateConflict("未知外部写者")):
                with self.assertRaises(StateConflict): journal.observe_product_writes()
                with self.assertRaises(StateConflict): journal.restore()
            self.assertEqual(self.file.read_bytes(), b"external-unknown")

    def test_scope_freeze_blocks_later_setup_intent_execution_and_confirmation(self):
        with self.journal() as journal:
            journal.prepare()
            journal.record_intent("setup", {"config.json": file_state(b"setup", mtime_ns=1)})
            journal.apply_intent("setup")
            scope = {"fixture_only": True, **self.identity, "authorized_at_utc": at(0)}
            owner = SimpleNamespace(_provider=SimpleNamespace(_monitor=SimpleNamespace(authorize_state_scope=lambda value:
                {"kind": "product_scope", "scope_sha256": value, "observed_at_utc": at(0)})))
            with mock.patch.object(product, "scope_context", return_value=scope): journal.authorize_product_scope(owner)
            before = self.file.read_bytes()
            for action in (lambda: journal.record_intent("later", {"config.json": file_state(b"later")}),
                           lambda: journal.apply_intent("setup"), lambda: journal.confirm_intent("setup")):
                with self.assertRaises(ValueError): action()
            self.assertEqual(self.file.read_bytes(), before)
            self.assertFalse(any(row["event"] == "operation_intent" and row["payload"]["operation_id"] == "later"
                                 for row in journal._rows))

    def test_scope_ack_requires_exact_hash_closed_schema_and_prior_authorization(self):
        scope = {"fixture_only": True, **self.identity, "authorized_at_utc": at(2)}
        good = {"kind": "product_scope", "scope_sha256": c.digest(scope), "observed_at_utc": at(3)}
        product.verify_scope_ack(scope, good)
        for changes in ({"scope_sha256": "f"*64}, {"observed_at_utc": at(1)}, {"formal_ready": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): product.verify_scope_ack(scope, {**good, **changes})

    def archived_restore(self):
        """真实 journal、真实文件恢复和 fixture ECDSA；不登记或派发正式来源。"""
        from tools.official_client_capture import claude_mac_agent_bridge as bridge
        from tools.official_client_capture.tests.test_claude_mac_formal_bridge import FormalBridgeTests
        fixture = FormalBridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.mapping_rows = [{"logical": fixture.state[0], "physical": str(self.file)}]
        fixture.mac_install = fixture.installation("mac")
        fixture.engine = bridge.MacBridgeEngine(fixture.task, fixture.mac_install)
        fixture.prepared()
        self.root, self.task = fixture.engine.root, fixture.task
        self.identity = {"task_sha256": self.task["task_sha256"], "task_nonce": self.task["task_nonce"],
            "producer_run_id": self.task["execution"]["producer_run_id"],
            "deadline_utc": self.task["execution"]["request_policy"]["deadline_utc"]}
        patch = mock.patch.object(bridge, "require_mac_bridge_installation", return_value=fixture.mac_install)
        patch.start()
        self.addCleanup(patch.stop)
        context = bridge.verify_bridge_path_context(self.task, self.root)
        ready = {"kind": "ready", "task_sha256": self.task["task_sha256"], "run_id": self.identity["producer_run_id"],
            "boot_id": "fixture-boot", "observed_at_utc": at(0), "accessibility_trusted": False,
            "socket_events_complete": False, "module_activation_verified": False,
            "state_observation_schema": product.SCHEMA, "state_paths": [str(self.file)]}
        closed = {"kind": "closed", "task_sha256": self.task["task_sha256"], "run_id": self.identity["producer_run_id"],
            "observed_at_utc": at(11), "close_requested": True, "unsubscribe_result": 0, "delete_result": 0,
            "unclosed_instances": 0, "first_error": None, "drain_status": "observed_unqualified",
            "socket_events_complete": False, "module_activation_verified": False}
        with self.journal() as journal:
            journal.prepare()
            before = journal._snapshot()
            initial = (journal.root/"journal.jsonl").read_bytes()
            frozen_raw = (journal.root/"frozen-state.json").read_bytes()
            scope = {"schema_version": product.SCHEMA, **self.identity, "recorder_id": c.MANAGED_RECORDER_ID,
                "source_deployments": self.task["execution"]["source_deployments"], "bundle_root": str(self.root),
                "bundle_instance": {"dev": self.root.stat().st_dev, "ino": self.root.stat().st_ino}, "state_root": str(self.state),
                "state_instance": json.loads(frozen_raw)["root_instance"], "paths": list(self.paths),
                "path_mapping": context.mapping_binding, "qualification_sha256": "b"*64,
                "before_sha256": hashlib.sha256((journal.root/"state-before.bin").read_bytes()).hexdigest(),
                "frozen_state_sha256": hashlib.sha256(frozen_raw).hexdigest(), "baseline_sha256": c.digest(before),
                "native_ready": ready, "authorized_at_utc": at(0)}
            ack = {"kind": "product_scope", "scope_sha256": c.digest(scope), "observed_at_utc": at(0)}
            owner = SimpleNamespace(_provider=SimpleNamespace(_monitor=SimpleNamespace(authorize_state_scope=lambda value: ack)))
            frames = []
            def proof(_owner, active, frozen, baseline, actual):
                product.verify_changes(task=self.task, state_root=self.state, paths=self.paths,
                    baseline=baseline, actual=actual, frames=frames, authorized_at_utc=at(0))
                return {"native": c.binding(self.root/"native-events.jsonl", self.root),
                    "scope_sha256": c.digest(scope), "actual_sha256": c.digest(actual)}
            with mock.patch.object(product, "scope_context", return_value=scope), mock.patch.object(product, "verify_observation", side_effect=proof):
                journal.authorize_product_scope(owner)
                self.file.write_bytes(b"signed-fixture-change")
                actual = journal._snapshot()
                fact = self.close_fact(actual)
                frames.extend([ready, ack, *self.frames(actual, [self.frame("write", 2, 2), self.frame("close", 3, 3, fact=fact)]), closed])
                raw, public = self.signature(frames)
                original = self.root/"native-events.jsonl"
                original.write_bytes(raw)
                original.chmod(0o600)
                journal.observe_product_writes()
                journal.restore()
            archive = self.root/"state-originals"
            archive.mkdir(mode=0o700)
            for name in ("frozen-state.json", "state-before.bin", "state-after.bin"):
                shutil.copyfile(journal.root/name, archive/name)
                (archive/name).chmod(0o600)
            for name in ("state-before.bin", "state-after.bin"):
                shutil.copyfile(journal.root/name, self.root/name)
                (self.root/name).chmod(0o600)
            shutil.copytree(journal.root/"materials", archive/"materials")
            for snapshot in (initial, (journal.root/"journal.jsonl").read_bytes()):
                wal = archive/("journal-"+hashlib.sha256(snapshot).hexdigest()+".jsonl")
                wal.write_bytes(snapshot)
                wal.chmod(0o600)
        observed = origins.verify_native_original(self.task, raw, public)
        product.verify_archived_restore(self.task, self.root, observed, path_context=context)
        return context, observed

    def test_archived_before_after_material_and_wal_tamper_are_rejected_readonly(self):
        context, observed = self.archived_restore()
        originals = self.root/"state-originals"
        targets = [originals/"state-before.bin", originals/"state-after.bin", originals/"frozen-state.json",
                   next((originals/"materials").iterdir()), max(originals.glob("journal-*.jsonl"), key=lambda path: path.stat().st_size)]
        for path in targets:
            raw = path.read_bytes()
            with self.subTest(path=path.name):
                path.write_bytes(raw+b" ")
                with self.assertRaises(ValueError): product.verify_archived_restore(self.task, self.root, observed, path_context=context)
                path.write_bytes(raw)
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("归档恢复不得创建目录")), \
             mock.patch.object(StateJournal, "__enter__", side_effect=AssertionError("归档恢复不得取锁")):
            product.verify_archived_restore(self.task, self.root, observed, path_context=context)
        self.assertEqual(before, {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_archived_rehashed_ack_and_phase_order_do_not_bypass_originals(self):
        context, observed = self.archived_restore()
        original = max((self.root/"state-originals").glob("journal-*.jsonl"), key=lambda path: path.stat().st_size)
        raw = original.read_bytes()
        for operation in ("ack", "order", "unknown"):
            rows = [json.loads(line) for line in raw.splitlines()]
            indices = {row["event"]: index for index, row in enumerate(rows)}
            if operation == "ack": rows[indices["product_scope_ack"]]["payload"]["native_ack"]["scope_sha256"] = "f"*64
            elif operation == "order": rows[indices["product_scope_ack"]]["event"] = "product_state_observed"
            else: rows[indices["product_scope_ack"]]["event"] = "posthoc_scope"
            for index, row in enumerate(rows):
                row["previous_sha256"] = rows[index-1]["sha256"] if index else "0"*64
                row["sha256"] = c.digest({key: value for key, value in row.items() if key != "sha256"})
            changed = b"".join(c.canonical(row)+b"\n" for row in rows)
            replacement = original.parent/("journal-"+hashlib.sha256(changed).hexdigest()+".jsonl")
            original.unlink()
            replacement.write_bytes(changed)
            replacement.chmod(0o600)
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                product.verify_archived_restore(self.task, self.root, observed, path_context=context)
            replacement.unlink()
            original.write_bytes(raw)
            original.chmod(0o600)

    def test_snapshot_hard_limits_are_unchanged(self):
        with self.assertRaises(ValueError): SnapshotLimits(max_file_bytes=8*1024*1024+1).validate()
        self.file.write_bytes(b"x"*(8*1024*1024+1))
        with self.journal() as journal, self.assertRaises(ValueError): journal.prepare()


if __name__ == "__main__":
    unittest.main()
