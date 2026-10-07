"""Claude checker（升级控制面设计 B4-03）：画像生成、画像与评估用 manifest 的闭合校验、证据解析、断言引擎与单规则命令。

全部用 ``claude_evaluator_fixture`` 的合成证据，不读任何真实取证数据。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_acceptance_contract as contract
from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture.tests import claude_evaluator_fixture as fixture

CHECKER = Path(assertion.__file__).resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy(value):
    return json.loads(json.dumps(value))


class ExpectationsGeneratorTests(unittest.TestCase):
    def test_generator_is_deterministic_and_resolves_release_refs(self) -> None:
        first = fixture.build_profile()
        self.assertEqual(assertion.render_expectations(first), assertion.render_expectations(fixture.build_profile()))
        self.assertEqual([rule["rule_id"] for rule in first["rules"]], fixture.RULES)
        rules = {rule["rule_id"]: rule for rule in first["rules"]}
        self.assertEqual(rules["SPEC-HDR-001"]["atomic_assertion_ids"], ["SPEC-HDR-001", "SPEC-HDR-002"])
        checks = {check["id"]: check for rule in first["rules"] for check in rule["checks"]}
        # $release 引用在生成时换成目标 Release 的 Wire 值，画像里不再出现引用本身。
        self.assertEqual(checks["spec-hdr-001.version-header"]["assertion"]["value"], ["2023-06-01"])
        self.assertEqual(checks["spec-tls-001.sni"]["assertion"]["value"], "api.anthropic.com")
        self.assertNotIn(assertion.RELEASE_REF_KEY, assertion.render_expectations(first).decode("utf-8"))
        self.assertEqual(checks["spec-hdr-002.body-key-prefix"]["atomic_assertion_id"], "SPEC-HDR-002")
        # scenario-only 断言不归任何规则；没有规则引用的场景不进画像。
        self.assertNotIn("SPEC-STATE-005", json.dumps(first))
        self.assertEqual([item["scenario_id"] for item in first["scenarios"]], ["failure.529", "transport.http", "transport.native_tls"])
        self.assertEqual(set(first["generated_from"]), {"ledger", "scenarios", "templates", "release"})
        self.assertEqual(first["generated_from"]["release"], {"profile_sha256": None, "wire_sha256": assertion._canonical_sha256(fixture.RELEASE_WIRE)})

    def test_generator_accepts_required_rules_manifest(self) -> None:
        manifest = {
            "schema_version": assertion.REQUIRED_RULES_SCHEMA,
            "required_rules": [
                {"spec_id": "SPEC-TLS-001", "title": "TLS", "atomic_assertion_ids": ["SPEC-TLS-001"]},
                {"spec_id": "SPEC-HDR-001", "title": "头", "atomic_assertion_ids": ["SPEC-HDR-001", "SPEC-HDR-002"]},
                {"spec_id": "SPEC-FAIL-001", "title": "失败", "atomic_assertion_ids": ["SPEC-FAIL-001"]},
                {"spec_id": "SPEC-META-001", "title": "收据", "atomic_assertion_ids": ["SPEC-META-001"]},
            ],
        }
        profile = assertion.build_expectations(
            claude_version=fixture.TARGET_VERSION,
            ledger=manifest,
            scenarios_payload=_copy(fixture.SCENARIOS_PAYLOAD),
            templates=_copy(fixture.TEMPLATES),
            release_wire=_copy(fixture.RELEASE_WIRE),
        )
        # RequiredRules 清单按清单顺序，规则描述取 title。
        self.assertEqual([rule["rule_id"] for rule in profile["rules"]], ["SPEC-TLS-001", "SPEC-HDR-001", "SPEC-FAIL-001", "SPEC-META-001"])
        self.assertEqual(profile["rules"][0]["description"], "TLS")
        reference = {rule["rule_id"]: rule["checks"] for rule in fixture.build_profile()["rules"]}
        self.assertEqual({rule["rule_id"]: rule["checks"] for rule in profile["rules"]}, reference)

    def test_generator_fails_closed(self) -> None:
        def build(**overrides):
            arguments = {
                "claude_version": fixture.TARGET_VERSION,
                "ledger": _copy(fixture.LEDGER),
                "scenarios_payload": _copy(fixture.SCENARIOS_PAYLOAD),
                "templates": _copy(fixture.TEMPLATES),
                "release_wire": _copy(fixture.RELEASE_WIRE),
                "rule_manifest": _copy(fixture.RULE_MANIFEST),
            }
            arguments.update(overrides)
            return assertion.build_expectations(**arguments)

        templates = _copy(fixture.TEMPLATES)
        del templates["templates"]["SPEC-HDR-002"]
        cases = {
            "没有判据模板": {"templates": templates},
            "没有给出该制品": {"release_wire": None},
            "取不到值": {"release_wire": {"headers": {}, "tls": {"server_name": "api.anthropic.com"}}},
            "场景清单 schema_version": {"scenarios_payload": dict(_copy(fixture.SCENARIOS_PAYLOAD), schema_version="x/v1")},
            "规则集合不一致": {"rule_manifest": dict(_copy(fixture.RULE_MANIFEST), required_rules=fixture.RULES[:-1])},
            "版本号非法": {"claude_version": "2.1"},
        }
        uncovered = _copy(fixture.SCENARIOS_PAYLOAD)
        for scenario in uncovered["evidence_scenarios"]:
            scenario["covers"] = [rule for rule in scenario["covers"] if rule != "SPEC-TLS-001"]
        cases["没有任何证据场景覆盖"] = {"scenarios_payload": uncovered}
        extra_ref = _copy(fixture.TEMPLATES)
        extra_ref["templates"]["SPEC-TLS-001"]["checks"][0]["assertion"]["value"] = {"$release": "wire:/tls/server_name", "x": 1}
        cases["不能带其他字段"] = {"templates": extra_ref}
        bad_artifact = _copy(fixture.TEMPLATES)
        bad_artifact["templates"]["SPEC-TLS-001"]["checks"][0]["assertion"]["value"] = {"$release": "bundle:/x"}
        cases["未知制品"] = {"templates": bad_artifact}
        unknown_type = _copy(fixture.TEMPLATES)
        unknown_type["templates"]["SPEC-TLS-001"]["checks"][0]["select"]["record_type"] = "pcap_packet"
        cases["record_type 未登记"] = {"templates": unknown_type}
        for message, overrides in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(assertion.AssertionConfigurationError, message):
                build(**overrides)

    def test_expectations_subcommand_writes_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="claude-expectations-") as temporary:
            root = Path(temporary)
            inputs = {
                "ledger": fixture.LEDGER,
                "scenarios": fixture.SCENARIOS_PAYLOAD,
                "templates": fixture.TEMPLATES,
                "wire": fixture.RELEASE_WIRE,
                "rules": fixture.RULE_MANIFEST,
            }
            for name, payload in inputs.items():
                (root / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            output = root / "claude_rule_expectations.json"
            argv = [
                assertion.EXPECTATIONS_COMMAND,
                "--claude-version", fixture.TARGET_VERSION,
                "--ledger", str(root / "ledger.json"),
                "--scenarios", str(root / "scenarios.json"),
                "--templates", str(root / "templates.json"),
                "--release-wire", str(root / "wire.json"),
                "--rule-manifest", str(root / "rules.json"),
                "--output", str(output),
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(assertion.main(argv), 0)
            self.assertEqual(output.read_bytes(), assertion.render_expectations(fixture.build_profile()))
            self.assertEqual(json.loads(stdout.getvalue())["sha256"], _sha256(output))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(assertion.main(argv), 1)
            self.assertIn("拒绝覆盖", stderr.getvalue())


class ProfileValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = fixture.build_profile()

    def test_profile_structure_is_closed(self) -> None:
        def mutate(change):
            payload = _copy(self.profile)
            change(payload)
            return payload

        hdr = 1  # SPEC-HDR-001 在第二条
        cases = {
            "字段不闭合": lambda p: p.update(extra=True),
            "status 只能是 approved": lambda p: p.update(status="draft"),
            "没有机器 check": lambda p: p["rules"][hdr]["checks"].pop(),
            "原子断言不属于本规则": lambda p: p["rules"][hdr]["checks"][0].update(atomic_assertion_id="SPEC-TLS-001"),
            "必须是规则场景的子集": lambda p: p["rules"][hdr]["checks"][0]["select"].update(scenario_ids=["failure.529"]),
            "where\\[1\\] 的 operator 不受支持": lambda p: p["rules"][hdr]["checks"][0]["select"]["where"][0].update(operator="regex"),
            "断言 operator 不受支持": lambda p: p["rules"][hdr]["checks"][0]["assertion"].update(operator="all_greater"),
            "引用未定义场景": lambda p: p["rules"][hdr].update(scenario_ids=["transport.http", "transport.alpn"]),
            "id 非法": lambda p: p["rules"][hdr]["checks"][0].update(id=contract.COVERAGE_CHECK_ID),
            "规则重复": lambda p: p["rules"].append(_copy(p["rules"][0])),
            "引用未登记的证据类型": lambda p: p["scenarios"][0].update(required_artifact_kinds=["relay_binary", "har"]),
        }
        for message, change in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(assertion.AssertionConfigurationError, message):
                assertion.validate_profile_document(mutate(change), expected_claude_version=fixture.TARGET_VERSION)
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "不是期望的"):
            assertion.validate_profile_document(self.profile, expected_claude_version="2.1.226")

    def test_load_profile_digest_modes_and_rule_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="claude-profile-") as temporary:
            root = Path(temporary)
            profile_path = root / "profile.json"
            digest = fixture.write_profile(profile_path)
            rules_path = fixture.write_rule_manifest(root / "rules.json")
            loaded = assertion.load_profile(profile_path, rules_path, expected_claude_version=fixture.TARGET_VERSION, expected_profile_sha256=digest)
            self.assertEqual(loaded, self.profile)
            with self.assertRaisesRegex(assertion.AssertionConfigurationError, "与批准值不一致"):
                assertion.load_profile(profile_path, expected_claude_version=fixture.TARGET_VERSION, expected_profile_sha256="0" * 64)
            self.assertNotIn(fixture.TARGET_VERSION, assertion.FROZEN_PROFILE_SHA256S)
            with self.assertRaisesRegex(assertion.AssertionConfigurationError, "没有登记仓库基线画像冻结摘要"):
                assertion.load_profile(profile_path, expected_claude_version=fixture.TARGET_VERSION, verify_frozen_digest=True)
            with mock.patch.dict(assertion.FROZEN_PROFILE_SHA256S, {fixture.TARGET_VERSION: digest}):
                assertion.load_profile(profile_path, expected_claude_version=fixture.TARGET_VERSION, verify_frozen_digest=True)
            reordered = root / "reordered.json"
            reordered.write_text(json.dumps(dict(fixture.RULE_MANIFEST, required_rules=list(reversed(fixture.RULES)))), encoding="utf-8")
            with self.assertRaisesRegex(assertion.AssertionConfigurationError, "同序同集合"):
                assertion.load_profile(profile_path, reordered, expected_claude_version=fixture.TARGET_VERSION, expected_profile_sha256=digest)
            duplicated = root / "duplicated.json"
            duplicated.write_text('{"schema_version": "a", "schema_version": "b"}', encoding="utf-8")
            with self.assertRaisesRegex(assertion.AssertionConfigurationError, "键重复"):
                assertion.load_profile(duplicated, expected_claude_version=fixture.TARGET_VERSION, expected_profile_sha256=_sha256(duplicated))


class EvidenceParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="claude-checker-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        self.side = fixture.write_side(self.root / "official", capture_id="eval-official")

    def test_bind_capture_manifest_requires_sealed_bindings(self) -> None:
        manifest = json.loads(self.side.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], assertion.EVALUATION_MANIFEST_SCHEMA_VERSION)
        # 7 个带标签的 artifact，另补 5 个伴随文件：3 份上游响应字节、2 份中继连接索引，各自紧跟锚点。
        self.assertEqual(len(manifest["artifacts"]), 12)
        kinds = [(item["kind"], item["path"].rsplit("/", 1)[-1]) for item in manifest["artifacts"]]
        self.assertEqual(
            kinds[:6],
            [
                ("relay_manifest", "relay-manifest.json"),
                ("relay_index", "relay.json"),
                ("relay_binary", "conn001.client_to_upstream.bin"),
                ("relay_response", "conn001.upstream_to_client.bin"),
                ("relay_binary", "conn002.client_to_upstream.bin"),
                ("relay_response", "conn002.upstream_to_client.bin"),
            ],
        )
        response = next(item for item in manifest["artifacts"] if item["kind"] == "relay_response")
        anchor = next(item for item in manifest["artifacts"] if item["path"] == response["path"].replace(".upstream_to_client.bin", ".client_to_upstream.bin"))
        self.assertEqual((response["parser"], response["connection"], response["scenario_ids"], response["labels"]), ("h1_response_stream", anchor["connection"], anchor["scenario_ids"], anchor["labels"]))
        self.assertNotIn("derive", response)
        self.assertTrue(all(set(item) >= assertion.ARTIFACT_REQUIRED_FIELDS for item in manifest["artifacts"]))
        bindings = {item["path"]: {"sha256": item["sha256"], "bytes": item["bytes"]} for item in manifest["artifacts"]}
        partial = dict(bindings)
        partial.pop(next(iter(partial)))
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "没有封存绑定"):
            assertion.bind_capture_manifest(self.side.capture_manifest, partial, capture_id="x")
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "标签不一致"):
            assertion.bind_capture_manifest(dict(self.side.capture_manifest, label_mismatches=[{"probe": "print"}]), bindings, capture_id="x")
        # 连接索引必须在封存里；上游响应字节缺了就不补（挂起不回包的连接）。
        without_index = {path: value for path, value in bindings.items() if path != "job-main/print/relay/relay.json"}
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "中继连接索引"):
            assertion.bind_capture_manifest(self.side.capture_manifest, without_index, capture_id="x")
        without_response = {path: value for path, value in bindings.items() if path != "job-main/print/relay/conn001.upstream_to_client.bin"}
        rebound = assertion.bind_capture_manifest(self.side.capture_manifest, without_response, capture_id="x")
        self.assertEqual(len(rebound["artifacts"]), 11)
        # 标签声明不得自带伴随 kind。
        smuggled = _copy(self.side.capture_manifest)
        smuggled["artifacts"].append(dict(_copy(response), labels=dict(response["labels"])))
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "不得自带伴随文件"):
            assertion.bind_capture_manifest(smuggled, bindings, capture_id="x")

        def broken(change):
            payload = _copy(manifest)
            change(payload)
            return payload

        cases = {
            "不是规范相对路径": lambda p: p["artifacts"][0].update(path="job-main/../x.bin"),
            "路径重复": lambda p: p["artifacts"].append(_copy(p["artifacts"][0])),
            "kind／parser 组合未登记": lambda p: p["artifacts"][0].update(parser="jsonl"),
            "只能出现在中继请求／响应字节上": lambda p: next(a for a in p["artifacts"] if a["kind"] == "pcap").update(connection={"ordinal": 1, "connection_id": 1}),
            "h1_request_stream": lambda p: next(a for a in p["artifacts"] if a["kind"] == "relay_manifest").update(derive={"parser": "h1_request_stream", "kind": "process_trace"}),
            "labels 必须是字符串映射": lambda p: p["artifacts"][0].update(labels={"probe": 1}),
            "status 必须是 complete": lambda p: p.update(status="partial"),
        }
        for message, change in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(assertion.AssertionConfigurationError, message):
                assertion.validate_evaluation_manifest(broken(change), expected_claude_version=fixture.TARGET_VERSION)

    def test_h1_parser_handles_plain_gzip_and_chunked(self) -> None:
        secret = "<secret>" + "X" * 24
        stream = (
            fixture.http_request("/v1/messages", fixture.messages_body())
            + fixture.http_request("/v1/messages/count_tokens", {"model": "m", "messages": []}, encoding="gzip")
            + fixture.http_request("/v1/messages", fixture.messages_body("chunk"), chunked=True)
        )
        requests = assertion.parse_h1_requests(stream, "合成连接")
        self.assertEqual([item["request_line"] for item in requests], ["POST /v1/messages", "POST /v1/messages/count_tokens", "POST /v1/messages"])
        plain, gzipped, chunked = requests
        self.assertEqual(plain["body_keys"], ["model", "max_tokens", "messages", "stream"])
        self.assertEqual(plain["header_names_in_order"][:3], ["Host", "Authorization", "anthropic-version"])
        self.assertEqual(plain["query_pairs"], [["beta", "true"]])
        # 秘密头的取值一律替换，只留形态。
        self.assertEqual(plain["headers"][1], ["Authorization", assertion.REDACTED_VALUE])
        self.assertEqual(plain["header_values"]["authorization"], [assertion.REDACTED_VALUE])
        self.assertEqual(
            plain["secret_headers"]["authorization"], {"scheme": "Bearer", "length": len(f"Bearer {secret}"), "scrubbed": True, "placeholder_form": True}
        )
        self.assertNotIn(secret, json.dumps(requests))
        self.assertEqual((gzipped["content_encoding"], gzipped["body"], gzipped["body_json"]), ("gzip", {"model": "m", "messages": []}, True))
        self.assertGreater(gzipped["body_bytes"], 0)
        self.assertEqual((chunked["transfer_encoding"], chunked["content_length"], chunked["body"]["messages"][0]["content"]), ("chunked", None, "chunk"))
        self.assertEqual(chunked["stream_offset"] + chunked["stream_length"], len(stream))

    def test_h1_parser_fails_closed(self) -> None:
        good = fixture.http_request("/v1/messages", fixture.messages_body())
        cases = {
            "正文被截断": good[:-3],
            "请求头不完整": good.split(b"\r\n\r\n", 1)[0],
            "请求行非法": good.replace(b"POST /v1/messages?beta=true HTTP/1.1", b"POST  HTTP/2", 1),
            "多个不同的 Content-Length": good.replace(b"Content-Length:", b"Content-Length: 1\r\nContent-Length:", 1),
            "请求头行非法": good.replace(b"content-type: application/json", b"content-type application/json", 1),
            "chunked 正文不完整": fixture.http_request("/v1/messages", fixture.messages_body(), chunked=True)[:-4],
            "chunk 越界或缺少结尾": fixture.http_request("/v1/messages", fixture.messages_body(), chunked=True)[:-7],
        }
        for message, payload in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(assertion.AssertionConfigurationError, message):
                assertion.parse_h1_requests(payload, "合成连接")

    def test_observations_cover_all_record_types(self) -> None:
        manifest, observations = assertion.load_observations(self.side.manifest_path, self.side.evidence_root, fixture.TARGET_VERSION)
        by_type: dict[str, list] = {}
        for item in observations:
            by_type.setdefault(item.record_type, []).append(item)
        self.assertEqual(
            {key: len(value) for key, value in by_type.items()},
            {"relay_intervention": 1, "claude_http_response": 4, "claude_http_request": 4, "relay_connection": 3, "probe_run": 3, "runtime_receipt": 2, "tls_client_hello": 2},
        )
        intervention = by_type["relay_intervention"][0]
        self.assertEqual((intervention.data["action"], intervention.data["request"], intervention.data["connection_id"]), ("status_529", "POST /v1/messages", 1))
        responses = {(item.labels["probe"], item.data["connection_id"], item.data["response_index"]): item for item in by_type["claude_http_response"]}
        self.assertEqual(responses[("failure-529", 1, 1)].data["status"], 529)
        self.assertEqual(responses[("failure-529", 1, 1)].data["json_body"]["error"]["type"], "overloaded_error")
        chunked = responses[("failure-529", 2, 1)].data
        self.assertEqual((chunked["status"], chunked["transfer_encoding"], chunked["event_types"]), (200, "chunked", ["message_start", "content_block_delta", "message_stop"]))
        self.assertEqual(responses[("print", 1, 2)].data["request_endpoint"], "POST /v1/messages/count_tokens?beta=true")
        connections = {item.record_id: item for item in by_type["relay_connection"]}
        first = connections["job-main/failure-529/relay/conn001.client_to_upstream.bin#connection"]
        self.assertEqual((first.data["intervention_actions"], first.data["response_statuses"], first.data["opened_at_unix_ms"]), (["status_529"], [529], 1787320443000))
        self.assertIn("job-main/failure-529/relay/intervention.jsonl", first.evidence_paths)
        self.assertIn("job-main/failure-529/relay/relay.json", first.evidence_paths)
        self.assertEqual(connections["job-main/print/relay/conn001.client_to_upstream.bin#connection"].data["request_lines"], ["POST /v1/messages", "POST /v1/messages/count_tokens"])
        requests = {(item.labels["probe"], item.data["probe_request_index"]): item.data for item in by_type["claude_http_request"]}
        retry = requests[("failure-529", 1)]
        self.assertEqual(
            (retry["probe_endpoint_index"], retry["previous_same_endpoint_gap_ms"], retry["response"]["status"], retry["intervention_action"], retry["connection_client_alpn"]),
            (1, 600, 200, None, "http/1.1"),
        )
        self.assertEqual((requests[("failure-529", 0)]["intervention_action"], requests[("failure-529", 0)]["response"]["status"]), ("status_529", 529))
        self.assertEqual(retry["probe_env"], {"DISABLE_TELEMETRY": "1"})
        self.assertEqual(requests[("print", 0)]["header_map"]["anthropic-version"], "2023-06-01")
        self.assertEqual(requests[("print", 0)]["tools_sha256"], None)
        runs = {item.labels["probe"]: item.data for item in by_type["probe_run"]}
        self.assertEqual((runs["failure-529"]["message_response_statuses"], runs["failure-529"]["message_gaps_ms"], runs["failure-529"]["relay_mode"]), ([529, 200], [600], "synthetic"))
        self.assertEqual(runs["print"]["endpoints"], ["POST /v1/messages?beta=true", "POST /v1/messages/count_tokens?beta=true"])
        self.assertEqual(runs["failure-529"]["receipt"]["response_plan"], "status_529_then_pass")
        self.assertEqual(runs["tls"]["request_count"], 0)
        hello = by_type["tls_client_hello"][0]
        self.assertEqual((hello.data["sni"], hello.data["extension_types"], hello.data["cipher_suite_count"], hello.data["alpn_protocols"]), ("api.anthropic.com", [0, 43], 4, []))
        self.assertEqual(hello.data["destination"], fixture.UPSTREAM_IP)
        receipt = next(item for item in by_type["runtime_receipt"] if item.labels["probe"] == "print")
        self.assertEqual((receipt.data["client_version"], receipt.data["relay_integrity_result"], receipt.data["manifest"]["probe_id"]), (fixture.TARGET_VERSION, "pass", "print"))
        # selector 可见的映射不含 evidence_paths。
        self.assertNotIn("evidence_paths", intervention.as_mapping())
        self.assertEqual(manifest["capture_id"], "eval-official")

    def test_alpn_and_tampered_evidence(self) -> None:
        hello = assertion.parse_client_hello(fixture.client_hello("api.anthropic.com", ["h2", "http/1.1"]))
        self.assertEqual(hello[3], ["h2", "http/1.1"])
        target = self.side.evidence_root / "job-main" / "print" / "relay-manifest.json"
        target.write_bytes(target.read_bytes().replace(b'"pass"', b'"fail"'))
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "摘要与绑定不一致"):
            assertion.load_observations(self.side.manifest_path, self.side.evidence_root, fixture.TARGET_VERSION)
        os.replace(target, target.with_name("moved.json"))
        target.symlink_to(target.with_name("moved.json"))
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "含符号链接"):
            assertion.load_observations(self.side.manifest_path, self.side.evidence_root, fixture.TARGET_VERSION)


class AssertionEngineTests(unittest.TestCase):
    """where 的 8 个 operator 与断言的 21 个 operator，各取通过与不通过一例。"""

    @staticmethod
    def observation(index: int, data: dict, artifact: str = "a.bin") -> assertion.Observation:
        return assertion.Observation(f"r{index}", ("transport.http",), "claude_http_request", artifact, (artifact,), {"probe": "print"}, data)

    def test_where_operators(self) -> None:
        item = self.observation(1, {"path": "/v1/messages", "keys": ["model", "stream"], "headers": {"x": "1", "y": "2"}})
        cases = [
            ({"path": "data.path", "operator": "equal", "value": "/v1/messages"}, True),
            ({"path": "data.path", "operator": "equal", "value": "/v1/x"}, False),
            ({"path": "data.path", "operator": "not_equal", "value": "/v1/x"}, True),
            ({"path": "data.missing", "operator": "not_equal", "value": "/v1/x"}, False),
            ({"path": "data.headers.x", "operator": "present"}, True),
            ({"path": "data.headers.z", "operator": "absent"}, True),
            ({"path": "data.keys", "operator": "contains", "value": "stream"}, True),
            ({"path": "data.path", "operator": "in", "value": ["/v1/messages", "/v1/x"]}, True),
            ({"path": "data.path", "operator": "match", "value": "^/v1/mess"}, True),
            ({"path": "data.headers", "operator": "subset", "value": {"x": "1"}}, True),
            ({"path": "data.headers", "operator": "subset", "value": {"x": "2"}}, False),
            ({"path": "labels.probe", "operator": "equal", "value": "print"}, True),
        ]
        for condition, expected in cases:
            with self.subTest(condition=condition):
                self.assertIs(assertion._where_matches(item, [condition]), expected)

    def test_assertion_operators(self) -> None:
        records = [
            self.observation(1, {"v": "a", "list": ["x", "y"], "same": [1, 1], "o": {"k": 1, "z": 2}, "l": "x", "r": "x"}, "a.bin"),
            self.observation(2, {"v": "a", "list": ["x", "y", "z"], "same": [2, 2], "o": {"k": 1}, "l": "y", "r": "y"}, "b.bin"),
        ]
        cases = [
            ({"operator": "count_at_least", "value": 2}, True),
            ({"operator": "count_at_least", "value": 3}, False),
            ({"operator": "count_equal", "value": 2}, True),
            ({"operator": "all_equal", "path": "data.v", "value": "a"}, True),
            ({"operator": "any_equal", "path": "data.l", "value": "y"}, True),
            ({"operator": "all_absent", "path": "data.missing"}, True),
            ({"operator": "all_absent", "path": "data.v"}, False),
            ({"operator": "all_contains", "path": "data.list", "value": "y"}, True),
            ({"operator": "all_not_contains", "path": "data.list", "value": "z"}, False),
            ({"operator": "all_lowercase", "path": "data.list"}, True),
            ({"operator": "all_list_prefix", "path": "data.list", "value": ["x"]}, True),
            ({"operator": "all_list_suffix", "path": "data.list", "value": ["y"]}, False),
            ({"operator": "all_match", "path": "data.l", "value": "[xy]"}, True),
            ({"operator": "all_ordered_subset_of", "path": "data.list", "allowed": ["x", "y", "z"], "required": ["x"]}, True),
            ({"operator": "all_ordered_subset_of", "path": "data.list", "allowed": ["y", "x", "z"]}, False),
            ({"operator": "all_subset", "path": "data.o", "value": {"k": 1}}, True),
            ({"operator": "any_subset", "path": "data.o", "value": {"z": 2}}, True),
            ({"operator": "none_subset", "path": "data.o", "value": {"z": 2}}, False),
            ({"operator": "distinct_count_at_least", "path": "data.l", "value": 2}, True),
            ({"operator": "distinct_values_equal", "path": "data.v", "value": ["a"]}, True),
            ({"operator": "same_set_distinct_order", "path": "data.list", "minimum_records": 1, "minimum_artifacts": 1, "minimum_distinct_orders": 1}, True),
            ({"operator": "same_set_distinct_order", "path": "data.list", "minimum_records": 2, "minimum_artifacts": 2, "minimum_distinct_orders": 2}, False),
            ({"operator": "all_fields_equal", "left_path": "data.l", "right_path": "data.r"}, True),
            ({"operator": "all_list_all_same", "path": "data.same"}, True),
            ({"operator": "all_list_all_different", "path": "data.list"}, True),
        ]
        operators = {case["operator"] for case, _expected in cases}
        self.assertEqual(operators, set(assertion.CODEX_ASSERTION_OPERATORS))
        self.assertEqual(len(assertion.CODEX_ASSERTION_OPERATORS), 21)
        for case, expected in cases:
            with self.subTest(case=case):
                passed, _actual = assertion._evaluate_assertion(records, case)
                self.assertIs(passed, expected)
        # 空选择集上 all_* 一律不通过（没有证据不能当作满足）。
        self.assertFalse(assertion._evaluate_assertion([], {"operator": "all_equal", "path": "data.v", "value": "a"})[0])

    def test_claude_operators(self) -> None:
        records = [
            self.observation(1, {"probe": "a", "n": 5, "gaps": [550, 600], "list": ["x", "y", "z"], "ids": ["u1"], "results": ["u1", "u2"], "s": "1", "label": "2-x", "body": {"m": 1, "s": True, "x": 9}}, "a.bin"),
            self.observation(2, {"probe": "a", "n": 7, "gaps": [], "list": ["y", "z"], "ids": [], "results": [], "s": "1", "label": "2-x", "body": {"m": 1, "s": True, "x": 8}}, "b.bin"),
            self.observation(3, {"probe": "b", "n": 9, "gaps": [700], "list": ["x", "y"], "ids": ["v"], "results": ["v"], "s": "2", "label": "2-x", "body": {"m": 1, "s": True, "x": 7}}, "c.bin"),
        ]
        reference_where = [{"path": "data.n", "operator": "equal", "value": 9}]
        cases = [
            ({"operator": "all_between", "path": "data.n", "minimum": 5, "maximum": 9}, True),
            ({"operator": "all_between", "path": "data.n", "minimum": 6, "maximum": 9}, False),
            ({"operator": "all_list_items_between", "path": "data.gaps", "minimum": 500, "maximum": 750}, True),
            ({"operator": "all_list_items_equal", "path": "data.list", "value": "y"}, False),
            ({"operator": "all_distinct", "path": "data.n"}, True),
            ({"operator": "all_distinct", "path": "data.s"}, False),
            ({"operator": "group_distinct_count_equal", "group_path": "data.probe", "path": "data.s", "value": 1}, True),
            ({"operator": "group_distinct_count_equal", "group_path": "data.probe", "path": "data.n", "value": 1}, False),
            ({"operator": "all_fields_compare", "left_path": "data.n", "right_path": "data.n", "comparison": "le"}, True),
            ({"operator": "all_fields_compare", "left_path": "data.s", "right_path": "data.probe", "comparison": "lt"}, False),
            ({"operator": "all_fields_subset", "left_path": "data.ids", "right_path": "data.results"}, True),
            ({"operator": "all_nonempty", "path": "data.ids"}, False),
            ({"operator": "any_contains", "path": "data.list", "value": "x"}, True),
            ({"operator": "all_list_contains_run", "path": "data.list", "value": ["y", "z"]}, False),
            ({"operator": "all_list_contains_run", "path": "data.list", "value": ["y"]}, True),
            ({"operator": "all_equal_reference", "path": "data.body", "reference": {"where": reference_where}, "exclude_keys": ["x"]}, True),
            ({"operator": "all_equal_reference", "path": "data.body", "reference": {"where": reference_where}}, False),
            ({"operator": "none_equal_reference", "path": "data.body.x", "reference": {"where": reference_where}}, False),
            ({"operator": "all_equal_reference", "path": "data.s", "reference": {"where": [{"path": "data.s", "operator": "equal", "value": "1"}]}}, False),
            ({"operator": "all_in", "path": "data.s", "value": ["1", "2"]}, True),
            ({"operator": "all_in", "path": "data.s", "value": ["1"]}, False),
            ({"operator": "all_equal_reference", "path": "data.label", "reference": {"where": reference_where, "path": "data.s"}, "append": "-x"}, True),
        ]
        self.assertEqual({case["operator"] for case, _expected in cases}, set(assertion.CLAUDE_ASSERTION_OPERATORS))
        for case, expected in cases:
            with self.subTest(case=case):
                assertion._validate_assertion(case, "测试", ["transport.http"])
                passed, _actual = assertion._evaluate_assertion(
                    records, case, observations=records, default_scenarios=["transport.http"], record_type="claude_http_request"
                )
                self.assertIs(passed, expected)
        # 参照必须恰好一条：两条命中时不通过，并报告命中数。
        ambiguous = {"operator": "all_equal_reference", "path": "data.s", "reference": {"where": [{"path": "data.s", "operator": "equal", "value": "1"}]}}
        _passed, actual = assertion._evaluate_assertion(
            records, ambiguous, observations=records, default_scenarios=["transport.http"], record_type="claude_http_request"
        )
        self.assertEqual(actual["reference_count"], 2)
        # 参照比较按顶层键顺序：键序不同即不等（与 FW-F same_body_fields 同口径）。
        reordered = [self.observation(4, {"n": 1, "body": {"s": True, "m": 1}}), self.observation(5, {"n": 2, "body": {"m": 1, "s": True}})]
        case = {"operator": "all_equal_reference", "path": "data.body", "reference": {"where": [{"path": "data.n", "operator": "equal", "value": 2}]}}
        self.assertFalse(assertion._evaluate_assertion(reordered[:1], case, observations=reordered, default_scenarios=["transport.http"], record_type="claude_http_request")[0])
        # 参数闭集：缺参数、区间颠倒、比较符非法都在画像校验时拒绝。
        for broken, message in (
            ({"operator": "all_between", "path": "data.n", "minimum": 9, "maximum": 1}, "区间非法"),
            ({"operator": "all_fields_compare", "left_path": "data.n", "right_path": "data.n", "comparison": "lte"}, "comparison"),
            ({"operator": "all_distinct"}, "字段不闭合"),
            ({"operator": "all_equal_reference", "path": "data.n", "reference": {"scenario_ids": ["failure.529"]}}, "规则场景的子集"),
        ):
            with self.subTest(broken=broken), self.assertRaisesRegex(assertion.AssertionConfigurationError, message):
                assertion._validate_assertion(broken, "测试", ["transport.http"])
        # 长取值在结果文档里只显示摘要。
        long_value = "x" * (assertion.DISPLAY_LIMIT + 10)
        shown = assertion._display(long_value)
        self.assertEqual(set(shown["$summary"]), {"sha256", "length", "prefix"})
        self.assertEqual(assertion._display("short"), "short")


class CheckerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="claude-checker-cli-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        self.profile_path = self.root / "control" / "profile.json"
        self.profile_sha256 = fixture.write_profile(self.profile_path)
        self.rules_path = fixture.write_rule_manifest(self.root / "control" / "rules.json")
        self.official = fixture.write_side(self.root / "official", capture_id="eval-official")
        self.candidate = fixture.write_side(self.root / "candidate", capture_id="eval-candidate", anthropic_version="2023-01-01")
        self.profile = fixture.build_profile()

    def run_checker(self, side: fixture.SideEvidence, rule_id: str, output: Path, *, projection: Path | None = None, side_name: str | None = None) -> tuple[int, dict]:
        command = assertion.build_assertion_command(
            rule_id=rule_id,
            capture_manifest=str(side.manifest_path),
            evidence_root=str(side.evidence_root),
            output=str(output),
            profile=str(self.profile_path),
            rule_manifest=str(self.rules_path),
            expected_claude_version=fixture.TARGET_VERSION,
            expected_profile_sha256=self.profile_sha256,
            side=side_name,
            capture_manifest_projection=str(projection) if projection is not None else None,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = assertion.main(command[2:])
        return code, json.loads(output.read_text(encoding="utf-8"))

    def write_projection(self, side: fixture.SideEvidence, rule_id: str, name: str) -> Path:
        manifest = json.loads(side.manifest_path.read_text(encoding="utf-8"))
        projection = assertion.project_capture_manifest(self.profile, rule_id, manifest, side.evidence_root, fixture.TARGET_VERSION)
        path = self.root / name
        path.write_bytes(assertion.canonical_projection_bytes(projection))
        return path

    def test_command_builder_fixed_order(self) -> None:
        command = assertion.build_assertion_command(
            rule_id="SPEC-HDR-001", capture_manifest="m.json", evidence_root="/e", output="o.json", profile="p.json", rule_manifest="r.json",
            expected_claude_version="2.1.285", expected_profile_sha256="f" * 64, side="official", capture_manifest_projection="i.json",
        )
        self.assertEqual(
            command,
            [
                "python3", assertion.CHECKER_RELATIVE_PATH, "--rule-id", "SPEC-HDR-001", "--capture-manifest", "m.json", "--evidence-root", "/e",
                "--profile", "p.json", "--rule-manifest", "r.json", "--expected-claude-version", "2.1.285", "--expected-profile-sha256", "f" * 64,
                "--side", "official", "--capture-manifest-projection", "i.json", "--output", "o.json",
            ],
        )
        self.assertEqual(assertion.command_sha256(command), hashlib.sha256(json.dumps(command, separators=(",", ":")).encode("utf-8")).hexdigest())
        self.assertTrue((Path(assertion.__file__).resolve().parents[2] / assertion.CHECKER_RELATIVE_PATH).is_file())

    def test_pass_and_fail_documents(self) -> None:
        code, document = self.run_checker(self.official, "SPEC-HDR-001", self.root / "official-hdr.json", side_name="official")
        self.assertEqual((code, document["status"], document["exit_code"]), (0, "pass", 0))
        self.assertEqual(document["schema_version"], assertion.ASSERTION_SCHEMA_VERSION)
        self.assertEqual(document["checker_sha256"], _sha256(CHECKER))
        self.assertEqual([check["id"] for check in document["checks"]], contract.derive_expected_check_ids(self.profile)["SPEC-HDR-001"])
        self.assertNotIn("projection_sha256", document)
        code, document = self.run_checker(self.candidate, "SPEC-HDR-001", self.root / "candidate-hdr.json", side_name="candidate")
        self.assertEqual((code, document["status"]), (1, "fail"))
        failed = [check["id"] for check in document["checks"] if not check["passed"]]
        self.assertEqual(failed, ["spec-hdr-001.version-header"])
        version_check = next(check for check in document["checks"] if check["id"] == "spec-hdr-001.version-header")
        self.assertEqual(version_check["actual"]["values"], [["2023-01-01"]])
        # 请求记录的证据：请求字节，加上派生事实用到的配对响应、连接索引与中继清单（注入环境）。
        self.assertEqual(
            version_check["evidence_paths"],
            [
                "job-main/print/relay-manifest.json",
                "job-main/print/relay/conn001.client_to_upstream.bin",
                "job-main/print/relay/conn001.upstream_to_client.bin",
                "job-main/print/relay/relay.json",
            ],
        )

    def test_projection_mode_binds_digests(self) -> None:
        projection = self.write_projection(self.official, "SPEC-TLS-001", "tls-input.json")
        projected = json.loads(projection.read_text(encoding="utf-8"))
        # 投影只留与规则场景相交的 artifact。
        self.assertEqual([item["path"] for item in projected["artifacts"]], ["job-main/tls/tls-clienthello.pcap"])
        code, document = self.run_checker(self.official, "SPEC-TLS-001", self.root / "tls.json", projection=projection, side_name="official")
        self.assertEqual((code, document["status"]), (0, "pass"))
        self.assertEqual(document["projection_sha256"], _sha256(projection))
        self.assertEqual(document["capture_manifest_sha256"], _sha256(self.official.manifest_path))
        # 别的规则的投影：与完整 manifest 重算结果不一致，输入校验失败，文档不带投影摘要。
        wrong = self.write_projection(self.official, "SPEC-HDR-001", "hdr-input.json")
        code, document = self.run_checker(self.official, "SPEC-TLS-001", self.root / "tls-wrong.json", projection=wrong, side_name="official")
        self.assertEqual((code, [check["id"] for check in document["checks"]]), (1, ["input-validation"]))
        self.assertIn("projection-mismatch", document["checks"][0]["actual"]["error"])
        self.assertNotIn("projection_sha256", document)

    def test_input_failure_after_projection_keeps_digests(self) -> None:
        projection = self.write_projection(self.official, "SPEC-TLS-001", "tls-input.json")
        pcap = self.official.evidence_root / "job-main" / "tls" / "tls-clienthello.pcap"
        pcap.write_bytes(pcap.read_bytes() + b"\x00")
        code, document = self.run_checker(self.official, "SPEC-TLS-001", self.root / "tls.json", projection=projection)
        self.assertEqual((code, document["checks"][0]["id"]), (1, "input-validation"))
        self.assertIn("字节数与绑定不一致", document["checks"][0]["actual"]["error"])
        # 投影已核对通过：两项摘要保留（与 Codex 同一口径）。
        self.assertEqual(document["projection_sha256"], _sha256(projection))

    def test_side_restriction_skips_registered_check(self) -> None:
        manifest, observations = assertion.load_observations(self.official.manifest_path, self.official.evidence_root, fixture.TARGET_VERSION)
        table = {("SPEC-TLS-001", "spec-tls-001.no-alpn"): ("candidate",)}
        with mock.patch.object(contract, "SIDE_RESTRICTED_CHECKS", table):
            official = assertion.evaluate_rule(self.profile, "SPEC-TLS-001", observations, manifest, "official")
            candidate = assertion.evaluate_rule(self.profile, "SPEC-TLS-001", observations, manifest, "candidate")
        self.assertEqual([check["id"] for check in official], ["scenario-artifact-coverage", "spec-tls-001.sni"])
        self.assertEqual(len(candidate), 3)
        self.assertEqual(len(assertion.evaluate_rule(self.profile, "SPEC-TLS-001", observations, manifest)), 3)

    def test_coverage_check_fails_when_required_kind_missing(self) -> None:
        manifest = json.loads(self.official.manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"] = [item for item in manifest["artifacts"] if item["kind"] != "synthetic_intervention"]
        trimmed = self.root / "trimmed.json"
        trimmed.write_text(json.dumps(manifest), encoding="utf-8")
        loaded, observations = assertion.load_observations(trimmed, self.official.evidence_root, fixture.TARGET_VERSION)
        checks = assertion.evaluate_rule(self.profile, "SPEC-FAIL-001", observations, loaded)
        coverage = checks[0]
        self.assertEqual((coverage["id"], coverage["passed"]), (contract.COVERAGE_CHECK_ID, False))
        self.assertEqual(coverage["actual"]["failure.529"], ["relay_binary", "relay_index", "relay_manifest", "relay_response"])


if __name__ == "__main__":
    unittest.main()
