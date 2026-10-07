"""Claude 请求账务计量（Claude 升级控制面设计 B1-11）。

覆盖：keep-alive 连续请求按 Content-Length／chunked 严格切分；计量单位只算 ``api.anthropic.com`` 的 POST 模型端点，
token refresh 记辅助、GET／HEAD 不计；relay 与 capture 两类证据根的三态；续跑取代后的世代并入身份键；根名相同的
录制目录不撞键、真撞键失败关闭；Campaign 级核算的收据形态能被共享对账器的 ``request_accounting`` 直接消费。
设置 ``CLAUDE_2_1_226_RELAY_FIXTURE`` 指向 2.1.226 complete-v21 录制目录时，另验收 messages 123、count_tokens 36、
token-refresh 1。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_reconciler as reconciler

FIXTURE_ENV = "CLAUDE_2_1_226_RELAY_FIXTURE"


def _request(method: str, target: str, host: str, body: bytes = b"", *, chunked: bool = False) -> bytes:
    headers = [f"{method} {target} HTTP/1.1", f"Host: {host}", "Connection: keep-alive"]
    if chunked:
        headers.append("Transfer-Encoding: chunked")
        payload = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n" if body else b"0\r\n\r\n"
    else:
        headers.append(f"Content-Length: {len(body)}")
        payload = body
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + payload


def _messages(model: str = "claude-opus-4-5") -> bytes:
    return _request("POST", "/v1/messages?beta=true", "api.anthropic.com", json.dumps({"model": model, "messages": []}).encode())


def _write(path: Path, raw: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw.encode() if isinstance(raw, str) else raw)


def _relay_root(root: Path, connections: dict[int, bytes], *, manifest: dict[str, Any] | None = None) -> Path:
    for connection_id, raw in connections.items():
        _write(root / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin", raw)
    payload = manifest if manifest is not None else {
        "schema_version": provenance.RELAY_MANIFEST_SCHEMA,
        "production_forwarding_enabled": True,
        "connections": [{"connection_id": key, "valid": True} for key in connections],
    }
    _write(root / "relay" / "relay.json", json.dumps(payload))
    return root


class HttpSplitTests(unittest.TestCase):
    def test_keep_alive_requests_without_line_breaks(self) -> None:
        raw = _messages() + _request("POST", "/v1/messages/count_tokens?beta=true", "api.anthropic.com", b'{"x":1}') + _request(
            "HEAD", "/api/hello", "api.anthropic.com"
        )
        requests = provenance.split_http_requests(raw)
        self.assertEqual([(item["method"], item["path"]) for item in requests], [
            ("POST", "/v1/messages"), ("POST", "/v1/messages/count_tokens"), ("HEAD", "/api/hello"),
        ])
        self.assertEqual(provenance._body_model(requests[0]["body"], ""), "claude-opus-4-5")

    def test_chunked_body(self) -> None:
        raw = _request("POST", "/v1/messages", "api.anthropic.com:443", b'{"model":"m"}', chunked=True) + _messages()
        requests = provenance.split_http_requests(raw)
        self.assertEqual(len(requests), 2)
        self.assertEqual((requests[0]["host"], requests[0]["body"]), ("api.anthropic.com", b'{"model":"m"}'))

    def test_malformed_streams_fail_closed(self) -> None:
        cases = {
            "缺少 Header 结束": b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n",
            "请求行非法": b"garbage\r\n\r\n",
            "越过流结尾": b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\nContent-Length: 99\r\n\r\n{}",
            "重复 Content-Length": b"POST /v1/messages HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
            "升级后仍有字节": b"GET /ws HTTP/1.1\r\nUpgrade: websocket\r\n\r\n\x81\x00",
        }
        for label, raw in cases.items():
            with self.subTest(label=label), self.assertRaises(provenance._StreamError):
                provenance.split_http_requests(raw)

    def test_endpoint_categories(self) -> None:
        self.assertEqual(provenance.endpoint_category("api.anthropic.com", "POST", "/v1/messages"), "model")
        self.assertEqual(provenance.endpoint_category("api.anthropic.com", "POST", "/v1/messages/batches"), "model")
        self.assertEqual(provenance.endpoint_category("platform.claude.com", "POST", "/v1/oauth/token"), "token_refresh")
        for host, method, path in (
            ("api.anthropic.com", "GET", "/v1/messages"),
            ("api.anthropic.com", "HEAD", "/api/hello"),
            ("api.anthropic.com", "GET", "/api/claude_code/settings"),
            ("example.com", "POST", "/v1/messages"),
        ):
            self.assertIsNone(provenance.endpoint_category(host, method, path))


class EvidenceRootTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()

    def test_relay_counts_model_and_auxiliary(self) -> None:
        root = _relay_root(self.base / "run-a", {
            1: _request("HEAD", "/api/hello", "api.anthropic.com") + _messages() + _messages("claude-haiku-4-5"),
            2: _request("POST", "/v1/messages/count_tokens?beta=true", "api.anthropic.com", b"{}"),
            3: _request("POST", "/v1/oauth/token", "platform.claude.com", b"grant_type=refresh_token"),
        })
        kind, branches = provenance.root_branches(root, producer_run_id="run-a")
        branch = branches[0]
        self.assertEqual((kind, branch["status"], branch["production_forwarding_enabled"]), ("relay", "resolved", True))
        self.assertEqual([item["endpoint"] for item in branch["requests"]], ["/v1/messages", "/v1/messages", "/v1/messages/count_tokens"])
        self.assertEqual([item["model"] for item in branch["requests"]], ["claude-opus-4-5", "claude-haiku-4-5", None])
        self.assertEqual([item["category"] for item in branch["auxiliary_requests"]], ["token_refresh"])
        first = branch["requests"][0]
        self.assertEqual(first["native_coordinate"], {
            "connection": "conn001", "transport": "http", "message_ordinal": 2, "direction": "client_to_upstream",
        })
        self.assertEqual(first["identity_key"], provenance.identity_key("run-a", "claude_relay", first["native_coordinate"]))

    def test_relay_unresolved_and_zero_connection_states(self) -> None:
        missing = _relay_root(self.base / "missing", {1: _messages()}, manifest={
            "schema_version": provenance.RELAY_MANIFEST_SCHEMA,
            "connections": [{"connection_id": 1, "valid": True}, {"connection_id": 2, "valid": True}],
        })
        self.assertEqual(provenance.root_branches(missing, producer_run_id="m")[1][0]["status"], "unresolved")
        broken = _relay_root(self.base / "broken", {1: _messages() + b"garbage"})
        branch = provenance.root_branches(broken, producer_run_id="b")[1][0]
        self.assertEqual((branch["status"], branch["requests"]), ("unresolved", []))
        empty = _relay_root(self.base / "empty", {}, manifest={"schema_version": provenance.RELAY_MANIFEST_SCHEMA, "connections": []})
        branch = provenance.root_branches(empty, producer_run_id="e")[1][0]
        self.assertEqual((branch["status"], branch["authority_source"]), ("resolved", "relay_zero_connections"))
        invalid_only = _relay_root(self.base / "invalid", {}, manifest={
            "schema_version": provenance.RELAY_MANIFEST_SCHEMA, "connections": [{"connection_id": 1, "valid": False}],
        })
        self.assertEqual(provenance.root_branches(invalid_only, producer_run_id="i")[1][0]["status"], "unresolved")

    def test_superseded_generation_changes_keys(self) -> None:
        root = _relay_root(self.base / "run-a", {1: _messages()})
        before = provenance.collect_root_provenance([root])
        (self.base / f"run-a{provenance.SUPERSEDED_ROOT_INFIX}attempt-2").mkdir()
        self.assertEqual(provenance._superseded_generation(root), 1)
        kind, branches = provenance.root_branches(root, producer_run_id="run-a", generation=1)
        key = branches[0]["requests"][0]["identity_key"]
        self.assertEqual(key, provenance.identity_key("run-a", "claude_relay", branches[0]["requests"][0]["native_coordinate"], supersession_generation=1))
        self.assertNotEqual(key, provenance.root_branches(root, producer_run_id="run-a")[1][0]["requests"][0]["identity_key"])
        self.assertEqual(before["precise_total"], 1)

    def test_same_named_recording_roots_do_not_collide(self) -> None:
        roots = [_relay_root(self.base / "attempts" / probe / "attempt-001", {1: _messages()}) for probe in ("p1", "p2")]
        summary = provenance.collect_root_provenance(roots)
        self.assertEqual(summary["precise_total"], 2)
        self.assertEqual([item["producer_run_id"] for item in summary["roots"]], ["p1/attempt-001", "p2/attempt-001"])
        with self.assertRaises(provenance.ProvenanceError):
            provenance._producer_run_ids(roots, roots[0].parent)

    def test_capture_mitm_and_direct_estimation(self) -> None:
        root = self.base / "capture-run"
        _write(root / "manifest.json", json.dumps({
            "schema_version": provenance.CAPTURE_MANIFEST_SCHEMA,
            "case_results": [
                {"evidence": "direct", "subject": "oauth", "scenario": "s1"},
                {"evidence": "mitm", "subject": "oauth", "scenario": "s1"},
                {"evidence": "direct", "subject": "oauth", "scenario": "s2"},
            ],
        }))
        mitm = root / "mitm" / "oauth" / "s1"
        _write(mitm / "claude-http.jsonl", "\n".join(json.dumps(item) for item in [
            {"_flow_id": "f1", "request": {"method": "POST", "host": "api.anthropic.com", "path": "/v1/messages?beta=true", "body": {"json": {"model": "m"}}}},
            {"_flow_id": "f2", "request": {"method": "POST", "host": "api.anthropic.com", "path": "/v1/messages/count_tokens?beta=true", "body": {"text": "{}"}}},
        ]) + "\n")
        _write(mitm / "misc-http.jsonl", json.dumps(
            {"_flow_id": "f3", "request": {"method": "POST", "host": "platform.claude.com", "path": "/v1/oauth/token"}}
        ) + "\n")
        none = provenance.collect_root_provenance([root])
        self.assertEqual((none["precise_total"], none["estimated_total"], none["auxiliary_by_category"]), (2, 0, {"token_refresh": 1}))
        self.assertEqual(none["unresolved_roots"], [str(root)])
        sibling = provenance.collect_root_provenance([root], estimation_policy="upper_bound_from_sibling")
        self.assertEqual(sibling["estimated_total"], 2)
        self.assertEqual(sibling["roots"][0]["branch_statuses"], ["estimated", "resolved", "unresolved"])


LIVE_EMPTY_HANDSHAKE = "live_client_closed_tls_handshake_before_application_data"
CLIENT_RESET = "client_reset_transport_before_tls_handshake"


def _connection_record(connection_id: int, raw: bytes | None, *, valid: Any = True) -> dict[str, Any]:
    """中继 ``relay.json`` 的连接记录：有客户端字节时记字节数、摘要与分段；``raw`` 为 None 时是零字节握手形态。

    零字节形态与中继写出的 live 空握手逐字段相同（``bytes``／``sha256`` 为空对象、``segments`` 为空数组），
    这种连接在 ``relay.json`` 里记 ``valid: true``，但没有任何客户端字节文件。
    """

    record: dict[str, Any] = {
        "connection_id": connection_id,
        "client_alpn_offer": ["http/1.1"],
        "alpn_source": "assumed",
        "client_alpn": None,
        "sni": "api.anthropic.com",
        "valid": valid,
        "opened_at_unix_ms": 1,
        "closed_at_unix_ms": 2,
    }
    if raw is None:
        record.update({"upstream_alpn_offer": None, "upstream_alpn": None, "bytes": {}, "sha256": {}, "segments": []})
    else:
        record.update(
            {
                "client_alpn": "http/1.1",
                "upstream_alpn": "http/1.1",
                "bytes": {"client_to_upstream": len(raw)},
                "sha256": {"client_to_upstream": hashlib.sha256(raw).hexdigest()},
                "segments": [{"direction": "client_to_upstream", "offset": 0, "length": len(raw)}],
            }
        )
    return record


def _fw_e_root(
    root: Path,
    connections: dict[int, bytes | None],
    *,
    excluded: dict[int, str] | None = None,
    valid: dict[int, Any] | None = None,
    records: dict[int, dict[str, Any]] | None = None,
    run_manifest: dict[str, Any] | None | bool = True,
) -> Path:
    """FW-E 中继运行根（probe 目录）：``relay/`` 字节与连接索引，加运行根上的中继清单 ``relay-manifest.json``。

    ``excluded`` 是中继完整性登记的握手前断开连接（连接号 → 原因）；``run_manifest`` 为 False 时不写中继清单，
    给出字典时原样写入（用来造非法清单）。
    """

    rows = []
    for connection_id, raw in connections.items():
        if raw is not None:
            _write(root / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin", raw)
        row = _connection_record(connection_id, raw, valid=(valid or {}).get(connection_id, True))
        row.update((records or {}).get(connection_id, {}))
        rows.append(row)
    _write(root / "relay" / "relay.json", json.dumps({"schema_version": provenance.RELAY_MANIFEST_SCHEMA, "connections": rows}))
    if run_manifest is True:
        run_manifest = {
            "schema_version": provenance.RELAY_RUN_MANIFEST_SCHEMA,
            "relay_integrity": {
                "result": "passed",
                "excluded_connections": [
                    {"connection_id": key, "reason": reason, "manifest_error": None} for key, reason in (excluded or {}).items()
                ],
            },
        }
    if isinstance(run_manifest, dict):
        _write(root / "relay-manifest.json", json.dumps(run_manifest))
    return root


class RelayHandshakeExclusionTests(unittest.TestCase):
    """中继登记的握手前断开连接（含 relay.json 记 ``valid: true`` 的 live 空握手）不再让证据根未决，且逐条留证。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()

    def branch(self, root: Path) -> dict[str, Any]:
        kind, branches = provenance.root_branches(root, producer_run_id=root.name)
        self.assertEqual(kind, "relay")
        return branches[0]

    def test_live_empty_handshake_excluded_by_relay_manifest(self) -> None:
        """复现：live 空握手（valid 为真、零字节、无字节文件）曾让整个证据根未决；按中继清单的排除登记剔除后照常计数。"""

        root = _fw_e_root(
            self.base / "v4-background",
            {1: _messages(), 2: None, 3: None, 4: _messages("claude-haiku-4-5")},
            excluded={2: LIVE_EMPTY_HANDSHAKE, 3: CLIENT_RESET},
            valid={3: False},
        )
        branch = self.branch(root)
        self.assertEqual(branch["status"], "resolved", branch.get("reason"))
        self.assertEqual([item["native_coordinate"]["connection"] for item in branch["requests"]], ["conn001", "conn004"])
        self.assertEqual(
            branch["zero_client_byte_connections"],
            [
                {"connection_id": 2, "valid": True, "classification": "relay_excluded", "reason": LIVE_EMPTY_HANDSHAKE},
                {"connection_id": 3, "valid": False, "classification": "relay_excluded", "reason": CLIENT_RESET},
            ],
        )
        summary = provenance.collect_root_provenance([root])
        self.assertEqual((summary["precise_total"], summary["unresolved_roots"]), (2, []))
        self.assertEqual(summary["roots"][0]["zero_client_byte_connections"], {"relay_excluded": 2})

    def test_unexcluded_zero_byte_connection_counts_zero_and_is_recorded(self) -> None:
        """不在排除清单里、valid 为真且零字节：没有客户端字节就没有请求，计零并逐条留证，不悄悄丢掉。"""

        for label, run_manifest in (("中继清单没有登记", True), ("没有中继清单", False)):
            with self.subTest(label=label):
                root = _fw_e_root(self.base / label, {1: _messages(), 2: None}, run_manifest=run_manifest)
                branch = self.branch(root)
                self.assertEqual((branch["status"], len(branch["requests"])), ("resolved", 1))
                self.assertEqual(
                    branch["zero_client_byte_connections"],
                    [{"connection_id": 2, "valid": True, "classification": "unexcluded", "reason": None}],
                )
                summary = provenance.collect_root_provenance([root])
                self.assertEqual(summary["roots"][0]["zero_client_byte_connections"], {"unexcluded": 1})

    def test_handshake_only_root_resolves_to_zero_requests(self) -> None:
        root = _fw_e_root(self.base / "handshake-only", {1: None, 2: None}, excluded={1: LIVE_EMPTY_HANDSHAKE}, valid={2: False})
        branch = self.branch(root)
        self.assertEqual((branch["status"], branch["authority_source"], branch["requests"]), ("resolved", "relay_zero_client_bytes", []))
        self.assertEqual([item["classification"] for item in branch["zero_client_byte_connections"]], ["relay_excluded", "unexcluded"])

    def test_contradictions_stay_unresolved(self) -> None:
        cases = {
            # 排除登记的连接却有客户端字节：中继清单与字节互相矛盾，不能据清单剔除，也不能只信字节。
            "排除登记的连接有客户端字节": ({1: _messages()}, {1: LIVE_EMPTY_HANDSHAKE}, {}, "存在客户端字节"),
            # 排除登记了连接索引里没有的连接号。
            "排除登记不在连接索引": ({1: _messages()}, {7: LIVE_EMPTY_HANDSHAKE}, {}, "不在 relay.json"),
            # 排除登记了，但 relay.json 声明了非零客户端字节（字节文件丢失）。
            "排除登记但声明了字节": (
                {1: _messages(), 2: None}, {2: LIVE_EMPTY_HANDSHAKE}, {2: {"bytes": {"client_to_upstream": 10}}}, "缺少客户端原始字节"
            ),
            # 没有排除登记、valid 为真，声明了非零字节却没有文件：仍是缺字节。
            "未登记且声明了字节": ({1: _messages(), 2: None}, {}, {2: {"bytes": {"client_to_upstream": 10}}}, "缺少客户端原始字节"),
            # 只有上游方向的字节文件：零客户端字节却有响应，形态异常。
            "零客户端字节却有上游字节": ({1: _messages(), 2: None}, {2: LIVE_EMPTY_HANDSHAKE}, {}, "上游字节"),
            # 未登记的排除原因：中继新增原因时计量必须同步登记，失败关闭。
            "未知排除原因": ({1: _messages(), 2: None}, {2: "new_reason_from_future_relay"}, {}, "未登记的握手前断开原因"),
        }
        for index, (label, (connections, excluded, records, message)) in enumerate(cases.items()):
            with self.subTest(label=label):
                root = _fw_e_root(self.base / f"case-{index}", connections, excluded=excluded, records=records)
                if label == "零客户端字节却有上游字节":
                    _write(root / "relay" / "conn002.upstream_to_client.bin", b"HTTP/1.1 200 OK\r\n\r\n")
                branch = self.branch(root)
                self.assertEqual(branch["status"], "unresolved", label)
                self.assertIn(message, branch["reason"])

    def test_invalid_run_manifest_fails_closed(self) -> None:
        cases = {
            "schema 不符": {"schema_version": "byte-relay/v1", "relay_integrity": None},
            "排除记录不是对象": {"schema_version": provenance.RELAY_RUN_MANIFEST_SCHEMA, "relay_integrity": {"result": "passed", "excluded_connections": [7]}},
            "排除记录缺原因": {
                "schema_version": provenance.RELAY_RUN_MANIFEST_SCHEMA,
                "relay_integrity": {"result": "passed", "excluded_connections": [{"connection_id": 2}]},
            },
            "排除连接号重复": {
                "schema_version": provenance.RELAY_RUN_MANIFEST_SCHEMA,
                "relay_integrity": {
                    "result": "passed",
                    "excluded_connections": [
                        {"connection_id": 2, "reason": LIVE_EMPTY_HANDSHAKE}, {"connection_id": 2, "reason": LIVE_EMPTY_HANDSHAKE},
                    ],
                },
            },
        }
        for index, (label, manifest) in enumerate(cases.items()):
            with self.subTest(label=label):
                root = _fw_e_root(self.base / f"bad-{index}", {1: _messages(), 2: None}, run_manifest=manifest)
                with self.assertRaises(provenance.ProvenanceError):
                    provenance.root_branches(root, producer_run_id=root.name)

    def test_failed_relay_run_does_not_supply_exclusions(self) -> None:
        """中继完整性没有通过（失败 run 的 relay_integrity 为 null）时没有排除登记，零字节连接按未登记处理。"""

        root = _fw_e_root(
            self.base / "failed-run",
            {1: _messages(), 2: None},
            run_manifest={"schema_version": provenance.RELAY_RUN_MANIFEST_SCHEMA, "status": "failed", "relay_integrity": None},
        )
        branch = self.branch(root)
        self.assertEqual(branch["status"], "resolved")
        self.assertEqual([item["classification"] for item in branch["zero_client_byte_connections"]], ["unexcluded"])

    def test_exclusion_reasons_match_relay_constants(self) -> None:
        from tools.official_client_capture import claude_fw_e_relay as relay

        self.assertEqual(
            provenance.RELAY_HANDSHAKE_EXCLUSION_REASONS,
            {
                relay.EMPTY_HANDSHAKE_TERMINATION_REASON,
                relay.RELAY_SHUTDOWN_HANDSHAKE_TERMINATION_REASON,
                relay.CLIENT_RESET_HANDSHAKE_TERMINATION_REASON,
                relay.LIVE_EMPTY_HANDSHAKE_TERMINATION_REASON,
            },
        )
        self.assertEqual(provenance.RELAY_RUN_MANIFEST_SCHEMA, relay.SCHEMA_RUN)


class CampaignProvenanceTests(unittest.TestCase):
    CAMPAIGN_ID = "claude-campaign-test"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve() / "data"
        self.campaign = self.data / "evidence" / "campaigns" / self.CAMPAIGN_ID
        self.captures = self.data / "captures"
        self.relay = _relay_root(self.captures / "official-relay-s1", {1: _messages() + _messages(), 2: _request(
            "POST", "/v1/oauth/token", "platform.claude.com", b"x"
        )})
        _write(self.captures / "official-unsupported" / "note.txt", "无权威来源")

    def _campaign(self, jobs: list[dict[str, Any]], *, client: str = "claude") -> None:
        manifest = {
            "schema_version": provenance.CAMPAIGN_SCHEMA,
            "client": client,
            "campaign_id": self.CAMPAIGN_ID,
            "campaign_mode": "formal",
            "configuration": {"platform": "linux/arm64"},
            "jobs": jobs,
        }
        raw = json.dumps(manifest).encode()
        _write(self.campaign / "campaign.json", raw)
        _write(self.campaign / "campaign.sha256", hashlib.sha256(raw).hexdigest() + "\n")

    def _attempt(self, job_id: str, roots: list[Path]) -> None:
        attempt = self.campaign / "official" / "attempts" / "a1"
        _write(attempt / "reservation.json", "{}")
        _write(attempt / f"job-{job_id}.json", json.dumps({"id": job_id, "evidence_roots": [str(root) for root in roots]}))

    def test_campaign_receipt_feeds_shared_reconciler(self) -> None:
        self._campaign([
            {"id": "official-relay-s1", "phase": "official", "evidence_roots": [str(self.relay)]},
            {"id": "official-unsupported", "phase": "official", "evidence_roots": []},
            {"id": "official-pending", "phase": "official", "evidence_roots": []},
            {"id": "candidate-relay", "phase": "candidate", "evidence_roots": []},
        ])
        self._attempt("official-relay-s1", [self.relay])
        self._attempt("official-unsupported", [self.captures / "official-unsupported"])
        receipt = provenance.collect_campaign_provenance(self.campaign, formal_campaign_id=self.CAMPAIGN_ID, observed_at_utc="2026-10-07T00:00:00Z")
        self.assertEqual((receipt["schema_version"], receipt["client"], receipt["status"]), ("live-request-provenance/v2", "claude", "accounting_unresolved"))
        self.assertEqual((receipt["precise_total"], receipt["auxiliary_total"]), (2, 1))
        self.assertEqual(receipt["unresolved_job_ids"], ["official-unsupported"])
        self.assertEqual(receipt["pending_job_ids"], ["official-pending"])
        self.assertNotIn("candidate-relay", [job["job_id"] for job in receipt["jobs"]])
        self.assertEqual({item["producer_run_id"] for item in receipt["requests"]}, {"captures/official-relay-s1"})
        accounting = reconciler.request_accounting(receipt, head={}, initial_keys=set(), campaign_id=self.CAMPAIGN_ID)
        self.assertEqual(len(accounting["new_keys"]), 2)
        self.assertEqual(accounting["unresolved_job_ids"], ["official-unsupported"])
        again = provenance.collect_campaign_provenance(self.campaign, formal_campaign_id=self.CAMPAIGN_ID, observed_at_utc="2026-10-07T00:00:00Z")
        self.assertEqual(again, receipt)

    def test_campaign_identity_is_checked(self) -> None:
        self._campaign([], client="codex")
        with self.assertRaisesRegex(provenance.ProvenanceError, "身份不一致"):
            provenance.collect_campaign_provenance(self.campaign, formal_campaign_id=self.CAMPAIGN_ID)
        self._campaign([])
        empty = provenance.collect_campaign_provenance(self.campaign, formal_campaign_id=self.CAMPAIGN_ID)
        self.assertEqual((empty["status"], empty["precise_total"]), ("complete", 0))
        with self.assertRaisesRegex(provenance.ProvenanceError, "估计政策非法"):
            provenance.collect_campaign_provenance(self.campaign, formal_campaign_id=self.CAMPAIGN_ID, estimation_policy="guess")


@unittest.skipUnless(os.environ.get(FIXTURE_ENV), f"未设置 {FIXTURE_ENV}（2.1.226 complete-v21 录制目录）")
class RecordedRelayFixtureTests(unittest.TestCase):
    def test_complete_v21_counts(self) -> None:
        campaign = Path(os.environ[FIXTURE_ENV]).resolve()
        roots = sorted((campaign / "attempts").glob("*/attempt-001"))
        summary = provenance.collect_root_provenance(roots)
        self.assertEqual(len(roots), 77)
        self.assertEqual(summary["unresolved_roots"], [])
        self.assertEqual(summary["model_requests_by_endpoint"], {"/v1/messages": 123, "/v1/messages/count_tokens": 36})
        self.assertEqual(summary["auxiliary_by_category"], {"token_refresh": 1})


if __name__ == "__main__":
    unittest.main()
