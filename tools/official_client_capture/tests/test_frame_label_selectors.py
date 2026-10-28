"""证据标签 frame_labels 的选择键：原始帧序号（旧写法）、消息序号、事件类型序号。

背景：候选网关把压缩写入的一条 WebSocket 消息拆成多个帧，同一条消息的首帧序号随分片数漂移。上一版候选 A06 的
预热消息占 7 帧，声明里写成“原始帧序号 1”的 first_business 标签落在预热消息的续帧上，7 条判据检查选不到任何观测，
直到封存预览才暴露。选择键改成可按消息序号、事件类型序号命中后，标签不再依赖分片数。

本文件覆盖：
1. 键语法（解析器、manifest schema、编目器三处同一份语法）；
2. 解析函数（合成帧：分片、控制帧、冲突、未命中）；
3. 旧写法兼容（只含原始帧序号时与改动前逐帧 ``get(str(序号))`` 的结果逐字相同）；
4. 真实夹具：上一版候选 A06 的派生观测与中继原始字节（三条 response.create 的起始帧在 0、7、14）。
"""

from __future__ import annotations

import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import build_evidence_catalog as catalog
from tools.official_client_capture import candidate_rule_assertion as cra
from tools.official_client_capture.candidate_rule_assertion import (
    AssertionConfigurationError,
    file_sha256,
    load_observations,
    parse_frame_label_selector,
    resolve_frame_label_selectors,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prior_candidate_trace"
# 夹具 manifest 用一个与真实版本无关的版本号，断言引擎只要求它与调用方传入的预期值一致。
FIXTURE_VERSION = "9.9.9"
A06_ENTRY_SUFFIX = "A06_relay_conn001.client_to_upstream.bin.observation.ndjson"


def frames(spec: list[tuple[str, str | None]]) -> list[dict[str, object]]:
    """按（操作码, 事件类型）造一串帧事实，帧序号按位置。"""

    result: list[dict[str, object]] = []
    for position, (opcode, event_type) in enumerate(spec):
        fact: dict[str, object] = {"frame_index": position, "opcode": opcode}
        if event_type is not None:
            fact["event_type"] = event_type
        result.append(fact)
    return result


# 与上一版候选 A06 同形：三条 response.create 的起始帧在 0、7、14，中间是续帧。
FRAGMENTED = frames(
    [("TEXT", "response.create")] + [("CONT", None)] * 6
    + [("TEXT", "response.create")] + [("CONT", None)] * 6
    + [("TEXT", "response.create")] + [("CONT", None)] * 2
)


class SelectorSyntaxTest(unittest.TestCase):
    def test_accepted_and_rejected_keys(self) -> None:
        accepted = {
            "0": ("frame", 0),
            "7": ("frame", 7),
            "message:0": ("message", 0),
            "message:12": ("message", 12),
            "event:response.create:1": ("event", "response.create", 1),
            "event:a-b_c.d:0": ("event", "a-b_c.d", 0),
        }
        for key, expected in accepted.items():
            with self.subTest(key=key):
                self.assertEqual(parse_frame_label_selector(key), expected)
        rejected = [
            "", "00", "01", "-1", "1.5", " 1", "1 ", "1\n", "message", "message:", "message:01", "message:-1",
            "Message:1", "event:response.create", "event::1", "event:a:b:1", "event:resp onse:1",
            "event:response.create:01", "event:" + "x" * 65 + ":1", "frame:1", None, 1, ("1",),
        ]
        for key in rejected:
            with self.subTest(key=key):
                self.assertIsNone(parse_frame_label_selector(key))

    def test_manifest_schema_and_parser_share_one_pattern(self) -> None:
        schema = json.loads((TOOL_ROOT / "candidate_capture_manifest.schema.json").read_text(encoding="utf-8"))
        pattern = schema["properties"]["artifacts"]["items"]["properties"]["frame_labels"]["propertyNames"]["pattern"]
        self.assertEqual(pattern, cra.FRAME_LABEL_SELECTOR_PATTERN)

    # 选择键写法在 0.165.0 的声明里才开始使用；更早版本的声明已封存证据的重放与重新编目不能变，必须仍是旧写法。
    FIRST_SELECTOR_VERSION = (0, 165, 0)

    def test_every_existing_declaration_key_still_parses_as_a_raw_frame_index(self) -> None:
        """历史声明里的 frame_labels 键必须全是旧写法且仍合法；0.165.0 起的声明可用任一合法选择键。"""

        checked = 0
        for path in sorted(TOOL_ROOT.glob("codex_upgrade_evidence_labels_*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            version = tuple(int(part) for part in document["codex_version"].split("."))
            for entry in document["entries"]:
                for rule in entry["rules"]:
                    for key in (rule.get("frame_labels") or {}):
                        checked += 1
                        with self.subTest(path=path.name, key=key):
                            parsed = parse_frame_label_selector(key)
                            self.assertIsNotNone(parsed)
                            if version < self.FIRST_SELECTOR_VERSION:
                                self.assertEqual(parsed[0], "frame")
        self.assertGreater(checked, 0)

    def test_0165_a06_declaration_uses_event_selectors(self) -> None:
        """0.165.0 的 A06 帧标签按事件类型序号命中，不再写原始帧序号（候选分片数变化时索引会漂移）。"""

        document = json.loads((TOOL_ROOT / "codex_upgrade_evidence_labels_0_165_0.json").read_text(encoding="utf-8"))
        keys = [
            key
            for entry in document["entries"]
            for rule in entry["rules"]
            for key in (rule.get("frame_labels") or {})
        ]
        self.assertEqual(sorted(keys), ["event:response.create:0", "event:response.create:1"])


class ResolveSelectorsTest(unittest.TestCase):
    path = "derived/A06/trace.jsonl"

    def resolve(self, frame_labels: dict, spec=FRAGMENTED) -> dict[int, dict[str, str]]:
        return resolve_frame_label_selectors(frame_labels, spec, self.path)

    def test_legacy_raw_indexes_resolve_exactly_like_the_old_per_frame_lookup(self) -> None:
        labels = {"0": {"frame_role": "warmup"}, "7": {"frame_role": "first_business"}}
        resolved = self.resolve(labels)
        for fact in FRAGMENTED:
            self.assertEqual(
                resolved.get(fact["frame_index"], {}),
                labels.get(str(fact["frame_index"]), {}),
            )

    def test_event_and_message_selectors_follow_the_message_not_the_fragment_count(self) -> None:
        expected = {0: {"frame_role": "warmup"}, 7: {"frame_role": "first_business"}}
        by_event = self.resolve({
            "event:response.create:0": {"frame_role": "warmup"},
            "event:response.create:1": {"frame_role": "first_business"},
        })
        by_message = self.resolve({
            "message:0": {"frame_role": "warmup"},
            "message:1": {"frame_role": "first_business"},
        })
        self.assertEqual(by_event, expected)
        self.assertEqual(by_message, expected)
        # 同一份声明在一消息一帧的形态（官方客户端）下命中帧 0、1：声明不依赖分片数。
        unfragmented = frames([("TEXT", "response.create")] * 3)
        self.assertEqual(
            self.resolve({"event:response.create:1": {"frame_role": "first_business"}}, unfragmented),
            {1: {"frame_role": "first_business"}},
        )

    def test_event_ordinal_counts_only_that_event_type_and_control_frames_are_not_messages(self) -> None:
        spec = frames([
            ("TEXT", "response.create"), ("PING", None), ("TEXT", "response.cancel"), ("CONT", None),
            ("BINARY", None), ("TEXT", "response.create"), ("CLOSE", None),
        ])
        self.assertEqual(self.resolve({"event:response.create:1": {"k": "v"}}, spec), {5: {"k": "v"}})
        self.assertEqual(self.resolve({"event:response.cancel:0": {"k": "v"}}, spec), {2: {"k": "v"}})
        # 消息序号：TEXT(0) TEXT(2) BINARY(4) TEXT(5)；PING／CONT／CLOSE 不计数。
        self.assertEqual(self.resolve({"message:2": {"k": "v"}}, spec), {4: {"k": "v"}})
        self.assertEqual(self.resolve({"message:3": {"k": "v"}}, spec), {5: {"k": "v"}})
        # BINARY 消息没有事件类型：事件键数不到它。
        with self.assertRaisesRegex(AssertionConfigurationError, "没有命中任何消息"):
            self.resolve({"event:response.create:2": {"k": "v"}}, spec)

    def test_unmatched_selectors_fail_closed_and_describe_the_stream(self) -> None:
        with self.assertRaisesRegex(
            AssertionConfigurationError,
            r"引用了不存在的 WebSocket 帧：derived/A06/trace.jsonl: 选择键 \['event:response.create:3'\] 没有命中任何消息"
            r"（该 artifact 共 3 条数据消息、事件类型分布 \{response.create: 3\}）",
        ):
            self.resolve({"event:response.create:3": {"k": "v"}})
        with self.assertRaisesRegex(AssertionConfigurationError, r"选择键 \['message:3'\]"):
            self.resolve({"message:3": {"k": "v"}})
        with self.assertRaisesRegex(AssertionConfigurationError, r"选择键 \['event:response.append:0'\]"):
            self.resolve({"event:response.append:0": {"k": "v"}})
        # 旧写法：保持改动前的报错文字（已有测试与审核习惯依赖）。
        with self.assertRaisesRegex(
            AssertionConfigurationError, r"引用了不存在的 WebSocket 帧：derived/A06/trace.jsonl: \[17, 20\]"
        ):
            self.resolve({"20": {"k": "v"}, "17": {"k": "v"}})
        with self.assertRaisesRegex(AssertionConfigurationError, "没有命中任何消息"):
            self.resolve({"message:0": {"k": "v"}}, [])

    def test_two_keys_on_one_frame_merge_when_values_agree_and_conflict_otherwise(self) -> None:
        merged = self.resolve({
            "7": {"frame_role": "first_business"},
            "event:response.create:1": {"frame_role": "first_business", "turn": "t1"},
            "message:1": {"turn": "t1"},
        })
        self.assertEqual(merged, {7: {"frame_role": "first_business", "turn": "t1"}})
        with self.assertRaisesRegex(
            AssertionConfigurationError, r"帧 7 的标签 frame_role 被 '7' 与 'event:response.create:1' 给成不同取值"
        ):
            self.resolve({"7": {"frame_role": "a"}, "event:response.create:1": {"frame_role": "b"}})

    def test_invalid_key_is_rejected_here_too(self) -> None:
        with self.assertRaisesRegex(AssertionConfigurationError, "索引非法"):
            self.resolve({"01": {"k": "v"}})


class CatalogAcceptsSelectorKeysTest(unittest.TestCase):
    def rule(self, frame_labels: object) -> dict:
        return {
            "glob": "relay/conn*.client_to_upstream.bin",
            "scenario_ids": ["A06"],
            "kind": "relay_binary",
            "parser": "opaque_bound_source",
            "derive": {"parser": "h1_request_stream", "kind": "websocket_trace"},
            "labels": {"transport": "websocket"},
            "frame_labels": frame_labels,
            "rationale": "测试声明",
        }

    def test_catalog_validation_accepts_all_three_spellings_and_rejects_bad_ones(self) -> None:
        for good in (
            {"0": {"frame_role": "warmup"}},
            {"event:response.create:0": {"frame_role": "warmup"}, "event:response.create:1": {"frame_role": "first_business"}},
            {"message:0": {"frame_role": "warmup"}, "7": {"phase": "x"}},
        ):
            with self.subTest(good=good):
                catalog._validate_rule(self.rule(good), "job", set(), "candidate")
        for bad in ({"01": {"a": "b"}}, {"message:x": {"a": "b"}}, {"event:response.create": {"a": "b"}}, {"frame:1": {"a": "b"}}):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(catalog.EvidenceCatalogError, "索引非法"):
                    catalog._validate_rule(self.rule(bad), "job", set(), "candidate")
        with self.assertRaisesRegex(catalog.EvidenceCatalogError, "不能覆盖 labels"):
            catalog._validate_rule(
                self.rule({"event:response.create:0": {"transport": "x"}}), "job", set(), "candidate"
            )

    def test_catalog_writes_selector_keys_into_the_manifest_draft_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            relay = root / "relay/conn001.client_to_upstream.bin"
            relay.parent.mkdir(parents=True)
            relay.write_bytes(b"x")
            declaration = {
                "schema_version": catalog.LABELS_SCHEMA,
                "codex_version": FIXTURE_VERSION,
                "entries": [{
                    "job_id": "candidate-frozen-core",
                    "side": "candidate",
                    "rules": [self.rule({"event:response.create:1": {"frame_role": "first_business"}})],
                }],
            }
            built = catalog.build_catalog(
                declaration, {"candidate-frozen-core": [("run", root)]}, side="candidate"
            )
        derived = [a for a in built["manifest_draft"]["artifacts"] if a.get("frame_labels")]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["frame_labels"], {"event:response.create:1": {"frame_role": "first_business"}})


class PriorCandidateFixtureTest(unittest.TestCase):
    """真实夹具：上一版候选 A06 的 WS 出站流，压缩消息被拆成多帧。"""

    def build_bundle(self, root: Path, frame_labels: dict) -> tuple[Path, Path]:
        bundle = root / "assertion-bundle"
        shutil.copytree(FIXTURE / "bundle", bundle)
        entries = json.loads((FIXTURE / "manifest_entries.json").read_text(encoding="utf-8"))["artifacts"]
        artifacts = []
        for entry in entries:
            entry = json.loads(json.dumps(entry))
            entry["sha256"] = file_sha256(bundle / entry["path"])
            if entry["path"].endswith(A06_ENTRY_SUFFIX):
                entry["frame_labels"] = frame_labels
            artifacts.append(entry)
        manifest = bundle / "capture-manifest.json"
        manifest.write_text(
            json.dumps({
                "schema_version": cra.CAPTURE_MANIFEST_SCHEMA_VERSION,
                "codex_version": FIXTURE_VERSION,
                "capture_id": "prior-candidate-fixture",
                "status": "complete",
                "artifacts": artifacts,
            }),
            encoding="utf-8",
        )
        return bundle, manifest

    def labelled_frames(self, frame_labels: dict) -> dict[int, dict]:
        with tempfile.TemporaryDirectory() as directory:
            bundle, manifest = self.build_bundle(Path(directory), frame_labels)
            _, observations = load_observations(manifest, bundle, FIXTURE_VERSION)
        return {
            item.data["frame_index"]: {k: v for k, v in item.labels.items() if k == "frame_role"}
            for item in observations
            if item.record_type == "websocket_frame" and item.scenario_id == "A06" and "frame_role" in item.labels
        }

    def test_fixture_stream_has_three_response_create_messages_starting_at_0_7_14(self) -> None:
        path = FIXTURE / "bundle/derived/A06/websocket_trace" / A06_ENTRY_SUFFIX
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        starts = [
            r["data"]["frame_index"] for r in records
            if r["record_type"] == "websocket_frame" and r["data"].get("event_type") == "response.create"
        ]
        self.assertEqual(starts, [0, 7, 14])

    def test_old_raw_index_one_lands_on_a_continuation_frame_and_seven_on_the_business_message(self) -> None:
        # 上一版声明写的是 1：标签落在预热消息的续帧上（没有 event_type），而 selector 要求 response.create，选不到。
        self.assertEqual(
            self.labelled_frames({"0": {"frame_role": "warmup"}, "1": {"frame_role": "first_business"}}),
            {0: {"frame_role": "warmup"}, 1: {"frame_role": "first_business"}},
        )
        self.assertEqual(
            self.labelled_frames({"0": {"frame_role": "warmup"}, "7": {"frame_role": "first_business"}}),
            {0: {"frame_role": "warmup"}, 7: {"frame_role": "first_business"}},
        )

    def test_event_and_message_selectors_reproduce_the_corrected_raw_index_declaration(self) -> None:
        corrected = self.labelled_frames({"0": {"frame_role": "warmup"}, "7": {"frame_role": "first_business"}})
        by_event = self.labelled_frames({
            "event:response.create:0": {"frame_role": "warmup"},
            "event:response.create:1": {"frame_role": "first_business"},
        })
        by_message = self.labelled_frames({
            "message:0": {"frame_role": "warmup"},
            "message:1": {"frame_role": "first_business"},
        })
        self.assertEqual(by_event, corrected)
        self.assertEqual(by_message, corrected)

    def test_selector_beyond_the_stream_fails_closed_on_real_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle, manifest = self.build_bundle(
                Path(directory), {"event:response.create:3": {"frame_role": "x"}}
            )
            with self.assertRaisesRegex(
                AssertionConfigurationError, r"选择键 \['event:response.create:3'\] 没有命中任何消息（该 artifact 共 3 条数据消息"
            ):
                load_observations(manifest, bundle, FIXTURE_VERSION)

    def test_selector_labels_on_the_raw_h1_stream_path_with_fragmented_messages(self) -> None:
        """h1_request_stream 直接解析原始字节的路径与派生观测路径语义一致（这里用真实候选原始字节）。"""

        raw_root = FIXTURE / "bundle"
        raw = next(raw_root.glob("*/scenarios/A06/relay/conn001.client_to_upstream.bin"))
        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory) / "evidence"
            target = evidence_root / "A06/conn001.client_to_upstream.bin"
            target.parent.mkdir(parents=True)
            shutil.copy2(raw, target)
            manifest = Path(directory) / "capture-manifest.json"
            manifest.write_text(
                json.dumps({
                    "schema_version": cra.CAPTURE_MANIFEST_SCHEMA_VERSION,
                    "codex_version": FIXTURE_VERSION,
                    "capture_id": "prior-candidate-fixture",
                    "status": "complete",
                    "artifacts": [{
                        "path": "A06/conn001.client_to_upstream.bin",
                        "sha256": file_sha256(target),
                        "kind": "relay_binary",
                        "parser": "h1_request_stream",
                        "scenario_ids": ["A06"],
                        "labels": {"connection_id": "conn001", "variant": "default"},
                        "frame_labels": {
                            "event:response.create:0": {"frame_role": "warmup"},
                            "event:response.create:1": {"frame_role": "first_business"},
                        },
                    }],
                }),
                encoding="utf-8",
            )
            _, observations = load_observations(manifest, evidence_root, FIXTURE_VERSION)
        labelled = {
            item.data["frame_index"]: item.labels["frame_role"]
            for item in observations
            if item.record_type == "websocket_frame" and "frame_role" in item.labels
        }
        self.assertEqual(labelled, {0: "warmup", 7: "first_business"})


def masked_text_frame(payload: bytes, *, fin: bool, opcode: int) -> bytes:
    mask = b"\x11\x22\x33\x44"
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    length = len(payload)
    head = bytes(((0x80 if fin else 0) | opcode,))
    if length <= 125:
        return head + bytes((0x80 | length,)) + mask + masked
    return head + bytes((0x80 | 126,)) + struct.pack(">H", length) + mask + masked


class UncompressedFragmentationTest(unittest.TestCase):
    """合成的未压缩分片消息：同一条消息 TEXT(fin=0)＋CONT(fin=1)，选择键命中消息的起始帧。"""

    def test_h1_stream_with_fragmented_messages(self) -> None:
        def message(marker: str) -> bytes:
            payload = json.dumps({"type": "response.create", "marker": marker}).encode("utf-8")
            half = len(payload) // 2
            return (
                masked_text_frame(payload[:half], fin=False, opcode=0x1)
                + masked_text_frame(payload[half:], fin=True, opcode=0x0)
            )

        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory) / "evidence"
            evidence = evidence_root / "A06/conn001.client_to_upstream.bin"
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(
                b"GET /backend-api/codex/responses HTTP/1.1\r\nhost: chatgpt.com\r\nupgrade: websocket\r\n\r\n"
                + message("warmup") + message("business") + message("followup")
            )
            manifest = Path(directory) / "capture-manifest.json"
            manifest.write_text(
                json.dumps({
                    "schema_version": cra.CAPTURE_MANIFEST_SCHEMA_VERSION,
                    "codex_version": FIXTURE_VERSION,
                    "capture_id": "synthetic",
                    "status": "complete",
                    "artifacts": [{
                        "path": "A06/conn001.client_to_upstream.bin",
                        "sha256": file_sha256(evidence),
                        "kind": "relay_binary",
                        "parser": "h1_request_stream",
                        "scenario_ids": ["A06"],
                        "labels": {"connection_id": "conn001"},
                        "frame_labels": {"event:response.create:2": {"frame_role": "followup"}, "message:1": {"frame_role": "business"}},
                    }],
                }),
                encoding="utf-8",
            )
            _, observations = load_observations(manifest, evidence_root, FIXTURE_VERSION)
        labelled = {
            item.data["frame_index"]: item.labels["frame_role"]
            for item in observations
            if item.record_type == "websocket_frame" and "frame_role" in item.labels
        }
        # 帧序号：warmup 0-1、business 2-3、followup 4-5。
        self.assertEqual(labelled, {2: "business", 4: "followup"})


class ConnectionRolesTest(unittest.TestCase):
    """连接级派生标签（0.165.0 登记新增）：线程角色 thread_role 与整段重放帧 frame_role=replay。

    官方 TUI 每个会话另开一条标题线程；换模型、压缩、目录不支持 update 的模型改 effort 之后，官方在同一连接上整段重放历史。
    判据要按线程区分、并只看重放帧，所以引擎在载入 WS 连接观测时从帧事实派生这两个标签：线程来源取摘要器摘出的
    ``shape._turn_metadata.thread_source``，重放帧按“相对前一业务帧换了模型／请求级 effort 变了／前一业务帧是压缩请求”认出——
    只看触发原因，不看被判据验证的 previous_response_id、generate，避免判据同义反复。
    """

    VERSION = FIXTURE_VERSION

    @staticmethod
    def message(*, model: str = "m1", effort: str = "low", previous: str | None = None, generate: bool | None = None,
                types: tuple[str, ...] = ("message",), source: str | None = "user") -> bytes:
        body: dict = {"type": "response.create", "model": model}
        if previous is not None:
            body["previous_response_id"] = previous
        if generate is not None:
            body["generate"] = generate
        body["input"] = [{"type": name} for name in types]
        body["reasoning"] = {"effort": effort}
        if source is not None:
            body["client_metadata"] = {"x-codex-turn-metadata": json.dumps({"thread_source": source, "turn_id": "t"})}
        return masked_text_frame(json.dumps(body).encode("utf-8"), fin=True, opcode=0x1)

    def observations(self, stream: bytes, *, frame_labels: dict | None = None, scenario: str = "A20") -> list:
        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory) / "evidence"
            evidence = evidence_root / f"{scenario}/conn001.client_to_upstream.bin"
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(
                b"GET /backend-api/codex/responses HTTP/1.1\r\nhost: chatgpt.com\r\nupgrade: websocket\r\n\r\n" + stream
            )
            artifact: dict = {
                "path": f"{scenario}/conn001.client_to_upstream.bin",
                "sha256": file_sha256(evidence),
                "kind": "relay_binary",
                "parser": "h1_request_stream",
                "scenario_ids": [scenario],
                "labels": {"connection_id": "conn001"},
            }
            if frame_labels:
                artifact["frame_labels"] = frame_labels
            manifest = Path(directory) / "capture-manifest.json"
            manifest.write_text(
                json.dumps({
                    "schema_version": cra.CAPTURE_MANIFEST_SCHEMA_VERSION,
                    "codex_version": self.VERSION,
                    "capture_id": "synthetic",
                    "status": "complete",
                    "artifacts": [artifact],
                }),
                encoding="utf-8",
            )
            _, observations = load_observations(manifest, evidence_root, self.VERSION)
        return observations

    @staticmethod
    def roles(observations: list) -> tuple[set, dict, dict]:
        threads = {item.labels.get("thread_role") for item in observations}
        frames = {
            item.data["frame_index"]: item.labels.get("frame_role")
            for item in observations if item.record_type == "websocket_frame"
        }
        handshake = {
            item.labels.get("thread_role")
            for item in observations if item.record_type == "http_request"
        }
        return threads, frames, {"handshake": handshake}

    def test_model_switch_replays_are_the_frames_after_each_model_change(self) -> None:
        stream = (
            self.message(generate=False)                                  # 0 预热
            + self.message(previous="r1")                                 # 1 首个业务帧
            + self.message(model="m2")                                    # 2 换模型：整段重放
            + self.message(model="m1")                                    # 3 换回：整段重放
        )
        threads, frames, extra = self.roles(self.observations(stream))
        self.assertEqual(threads, {"main"})
        self.assertEqual(extra["handshake"], {"main"})
        self.assertEqual(frames, {0: None, 1: None, 2: "replay", 3: "replay"})

    def test_frame_after_a_compaction_request_is_a_replay_but_the_compaction_request_is_not(self) -> None:
        stream = (
            self.message(generate=False)
            + self.message(previous="r1")
            + self.message(previous="r2", types=("compaction_trigger",))   # 压缩请求自身是增量帧
            + self.message(types=("compaction", "message"))                # 压缩后的新窗口：整段重放
            + self.message(previous="r4")                                  # 其后恢复增量
        )
        _, frames, _ = self.roles(self.observations(stream, scenario="A21"))
        self.assertEqual(frames, {0: None, 1: None, 2: None, 3: "replay", 4: None})

    def test_prewarm_frames_are_not_business_frames(self) -> None:
        """预热帧（generate=false）的模型／effort 与首个业务帧不同也不能让首个业务帧变成重放。"""

        stream = self.message(generate=False, effort="medium") + self.message(previous="r1", effort="low")
        _, frames, _ = self.roles(self.observations(stream))
        self.assertEqual(frames, {0: None, 1: None})

    def test_request_level_effort_change_replays(self) -> None:
        """目录里不支持 update 的模型改 effort，请求级 reasoning.effort 直接变，客户端同连接发全量帧。"""

        stream = self.message(generate=False) + self.message(previous="r1") + self.message(effort="high")
        _, frames, _ = self.roles(self.observations(stream, scenario="A22"))
        self.assertEqual(frames, {0: None, 1: None, 2: "replay"})

    def test_title_thread_connection_gets_its_role_and_no_replays(self) -> None:
        stream = (
            self.message(generate=False, model="t1", source="thread_title")
            + self.message(model="t2", source="thread_title")
            + self.message(model="t3", source="thread_title")   # 模型变了，但标题线程不算主线程的重放
        )
        threads, frames, extra = self.roles(self.observations(stream))
        self.assertEqual(threads, {"title"})
        self.assertEqual(extra["handshake"], {"title"})
        self.assertEqual(set(frames.values()), {None})

    def test_unknown_source_is_other_and_missing_turn_metadata_leaves_no_role(self) -> None:
        threads, frames, _ = self.roles(self.observations(self.message(generate=False, source="subagent")))
        self.assertEqual(threads, {"other"})
        threads, frames, extra = self.roles(self.observations(self.message(generate=False, source=None)))
        self.assertEqual(threads, {None})
        self.assertEqual(extra["handshake"], {None})
        self.assertEqual(set(frames.values()), {None})

    def test_declared_frame_roles_are_never_overridden(self) -> None:
        stream = self.message(generate=False) + self.message(previous="r1") + self.message(model="m2")
        observations = self.observations(stream, frame_labels={"event:response.create:2": {"frame_role": "first_business"}})
        _, frames, _ = self.roles(observations)
        self.assertEqual(frames[2], "first_business")

    def test_derived_observation_path_derives_the_same_labels_as_the_raw_stream_path(self) -> None:
        stream = (
            self.message(generate=False) + self.message(previous="r1") + self.message(model="m2")
        )
        raw = self.observations(stream)
        records = [
            {
                "schema_version": cra.OBSERVATION_SCHEMA_VERSION,
                "record_id": item.record_id,
                "scenario_id": item.scenario_id,
                "record_type": item.record_type,
                "data": dict(item.data),
            }
            for item in raw
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conn001.observation.ndjson"
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            derived = cra._trace_observations(
                path, "derived/A20/conn001.observation.ndjson", "observation_ndjson", ["A20"], {}, {}, {}
            )
        self.assertEqual(self.roles(derived), self.roles(raw))
        self.assertEqual({item.labels.get("thread_role") for item in derived}, {"main"})

    def test_w3_replay_checks_pass_end_to_end_from_raw_streams_and_reject_the_old_gateway_behaviour(self) -> None:
        """端到端：原始 WS 字节 → 引擎派生线程角色与重放帧标签 → 0.165.0 清单里 SPEC-WS-005 的 5 条重放判据。

        每个场景一条主线程连接（预热、首个业务帧、一次触发重放的变化后的整段帧）加一条标题线程连接（独立预热与业务帧，
        业务帧模型和主线程不同）；官方形态下 5 条判据全过，且标题线程不被主线程判据算进去。旧做法（重放帧链预热响应、
        补第二个预热、重放新开连接）逐条被拒。"""

        profile = json.loads((TOOL_ROOT / "candidate_rule_expectations_0_165_0.json").read_text(encoding="utf-8"))
        rule = next(item for item in profile["rules"] if item["rule_id"] == "SPEC-WS-005")
        checks = {check["id"]: check for check in rule["checks"]}
        replay_ids = ("replay-frame-no-previous-response-id", "replay-frame-no-generate", "replay-frame-carries-full-history",
                      "replay-no-second-warmup", "replay-same-connection")

        def message(**kwargs) -> bytes:
            return self.message(types=("additional_tools", "message"), **kwargs)

        def official_connections(scenario: str, trigger: dict) -> list:
            main = message(generate=False) + message(previous="r1") + message(**trigger)
            title = message(generate=False, model="t1", source="thread_title") + message(model="t2", source="thread_title")
            return self.observations(main, scenario=scenario) + self.observations(title, scenario=scenario)

        def evaluate(observations: list) -> dict:
            result = {}
            for check_id in replay_ids:
                selected = cra._select_observations(observations, checks[check_id]["select"], rule["scenario_ids"])
                result[check_id] = cra._evaluate_assertion(selected, checks[check_id]["assertion"])[0]
            return result

        triggers = {"A20": {"model": "m2"}, "A21": {"effort": "high"}, "A22": {"effort": "high"}}
        official = [item for scenario, trigger in triggers.items() for item in official_connections(scenario, trigger)]
        self.assertEqual(evaluate(official), {check_id: True for check_id in replay_ids})

        # 旧做法一：重放帧被网关链了预热响应；二：补了第二个预热；三：重放新开了连接（再一条带自己握手的主线程连接）。
        chained = []
        for scenario, trigger in triggers.items():
            main = message(generate=False) + message(previous="r1") + message(previous="r1", **trigger)
            chained += self.observations(main, scenario=scenario)
        self.assertFalse(evaluate(chained)["replay-frame-no-previous-response-id"])
        second_warmup = []
        for scenario, trigger in triggers.items():
            main = message(generate=False) + message(previous="r1") + message(generate=False, **trigger) + message(**trigger)
            second_warmup += self.observations(main, scenario=scenario)
        self.assertFalse(evaluate(second_warmup)["replay-no-second-warmup"])
        reconnected = official + [
            item for scenario, trigger in triggers.items()
            for item in self.observations(message(generate=False, **trigger) + message(**trigger), scenario=scenario)
        ]
        self.assertFalse(evaluate(reconnected)["replay-same-connection"])

    def test_derive_connection_roles_directly(self) -> None:
        """函数层：没有 turn metadata、没有帧的连接不产生角色。"""

        self.assertEqual(cra.derive_connection_roles([]), (None, set()))
        frames = [{"frame_index": 0, "opcode": "TEXT", "event_type": "response.create", "shape": {}}]
        self.assertEqual(cra.derive_connection_roles(frames), (None, set()))


if __name__ == "__main__":
    unittest.main()
