"""固定私有依赖下的 H2 解析与漂移专项；不启动真实产品、监听或正式来源。"""

from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest import mock

from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture.claude_mac_capture_h2 import (
    CLIENT_DIRECTION as C, SERVER_DIRECTION as S, H2Parser, PREFACE,
    validate_hpack_integer_bounds,
)
from tools.official_client_capture.claude_mac_capture_h2_dependencies import FrozenDependencies, PINS

LIMITS = {"max_header_bytes": 65536, "max_body_bytes": 65536, "max_wire_bytes": 1048576,
          "max_streams": 16, "max_frame_bytes": 65536, "max_hpack_table_bytes": 8192,
          "max_pending_settings": 16, "max_frames": 512, "max_settings_entries": 64}


def frame(kind, flags, stream_id, payload=b""):
    return len(payload).to_bytes(3, "big") + bytes([kind, flags]) + stream_id.to_bytes(4, "big") + payload


def settings(items):
    return b"".join(identifier.to_bytes(2, "big") + value.to_bytes(4, "big")
                    for identifier, value in items)


def request(encoder, *, method=b"POST", path=b"/v1/messages", extras=()):
    return encoder.encode([(b":method", method), (b":scheme", b"https"),
                           (b":authority", b"localhost"), (b":path", path), *extras])


def bundles():
    from tools.official_client_capture.tests.claude_h2_dependency_fixture import bundles as frozen_bundles
    return frozen_bundles()


class DependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.production, cls.peer = bundles()
        cls.production.new_hpack_decoder(65536)

    @contextmanager
    def altered(self, path, raw):
        original = c.read_bytes(path)
        path.write_bytes(raw)
        try:
            yield
        finally:
            path.write_bytes(original)

    def test_official_wheel_full_closure_and_production_scope(self):
        receipt = self.production.receipt()
        self.assertEqual([item["name"] for item in receipt["packages"]], ["hpack"])
        self.assertEqual(receipt["packages"][0]["wheel_sha256"], PINS["hpack"][1])
        self.assertEqual(len(self.production.packages["hpack"]["files"]), 14)
        self.assertFalse(receipt["formal_ready"])
        self.assertFalse(receipt["launch_allowed"])
        self.assertEqual(receipt["source_scope"], "regression_fixture")
        self.assertTrue(any(item["path"].endswith("/LICENSE") for item in receipt["read_set"]))
        self.assertTrue(all(item["origin"].startswith(str(self.root / "packages/hpack-4.1.0"))
                            for item in receipt["loaded_origins"]))
        with self.assertRaises(c.CaptureError):
            self.production.load_module("h2.connection")

    def test_huffman_child_drift_is_detected_after_loading(self):
        path = self.root / "packages/hpack-4.1.0/hpack/huffman_table.py"
        with self.altered(path, c.read_bytes(path) + "\n# 漂移专项\n".encode()):
            with self.assertRaises(c.CaptureError):
                self.production.new_hpack_decoder(65536)

    def test_distribution_metadata_drift_is_detected(self):
        path = self.root / "packages/hpack-4.1.0/hpack-4.1.0.dist-info/METADATA"
        with self.altered(path, c.read_bytes(path) + b"\n"):
            with self.assertRaises(c.CaptureError):
                self.production.receipt()

    def test_extra_package_file_is_rejected(self):
        path = self.root / "packages/hpack-4.1.0/hpack/unmanaged.py"
        path.write_bytes(b"extra = True\n")
        path.chmod(0o600)
        try:
            with self.assertRaisesRegex(c.CaptureError, "额外"):
                self.production.revalidate()
        finally:
            path.unlink()

    def test_symlink_dependency_is_rejected(self):
        path = self.root / "packages/hpack-4.1.0/hpack/huffman.py"
        saved = self.root / "temporarily-saved-huffman.py"
        path.rename(saved)
        try:
            path.symlink_to(saved)
            with self.assertRaises(c.CaptureError):
                self.production.revalidate()
        finally:
            path.unlink()
            saved.rename(path)

    def test_actual_origin_and_system_module_shadow_are_rejected(self):
        module = self.production.load_module("hpack")
        with mock.patch.object(module, "__file__", "/private/tmp/unmanaged-hpack/__init__.py"):
            with self.assertRaisesRegex(c.CaptureError, "origin"):
                self.production.revalidate()
        fake = ModuleType("hpack")
        fake.__version__ = "4.1.0"
        with mock.patch.dict(sys.modules, {"hpack": fake}):
            with self.assertRaisesRegex(c.CaptureError, "系统包"):
                self.production.revalidate()

    def test_incomplete_inventory_and_formal_flags_are_rejected(self):
        for label in ("inventory", "formal"):
            value = c.parse_json(c.canonical(self.production.manifest))
            if label == "inventory":
                value["packages"][0]["files"].pop()
            else:
                value["formal_ready"] = True
            path = self.root / ("bad-" + label + "-dependencies.json")
            c.write_json_once(path, value)
            try:
                with self.assertRaises(c.CaptureError):
                    FrozenDependencies(self.root, c.binding(self.root, path))
            finally:
                path.unlink()


class H2ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.production, cls.peer = bundles()
        cls.hpack = cls.production.load_module("hpack")
        cls.connection = cls.peer.load_module("h2.connection")
        cls.config = cls.peer.load_module("h2.config")

    def parser(self, on_header=None, limits=None):
        observer = H2Parser("fixture-h2-connection", limits or LIMITS, on_header,
                            dependencies=self.production)
        observer.feed(C, PREFACE + frame(4, 0, 0), c.utc_now())
        observer.feed(S, frame(4, 0, 0), c.utc_now())
        observer.feed(C, frame(4, 1, 0), c.utc_now())
        observer.feed(S, frame(4, 1, 0), c.utc_now())
        return observer

    def exchange(self, observer, client, server, *, split=False):
        for _round in range(8):
            moved = False
            for direction, source, target in ((C, client, server), (S, server, client)):
                raw = source.data_to_send()
                if raw:
                    moved = True
                    for offset in range(0, len(raw), 7 if split else len(raw)):
                        observer.feed(direction, raw[offset:offset + (7 if split else len(raw))], c.utc_now())
                    target.receive_data(raw)
            if not moved:
                return
        self.fail("固定 peer 交换未在有限轮次内收口")

    def test_fixed_peer_multiplex_dynamic_headers_reverse_responses(self):
        admissions = []
        observer = H2Parser("fixture-peer", LIMITS,
                            lambda row: admissions.append((row["stream_id"], row["request_ordinal"], row["host"])),
                            dependencies=self.production)
        client = self.connection.H2Connection(config=self.config.H2Configuration(client_side=True))
        server = self.connection.H2Connection(config=self.config.H2Configuration(client_side=False))
        client.initiate_connection()
        server.initiate_connection()
        self.exchange(observer, client, server, split=True)
        common = [(b":method", b"POST"), (b":scheme", b"https"), (b":authority", b"localhost"),
                  (b":path", b"/v1/messages"), (b"x-fixture", b"reused-dynamic-entry")]
        client.send_headers(1, common)
        client.send_data(1, b"one", end_stream=True)
        client.send_headers(3, common)
        client.send_data(3, b"three", end_stream=True)
        self.exchange(observer, client, server, split=True)
        server.send_headers(3, [(b":status", b"200")])
        server.send_data(3, b"response-three", end_stream=True)
        server.send_headers(1, [(b":status", b"200")])
        server.send_data(1, b"event: result\ndata: one\n\n", end_stream=True)
        self.exchange(observer, client, server, split=True)
        observer.eof(C)
        observer.eof(S)
        self.assertEqual(admissions, [(1, 1, "localhost"), (3, 2, "localhost")])
        rows = observer.request_rows()
        self.assertEqual([base64.b64decode(row["request_body_base64"]) for row in rows], [b"one", b"three"])
        self.assertEqual(base64.b64decode(rows[0]["response_body_base64"]), b"event: result\ndata: one\n\n")
        self.assertEqual(base64.b64decode(rows[1]["response_body_base64"]), b"response-three")
        blocks = [item for item in observer.header_block_rows() if item["direction"] == C]
        self.assertLess(blocks[1]["bytes"], blocks[0]["bytes"])
        self.assertEqual([row["status"] for row in observer.outcome_rows()], ["complete", "complete"])
        self.assertFalse(observer.has_pending())
        self.assertEqual(observer.errors, [])
        self.assertEqual(len(self.peer.receipt()["packages"]), 3)
        self.assertEqual(len(observer.dependency_receipt()["packages"]), 1)

    def test_fragmented_continuation_padding_priority_native_ordinal(self):
        seen = []
        observer = self.parser(lambda row: seen.append(row["request_ordinal"]))
        encoder = self.hpack.Encoder()
        raw = request(encoder)
        first = frame(1, 1 | 8 | 32, 1, b"\x02" + b"\x00\x00\x00\x00\x0f" + raw[:5] + b"\x00\x00")
        observer.feed(C, first[:11], c.utc_now())
        self.assertEqual(len(observer.requests), 1)
        self.assertEqual(seen, [])
        observer.feed(C, first[11:], c.utc_now())
        observer.feed(C, frame(9, 4, 1, raw[5:]), c.utc_now())
        self.assertEqual(seen, [1])
        self.assertEqual(observer.requests[0]["request_wire_start"], len(PREFACE) + 18)
        self.assertTrue(observer.requests[0]["request_complete"])
        block = observer.header_block_rows()[0]
        self.assertEqual(len(block["frame_references"]), 2)
        self.assertEqual(block["sha256"], __import__("hashlib").sha256(raw).hexdigest())

    def test_continuation_cannot_interleave_or_switch_stream(self):
        for bad in (frame(0, 0, 1, b"x"), frame(9, 4, 3, b"x"), frame(6, 0, 0, b"12345678")):
            with self.subTest(bad=bad[3]):
                observer = self.parser()
                raw = request(self.hpack.Encoder())
                observer.feed(C, frame(1, 0, 1, raw[:4]), c.utc_now())
                with self.assertRaises(c.CaptureError):
                    observer.feed(C, bad, c.utc_now())
                self.assertEqual(observer.requests[0]["request_ordinal"], 1)

    def test_bidirectional_settings_oldest_ack_and_minimum_table_update(self):
        observer = self.parser()
        client_encoder, server_encoder = self.hpack.Encoder(), self.hpack.Encoder()
        observer.feed(C, frame(1, 5, 1, request(client_encoder)), c.utc_now())
        observer.feed(C, frame(4, 0, 0, settings([(1, 0)])) + frame(4, 0, 0, settings([(1, 4096)])), c.utc_now())
        self.assertEqual(observer.decoders[S].max_allowed_table_size, 4096)
        observer.feed(S, frame(4, 1, 0), c.utc_now())
        self.assertEqual(observer.decoders[S].max_allowed_table_size, 0)
        observer.feed(S, frame(4, 1, 0), c.utc_now())
        self.assertEqual(observer.decoders[S].max_allowed_table_size, 4096)
        self.assertEqual(observer.minimum_table_update[S], 0)
        self.assertEqual(observer.decoders[C].max_allowed_table_size, 4096)
        server_encoder.header_table_size = 0
        server_encoder.header_table_size = 4096
        observer.feed(S, frame(1, 5, 1, server_encoder.encode([(b":status", b"200")])), c.utc_now())
        observer.feed(S, frame(4, 0, 0, settings([(1, 1024)])), c.utc_now())
        observer.feed(C, frame(4, 1, 0), c.utc_now())
        self.assertEqual(observer.decoders[C].max_allowed_table_size, 1024)
        self.assertEqual(observer.decoders[S].max_allowed_table_size, 4096)
        client_encoder.header_table_size = 1024
        observer.feed(C, frame(1, 5, 3, request(client_encoder)), c.utc_now())
        self.assertEqual(len(observer.requests), 2)
        self.assertIsNone(observer.minimum_table_update[C])

    def test_repeated_setting_in_one_frame_cannot_hide_required_shrink(self):
        observer = self.parser()
        observer.feed(C, frame(1, 5, 1, request(self.hpack.Encoder())), c.utc_now())
        observer.feed(C, frame(4, 0, 0, settings([(1, 0), (1, 4096)])), c.utc_now())
        observer.feed(S, frame(4, 1, 0), c.utc_now())
        with self.assertRaisesRegex(c.CaptureError, "最小"):
            observer.feed(S, frame(1, 5, 1, self.hpack.Encoder().encode([(b":status", b"200")])), c.utc_now())

    def test_announced_unacked_table_limit_does_not_reject_inflight_headers(self):
        observer = self.parser()
        encoder = self.hpack.Encoder()
        observer.feed(S, frame(4, 0, 0, settings([(1, 0)])), c.utc_now())
        observer.feed(C, frame(1, 5, 1, request(encoder)), c.utc_now())
        self.assertEqual(observer.requests[0]["method"], "POST")
        observer.feed(C, frame(4, 1, 0), c.utc_now())
        with self.assertRaisesRegex(c.CaptureError, "最小"):
            observer.feed(C, frame(1, 5, 3, request(encoder)), c.utc_now())

    def test_bad_settings_ack_and_frame_limits_are_rejected(self):
        bad_frames = (frame(4, 1, 0), frame(4, 1, 0, b"xxxxxx"),
                      frame(4, 0, 1), frame(4, 0, 0, b"x"),
                      frame(4, 0, 0, settings([(1, 8193)])),
                      frame(4, 0, 0, settings([(5, 16383)])),
                      frame(4, 0, 0, settings([(4, 0x80000000)])))
        for bad in bad_frames:
            with self.subTest(bad=bad):
                observer = self.parser()
                with self.assertRaises(c.CaptureError):
                    observer.feed(C, bad, c.utc_now())
        observer = self.parser()
        with self.assertRaises(c.CaptureError):
            observer.feed(S, frame(4, 0, 0, settings([(2, 1)])), c.utc_now())

    def test_hpack_integer_attack_is_bounded_without_false_ff_string_rejection(self):
        with self.assertRaises(c.CaptureError):
            validate_hpack_integer_bounds(b"\xff" * 2000)
        with self.assertRaises(c.CaptureError):
            validate_hpack_integer_bounds(b"\xff\xff\xff\xff\xff\x7f")
        encoder = self.hpack.Encoder()
        block = encoder.encode([(b":method", b"POST"), (b":scheme", b"https"),
                                (b":authority", b"localhost"), (b":path", b"/v1/messages"),
                                (b"x-fixture", b"\xff" * 1024)], huffman=False)
        self.assertEqual(validate_hpack_integer_bounds(block), [])
        observer = self.parser()
        observer.feed(C, frame(1, 5, 1, block), c.utc_now())
        self.assertEqual(observer.requests[0]["method"], "POST")

    def test_budget_trigger_once_before_body_and_rejected_post_body_kept(self):
        seen = []
        def admit(row):
            seen.append((row["stream_id"], row["request_ordinal"], bytes(row["request_body"])))
            if len(seen) > 1:
                raise c.CaptureError("冻结预算为一条")
        observer = self.parser(admit)
        encoder = self.hpack.Encoder()
        observer.feed(C, frame(1, 4, 1, request(encoder)) + frame(0, 1, 1, b"first"), c.utc_now())
        with self.assertRaisesRegex(c.CaptureError, "预算"):
            observer.feed(C, frame(1, 4, 3, request(encoder)) + frame(0, 1, 3, b"second"), c.utc_now())
        self.assertEqual(seen, [(1, 1, b""), (3, 2, b"")])
        self.assertEqual(base64.b64decode(observer.request_rows()[1]["request_body_base64"]), b"second")
        self.assertEqual([row["method"] for row in observer.requests], ["POST", "POST"])
        self.assertTrue(observer.errors)

    def test_trailers_interim_and_failed_response_do_not_create_extra_post(self):
        seen = []
        observer = self.parser(lambda row: seen.append(row["stream_id"]))
        client_encoder, server_encoder = self.hpack.Encoder(), self.hpack.Encoder()
        observer.feed(C, frame(1, 4, 1, request(client_encoder)) + frame(0, 0, 1, b"not-json")
                      + frame(1, 5, 1, client_encoder.encode([(b"x-end", b"yes")])), c.utc_now())
        observer.feed(S, frame(1, 4, 1, server_encoder.encode([(b":status", b"100")])), c.utc_now())
        self.assertFalse(observer.requests[0]["response_seen"])
        observer.feed(S, frame(1, 4, 1, server_encoder.encode([(b":status", b"529")]))
                      + frame(0, 0, 1, b"rejected")
                      + frame(1, 5, 1, server_encoder.encode([(b"x-end", b"yes")])), c.utc_now())
        self.assertEqual(seen, [1])
        self.assertEqual(base64.b64decode(observer.request_rows()[0]["request_body_base64"]), b"not-json")
        self.assertEqual(base64.b64decode(observer.request_rows()[0]["response_body_base64"]), b"rejected")
        self.assertEqual(observer.outcome_rows()[0]["status"], "complete")
        self.assertFalse(observer.has_pending())

    def test_reset_and_goaway_keep_native_post_identity(self):
        for closing in (frame(3, 0, 3, (8).to_bytes(4, "big")),
                        frame(7, 0, 0, (1).to_bytes(4, "big") + (0).to_bytes(4, "big"))):
            with self.subTest(frame_type=closing[3]):
                observer = self.parser()
                encoder = self.hpack.Encoder()
                observer.feed(C, frame(1, 5, 1, request(encoder)) + frame(1, 4, 3, request(encoder))
                              + frame(0, 1, 3, b"retained"), c.utc_now())
                observer.feed(S, frame(1, 5, 1, self.hpack.Encoder().encode([(b":status", b"200")]))
                              + closing, c.utc_now())
                self.assertEqual(len(observer.request_rows()), 2)
                self.assertEqual(observer.outcome_rows()[1]["status"], "failed")
                self.assertEqual(base64.b64decode(observer.request_rows()[1]["request_body_base64"]), b"retained")

    def test_partial_data_eof_keeps_entity_prefix_and_no_false_end_stream(self):
        observer = self.parser()
        observer.feed(C, frame(1, 4, 1, request(self.hpack.Encoder())), c.utc_now())
        raw = frame(0, 1, 1, b"abcde")
        observer.feed(C, raw[:-3], c.utc_now())
        with self.assertRaisesRegex(c.CaptureError, "EOF"):
            observer.eof(C)
        row = observer.request_rows()[0]
        self.assertEqual(base64.b64decode(row["request_body_base64"]), b"ab")
        self.assertFalse(observer.requests[0]["request_complete"])
        self.assertEqual(observer.outcome_rows()[0]["status"], "truncated")
        self.assertFalse(observer.frame_rows()[-1]["complete"])

    def test_partial_headers_eof_keeps_ordinal_without_fabricating_post(self):
        seen = []
        observer = self.parser(lambda row: seen.append(row))
        raw = frame(1, 5, 1, request(self.hpack.Encoder()))
        observer.feed(C, raw[:11], c.utc_now())
        self.assertEqual(len(observer.requests), 1)
        self.assertEqual(observer.requests[0]["request_ordinal"], 1)
        with self.assertRaises(c.CaptureError):
            observer.eof(C)
        self.assertEqual(seen, [])
        self.assertEqual(observer.request_rows()[0]["method"], "")
        self.assertEqual(observer.outcome_rows()[0]["status"], "truncated")

    def test_invalid_pseudo_header_keeps_known_post_and_no_complete(self):
        for extras in (((b":scheme", b"https"),), ((b"x-invalid", b"value\x01"),)):
            with self.subTest(extras=extras):
                seen = []
                observer = self.parser(lambda row: seen.append(row["method"]))
                raw = request(self.hpack.Encoder(), extras=extras)
                with self.assertRaises(c.CaptureError):
                    observer.feed(C, frame(1, 5, 1, raw), c.utc_now())
                self.assertEqual(seen, ["POST"])
                self.assertEqual(observer.request_rows()[0]["method"], "POST")
                self.assertEqual(observer.outcome_rows()[0]["status"], "truncated")

    def test_frozen_expansion_and_drift_before_header_decode_fail_closed(self):
        limits = {**LIMITS, "max_header_bytes": 1024}
        observer = self.parser(limits=limits)
        block = request(self.hpack.Encoder(), extras=((b"x-big", b"a" * 1500),))
        self.assertLess(len(block), 1024)
        with self.assertRaises(c.CaptureError):
            observer.feed(C, frame(1, 5, 1, block), c.utc_now())
        observer = self.parser()
        path = self.root / "packages/hpack-4.1.0/hpack/huffman_table.py"
        original = c.read_bytes(path)
        path.write_bytes(original + b"\n")
        try:
            with self.assertRaises(c.CaptureError):
                observer.feed(C, frame(1, 5, 1, request(self.hpack.Encoder())), c.utc_now())
        finally:
            path.write_bytes(original)

    def test_h1_wrong_stream_and_orphan_response_are_not_silently_accepted(self):
        for raw, direction in ((b"GET / HTTP/1.1\r\n\r\n", C),
                               (frame(1, 5, 2, request(self.hpack.Encoder())), C),
                               (frame(1, 5, 1, self.hpack.Encoder().encode([(b":status", b"200")])), S)):
            with self.subTest(direction=direction):
                if raw.startswith(b"GET"):
                    observer = H2Parser("no-downgrade", LIMITS, dependencies=self.production)
                else:
                    observer = self.parser()
                with self.assertRaises(c.CaptureError):
                    observer.feed(direction, raw, c.utc_now())


if __name__ == "__main__":
    unittest.main()
