"""真实本机 TLS-H2 原字节、预算、恢复及严格导出专项；所有来源仍是回归夹具。"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_capture_h2_contract as hc
from tools.official_client_capture import claude_mac_capture_h2_runtime as runtime
from tools.official_client_capture.claude_upgrade_mac_capture_client import CaptureClient
from tools.official_client_capture.claude_mac_capture_h2_dependencies import FrozenDependencies
from tools.official_client_capture.tests import test_claude_mac_capture_service as old
from tools.official_client_capture.tests import claude_h2_dependency_fixture as fixture

CERTS = Path(__file__).resolve().parent / "fixtures/claude_h2_tls"

def peer_modules():
    root, production, peer = fixture.bundles()
    return peer, peer.load_module("h2.connection"), peer.load_module("h2.config"), peer.load_module("h2.events")

def run_peer(args):
    """使用固定真实 h2 peer；保存其实际收到的字节和依赖来源。"""
    os.umask(0o077)
    peer, connection_module, config_module, events = peer_modules()
    root = Path(args.output).resolve()
    c.private_directory(root)
    c.write_json_once(root / "peer-dependencies.json", peer.receipt())
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.port))
    server.listen(1)
    server.settimeout(8)
    print("ready", flush=True)
    raw_parts, requests, ended = [], {}, []
    with server:
        raw, _ = server.accept()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.certificate, args.private_key)
        context.set_alpn_protocols(["http/1.1"] if args.mode == "mismatch" else ["h2"])
        with context.wrap_socket(raw, server_side=True) as sock:
            sock.settimeout(4)
            c.write_json_once(root / "peer-tls.json", {"selected_alpn": sock.selected_alpn_protocol(), "tls_version": sock.version()})
            peer_h2 = connection_module.H2Connection(config=config_module.H2Configuration(client_side=False, header_encoding=None))
            if args.mode != "mismatch":
                peer_h2.initiate_connection()
                sock.sendall(peer_h2.data_to_send())
            try:
                while True:
                    part = sock.recv(65536)
                    if not part:
                        break
                    raw_parts.append(part)
                    if args.mode == "mismatch":
                        continue
                    for event in peer_h2.receive_data(part):
                        if isinstance(event, events.RequestReceived):
                            requests[event.stream_id] = {"headers": [[base64.b64encode(a).decode(), base64.b64encode(b).decode()] for a, b in event.headers], "body": bytearray()}
                        elif isinstance(event, events.DataReceived):
                            requests[event.stream_id]["body"].extend(event.data)
                            peer_h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        elif isinstance(event, events.StreamEnded):
                            ended.append(event.stream_id)
                    if len(ended) == args.requests and args.mode != "hold":
                        for stream_id in reversed(ended):
                            body = b"data: " + bytes(requests[stream_id]["body"]) + b"\n\n"
                            peer_h2.send_headers(stream_id, [(b":status", b"200"), (b"content-type", b"text/event-stream"),
                                (b"content-length", str(len(body)).encode()), (b"x-peer-dynamic", b"same-frozen-value")])
                            peer_h2.send_data(stream_id, body, end_stream=True)
                        ended.append(-1)
                    pending = peer_h2.data_to_send()
                    if pending:
                        sock.sendall(pending)
                    serial = [{"stream_id": key, "body_base64": base64.b64encode(value["body"]).decode(), "headers": value["headers"]}
                              for key, value in requests.items()]
                    (root / "peer-requests.json").write_bytes(c.canonical({"requests": serial}))
                    (root / "peer-requests.json").chmod(0o600)
            except (OSError, ssl.SSLError):
                pass
            finally:
                c.write_once(root / "peer-received.bin", b"".join(raw_parts))
    return 0

class NativeH2ServiceTests(unittest.TestCase):
    def setUp(self):
        self.h = old.NativeCaptureTests("runTest")
        original = tempfile.TemporaryDirectory
        def private_temporary(*args, **kwargs):
            if kwargs.get("dir") == "/private/tmp":
                kwargs["dir"] = str(Path(tempfile.gettempdir()).resolve())
            return original(*args, **kwargs)
        with mock.patch.object(old.tempfile, "TemporaryDirectory", private_temporary):
            self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.root, self.data = self.h.root, self.h.data
        self.peer_process = None
        self.addCleanup(self.archive_artifacts)

    def archive_artifacts(self):
        """仅显式指定的任务私有产物根保留原件；原收据的路径与来源不重写。"""
        destination_root = os.environ.get("CLAUDE_H2_SERVICE_ARTIFACT_ROOT")
        if not destination_root:
            return
        self.h._stop_processes()
        root = c.private_directory(Path(destination_root).resolve())
        destination = root / self._testMethodName
        self.assertFalse(destination.exists(), "专项原件不得覆盖")
        self.assertFalse(any(path.is_symlink() for path in self.root.rglob("*")))
        shutil.copytree(self.root, destination)
        destination.chmod(0o700)
        for path in destination.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        c.write_json_once(destination / "fixture-scope.json", {"source_scope": c.SCOPE,
            "formal_ready": False, "launch_allowed": False, "test": self.id(),
            "original_root": str(self.root), "retained_root": str(destination),
            "description": "仅真实本机合成 peer 与原预算服务夹具的回归原件，不含真实账号或产品证明"})

    def prepare(self, amount=4, tls=True):
        for name in ("cert.pem", "key.pem"):
            c.write_once(self.data / name, (CERTS / name).read_bytes())
        hold = self.h._prepare(tls=tls, amount=amount)
        fixture.prepare(self.data / "h2-private")
        c.write_json_once(self.data / "runtime.json", runtime.collect_runtime())
        configuration = self.h.configuration
        configuration.update(schema_version="claude-upgrade-native-capture-configuration/v2",
            protocol_policy={"mode": "tls_alpn" if tls else "cleartext_h1", "allow_no_alpn_h1": False},
            h2_dependencies={"root_relative": "h2-private", "manifest": dict(fixture.PRODUCTION)},
            runtime_environment_binding=c.binding(self.data, self.data / "runtime.json"))
        configuration["limits"].update(max_streams=16, max_frame_bytes=65536, max_hpack_table_bytes=8192,
            max_pending_settings=16, max_frames=512, max_settings_entries=64)
        (self.data / "configuration.json").write_bytes(c.canonical(configuration) + b"\n")
        self.client = self.h.client = CaptureClient(configuration, self.root / "received", timeout_seconds=8)
        self.configuration, self.task = configuration, self.h.task
        return hold

    def upstream(self, mode="normal", requests=2):
        output = self.root / "peer"
        argv = [sys.executable, "-B", "-S", "-m", "tools.official_client_capture.tests.test_claude_mac_capture_h2_service",
                "--peer", "--port", str(self.h.upstream_port), "--certificate", str(self.data / "cert.pem"),
                "--private-key", str(self.data / "key.pem"), "--output", str(output),
                "--mode", mode, "--requests", str(requests)]
        process = self.h._spawn(argv, "h2-upstream")
        self.h._wait(lambda: "ready" in (self.root / "h2-upstream.log").read_text(), process)
        self.peer_process = process
        return process

    def daemon(self, hold):
        process = self.h._daemon()
        self.client.reserve(self.task, hold)
        self.client.wait_ready(self.task)
        return process

    def connection(self, count=2, offers=None):
        _, module, config, events = peer_modules()
        context = ssl.create_default_context(cafile=str(self.data / "cert.pem"))
        context.set_alpn_protocols(offers or ["h2"])
        sock = context.wrap_socket(socket.create_connection(("127.0.0.1", self.h.listener_port), timeout=3),
                                   server_hostname="127.0.0.1")
        self.addCleanup(sock.close)
        sock.settimeout(3)
        self.assertEqual(sock.selected_alpn_protocol(), "h2")
        peer = module.H2Connection(config=config.H2Configuration(client_side=True, header_encoding=None))
        peer.initiate_connection()
        bodies = {}
        for index in range(count):
            stream_id = index * 2 + 1
            body = c.canonical({"model": "fixture-model", "prompt": "native-h2-" + str(stream_id)})
            bodies[stream_id] = body
            peer.send_headers(stream_id, [(b":method", b"POST"), (b":scheme", b"https"), (b":authority", b"localhost"),
                (b":path", b"/v1/messages"), (b"content-length", str(len(body)).encode()),
                (b"x-client-dynamic", b"same-frozen-value")])
            peer.send_data(stream_id, body, end_stream=True)
        outgoing = peer.data_to_send()
        sock.sendall(outgoing)
        return sock, peer, events, bodies, outgoing

    def responses(self, sock, peer, events, count):
        responses, order = {}, []
        end = time.monotonic() + 5
        while len(order) < count and time.monotonic() < end:
            part = sock.recv(65536)
            self.assertTrue(part)
            for event in peer.receive_data(part):
                if isinstance(event, events.DataReceived):
                    responses.setdefault(event.stream_id, bytearray()).extend(event.data)
                    peer.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, events.StreamEnded):
                    order.append(event.stream_id)
            pending = peer.data_to_send()
            if pending:
                sock.sendall(pending)
        self.assertEqual(len(order), count)
        return responses, order

    def export(self):
        value = self.client.close_and_sync(self.task)
        root = Path(value["root"])
        native = c.read_json(root / "native-capture-manifest.json")
        self.assertEqual(native["schema_version"], "claude-upgrade-native-capture/v2")
        self.assertIs(native["formal_ready"], False)
        self.assertIs(native["launch_allowed"], False)
        if os.environ.get("CLAUDE_H2_SERVICE_ARTIFACT_ROOT"):
            result = subprocess.run([sys.executable, "-B", "-S", "-m", hc.__name__, "--replay", str(root)],
                cwd=Path(__file__).resolve().parents[3], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            c.write_once(self.root / "consumer-replay.json", result.stdout)
        return root, native

    def reject_rebound_export(self, root, key, transform):
        """改原件并重算全部外层绑定；只有原字节与语义复算仍能挡住这些篡改。"""
        altered = self.root / ("tampered-" + key)
        shutil.copytree(root, altered)
        native = c.read_json(altered / "native-capture-manifest.json")
        target = c.check_binding(altered, native[key])
        if key in {"h2_frames", "h2_header_blocks", "journal"}:
            value = hc._json_lines(altered, native[key])
            transform(altered, native, value)
            target.write_bytes(b"".join(c.canonical(row) + b"\n" for row in value))
        else:
            value = c.read_json(target)
            transform(altered, native, value)
            target.write_bytes(c.canonical(value) + b"\n")
        native[key] = c.binding(altered, target)
        (altered / "native-capture-manifest.json").write_bytes(c.canonical(native) + b"\n")
        inventory = c.read_json(altered / "authenticated-inventory.json")
        old_export_id = inventory["export_id"]
        inventory["files"] = [c.binding(altered, c.managed_path(altered, item["path"])) for item in inventory["files"]]
        inventory["native_manifest"] = c.binding(altered, altered / "native-capture-manifest.json")
        inventory["export_id"] = c.digest({name: value for name, value in inventory.items() if name != "export_id"})
        self.assertNotEqual(old_export_id, inventory["export_id"])
        c.validate_inventory(inventory, task_sha256=self.task["task_sha256"], expected_version=2)
        for reference in inventory["files"]:
            c.check_binding(altered, reference)
        with self.assertRaisesRegex(c.CaptureError, "冻结 native v2 复算失败"):
            c.verify_export(altered, inventory, task_sha256=self.task["task_sha256"], expected_version=2)

    def test_tls_h2_reverse_streams_raw_and_consumer(self):
        hold = self.prepare()
        self.upstream()
        self.daemon(hold)
        sock, peer, events, bodies, outgoing = self.connection()
        responses, order = self.responses(sock, peer, events, 2)
        self.assertEqual(order, [3, 1])
        for stream, body in bodies.items():
            self.assertEqual(bytes(responses[stream]), b"data: " + body + b"\n\n")
        root, native = self.export()
        self.assertEqual(native["status"], "complete")
        rows = hc._json_lines(root, native["requests"])
        self.assertEqual([row["stream_id"] for row in rows], [1, 3])
        for row in rows:
            self.assertEqual(base64.b64decode(row["request_body_base64"]), bodies[row["stream_id"]])
        frames = hc._json_lines(root, native["h2_frames"])
        blocks = hc._json_lines(root, native["h2_header_blocks"])
        self.assertTrue(frames and blocks)
        self.assertEqual(native["protocols"], ["h2"])
        wal = hc.read_events(root, native["journal"])
        self.assertLess(next(row["sequence"] for row in wal if row["event"] == "protocol_selected"),
                        next(row["sequence"] for row in wal if row["event"] == "segment"))
        self.assertEqual(len([row for row in wal if row["event"] == "request_header"]), 2)
        self.peer_process.wait(timeout=5)
        actual_upstream = c.read_bytes(self.root / "peer/peer-received.bin")
        client_raw = b"".join(c.read_bytes(c.check_binding(root, item)) for item in native["raw_files"]
                              if item["path"].endswith(".client_to_upstream.bin"))
        self.assertEqual(actual_upstream, client_raw)
        inventory = c.read_json(root / "authenticated-inventory.json")
        with self.assertRaises(c.CaptureError):
            c.verify_export(root, inventory, task_sha256=self.task["task_sha256"], expected_version=1)
        again = self.client.recover(self.task)
        self.assertEqual(again["export_id"], inventory["export_id"])

    def test_budget_rejection_keeps_both_post_bodies_without_forward(self):
        hold = self.prepare(amount=1)
        self.daemon(hold)
        sock, peer, events, bodies, outgoing = self.connection()
        self.h._wait(lambda: self.client.call("status", {})["requests"] == 2)
        root, native = self.export()
        self.assertNotEqual(native["status"], "complete")
        rows = hc._json_lines(root, native["requests"])
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(base64.b64decode(row["request_body_base64"]), bodies[row["stream_id"]])
        self.assertTrue(any("上界" in reason for reason in native["reasons"]))
        self.assertFalse((self.root / "peer").exists())

    def test_actual_alpn_mismatch_keeps_post_and_blocks_first_segment(self):
        hold = self.prepare()
        self.upstream(mode="mismatch")
        self.daemon(hold)
        sock, peer, events, bodies, outgoing = self.connection(offers=["h2", "http/1.1"])
        self.h._wait(lambda: self.client.call("status", {})["requests"] == 2)
        root, native = self.export()
        self.assertNotEqual(native["status"], "complete")
        self.assertTrue(any("ALPN" in reason for reason in native["reasons"]))
        self.peer_process.wait(timeout=5)
        self.assertEqual(c.read_bytes(self.root / "peer/peer-received.bin"), b"")
        observation = next(root.rglob("upstream-connection-observation.json"))
        facts = c.read_json(observation)["tls"]
        self.assertEqual(facts["client_negotiated_alpn"], "h2")
        self.assertEqual(facts["upstream_negotiated_alpn"], "http/1.1")
        self.assertEqual(facts["client_alpn_offers"], facts["upstream_alpn_offers"])
        self.assertEqual(len(hc._json_lines(root, native["requests"])), 2)

    def test_original_h2_wal_sigkill_recovery_never_listens(self):
        hold = self.prepare()
        self.upstream(mode="hold", requests=1)
        process = self.daemon(hold)
        sock, peer, events, bodies, outgoing = self.connection(count=1)
        self.h._wait(lambda: (self.root / "peer/peer-requests.json").exists())
        process.kill()
        process.wait(timeout=3)
        restarted = self.h._daemon()
        self.h._wait(lambda: self.client.call("status", {})["phase"] == "committed", restarted)
        value = self.client.recover(self.task)
        root = Path(value["root"])
        native = c.read_json(root / "native-capture-manifest.json")
        self.assertEqual(native["status"], "truncated")
        self.assertEqual(native["protocols"], ["h2"])
        self.assertEqual(base64.b64decode(hc._json_lines(root, native["requests"])[0]["request_body_base64"]), bodies[1])
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", self.h.listener_port)), 0)
        with self.assertRaises(c.CaptureError):
            self.client.wait_ready(self.task)

    def test_dependency_drift_before_ready_is_not_repaired_or_ignored(self):
        hold = self.prepare()
        process = self.h._daemon()
        self.client.reserve(self.task, hold)
        path = self.data / "capture/h2-dependencies/packages/hpack-4.1.0/hpack/huffman.py"
        original = c.read_bytes(path)
        path.write_bytes(original + b"\n# drift\n")
        with self.assertRaises(c.CaptureError):
            self.client.wait_ready(self.task)
        self.assertFalse((self.data / "capture/ready.json").exists())
        path.write_bytes(original)
        root, native = self.export()
        self.assertNotEqual(native["status"], "complete")

    def test_runtime_receipt_cannot_replace_actual_loaded_origins(self):
        self.prepare()
        expected = c.read_json(self.data / "runtime.json")
        self.assertEqual(expected, runtime.collect_runtime())
        self.assertEqual(expected["python"]["loaded_runtime"]["symbol"], "Py_GetVersion")
        expected["ssl"]["loaded_libraries"][0]["path"] = "/no-such-fixture-libssl"
        with self.assertRaises(c.CaptureError):
            runtime.verify_runtime(expected)
        artifact = FrozenDependencies(self.data / "h2-private", fixture.PRODUCTION, artifact_only=True)
        with self.assertRaises(c.CaptureError):
            artifact.new_hpack_decoder(65536)
        with self.assertRaises(c.CaptureError):
            artifact.receipt()

    def test_frozen_runtime_drift_before_ready_rejects_service(self):
        hold = self.prepare()
        self.h._daemon()
        self.client.reserve(self.task, hold)
        path = self.data / "runtime.json"
        original = c.read_bytes(path)
        value = c.read_json(path)
        value["python"]["loaded_runtime"]["sha256"] = "0" * 64
        path.write_bytes(c.canonical(value) + b"\n")
        with self.assertRaises(c.CaptureError):
            self.client.wait_ready(self.task)
        self.assertFalse((self.data / "capture/ready.json").exists())
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", self.h.listener_port)), 0)
        path.write_bytes(original)
        root, native = self.export()
        self.assertNotEqual(native["status"], "complete")

    def test_v2_cleartext_h1_still_preserves_native_request(self):
        hold = self.prepare(tls=False)
        self.h._upstream()
        self.daemon(hold)
        body = b'{"model":"fixture-model","native":"v2-h1"}'
        self.assertEqual(self.h._post(self.h._connection(), body), b"echo:" + body)
        root, native = self.export()
        self.assertEqual(native["status"], "complete")
        self.assertEqual(native["protocols"], ["h1"])
        row = hc._json_lines(root, native["requests"])[0]
        self.assertEqual(row["protocol"], "h1")
        self.assertIsNone(row["stream_id"])
        self.assertEqual(base64.b64decode(row["request_body_base64"]), body)

    def test_v1_same_connection_h1_contract_is_unchanged(self):
        self.h.test_real_reservation_ready_and_same_connection_ordinals()

    def test_v1_h2_tls_offer_is_still_rejected(self):
        def certificate():
            for name in ("cert.pem", "key.pem"):
                c.write_once(self.data / name, (CERTS / name).read_bytes())
        with mock.patch.object(self.h, "_certificate", side_effect=certificate):
            self.h.test_h2_tls_offer_is_not_downgraded()

    def test_h2_sync_interruption_resumes_same_export_without_listening(self):
        hold = self.prepare()
        self.upstream(requests=1)
        self.daemon(hold)
        sock, peer, events, bodies, outgoing = self.connection(count=1)
        self.responses(sock, peer, events, 1)
        inventory = self.client.call("close", {"task_sha256": self.task["task_sha256"], "reason": "normal"})
        original_chunk = self.client._chunk
        target = "h2-dependencies/packages/hpack-4.1.0/hpack/huffman_table.py"
        def interrupted(export_id, relative, offset, limit):
            if relative == target and offset >= c.MAX_CHUNK_BYTES:
                raise ConnectionError("专项真实分块通信中断")
            return original_chunk(export_id, relative, offset, limit)
        with mock.patch.object(self.client, "_chunk", side_effect=interrupted):
            with self.assertRaises(ConnectionError):
                self.client.sync_committed_export(self.task, inventory["export_id"])
        pending = self.root / "received/pending" / inventory["export_id"]
        self.assertEqual(len(c.read_bytes(pending / ".parts" / target)), c.MAX_CHUNK_BYTES)
        before = self.client.call("status", {})
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", self.h.listener_port)), 0)
        result = self.client.sync_committed_export(self.task, inventory["export_id"])
        self.assertEqual(result["export_id"], inventory["export_id"])
        self.assertEqual(result["status"], "complete")
        after = self.client.call("status", {})
        self.assertEqual((before["requests"], before["connections"]), (after["requests"], after["connections"]))
        self.assertEqual(after["requests"], 1)
        self.peer_process.wait(timeout=5)
        self.assertEqual(len(c.read_json(self.root / "peer/peer-requests.json")["requests"]), 1)
        with self.assertRaises(c.CaptureError):
            self.client.wait_ready(self.task)

    def test_corrupt_h2_frame_block_dependency_or_versions_fail_consumer(self):
        hold = self.prepare()
        self.upstream(requests=1)
        self.daemon(hold)
        sock, peer, events, bodies, outgoing = self.connection(count=1)
        self.responses(sock, peer, events, 1)
        root, native = self.export()
        inventory = c.read_json(root / "authenticated-inventory.json")
        for key in ("h2_frames", "h2_header_blocks", "dependency_manifest"):
            path = c.check_binding(root, native[key])
            raw = c.read_bytes(path)
            path.write_bytes(raw + b"\n")
            try:
                with self.assertRaises(c.CaptureError):
                    c.verify_export(root, inventory, task_sha256=self.task["task_sha256"], expected_version=2)
            finally:
                path.write_bytes(raw)
        wrong = {**inventory, "schema_version": c.INVENTORY_SCHEMA}
        wrong["export_id"] = c.digest({key: value for key, value in wrong.items() if key != "export_id"})
        with self.assertRaises(c.CaptureError):
            self.client._inventory(wrong)

        def frame(_root, _native, rows):
            rows[0]["flags"] ^= 1
        def block(_root, _native, rows):
            rows[0]["headers"][0]["value_base64"] = base64.b64encode(b"changed-method").decode()
        def dependency(_root, _native, value):
            value["loaded_origins"][0]["origin"] = "/no-such-original-hpack-source"
        def ready(_root, _native, value):
            value["configuration_sha256"] = "0" * 64
        def closed(altered, altered_native, rows):
            close = next(row for row in rows if row["event"] == "connection_closed")
            close["data"]["protocol"] = "h1"
            path = c.check_binding(altered, close["data"]["observation"])
            path.write_bytes(c.canonical({key: value for key, value in close["data"].items() if key != "observation"}) + b"\n")
            close["data"]["observation"] = c.binding(altered, path)
            connections_path = c.check_binding(altered, altered_native["connections"])
            connections_path.write_bytes(c.canonical(close["data"]) + b"\n")
            altered_native["connections"] = c.binding(altered, connections_path)
            altered_native["protocols"] = ["h1"]
            previous = "0" * 64
            for row in rows:
                row["previous_sha256"] = previous
                row["sha256"] = c.digest({key: value for key, value in row.items() if key != "sha256"})
                previous = row["sha256"]
        for key, transform in (("h2_frames", frame), ("h2_header_blocks", block),
                               ("dependency_read_set", dependency), ("ready_environment", ready), ("journal", closed)):
            with self.subTest(rebound=key):
                self.reject_rebound_export(root, key, transform)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--peer", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--certificate")
    parser.add_argument("--private-key")
    parser.add_argument("--output")
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--requests", type=int, default=2)
    args = parser.parse_args()
    if args.peer:
        sys.exit(run_peer(args))
    unittest.main()
