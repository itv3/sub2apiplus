"""逐请求 provenance v2：统一计量单位、估计政策、身份键去重与来源唯一性。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import (
    codex_upgrade_live_request_provenance as provenance,
)

RESPONSES = "/backend-api/codex/responses"


def _chmod_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")


def _h1_post(path: str, body: dict) -> bytes:
    payload = json.dumps(body).encode("utf-8")
    head = (
        f"POST {path} HTTP/1.1\r\nHost: chatgpt.com\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
    ).encode("latin-1")
    return head + payload


def _h1_get(path: str) -> bytes:
    return f"GET {path} HTTP/1.1\r\nHost: chatgpt.com\r\n\r\n".encode("latin-1")


def _ws_client_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length])
    else:
        header = bytes([0x81, 0x80 | 126]) + length.to_bytes(2, "big")
    return header + mask + masked


def _ws_connection(messages: list[dict]) -> bytes:
    handshake = (
        f"GET {RESPONSES} HTTP/1.1\r\nHost: chatgpt.com\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\n\r\n"
    ).encode("latin-1")
    return handshake + b"".join(_ws_client_frame(json.dumps(m)) for m in messages)


def _addon_body(payload: dict | None) -> dict:
    """按 addons/mitm_capture._body_summary 的真实格式构造请求正文摘要。

    真实记录里模型只在 body.json / body.text 内，body 顶层没有 model 字段。
    """

    text = json.dumps(payload) if payload is not None else ""
    raw = text.encode("utf-8")
    return {
        "length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_encoding": "",
        "decoded_length": len(raw),
        "decoded_sha256": hashlib.sha256(raw).hexdigest(),
        "decode_error": "",
        "text": text,
        "json": payload,
    }


def _mitm_http_row(
    run_id: str,
    subject: str,
    scenario: str,
    method: str,
    path: str,
    model: str | None = None,
    *,
    payload: dict | None = None,
) -> dict:
    if payload is None and model is not None:
        payload = {"model": model}
    return {
        "_run_id": run_id,
        "_subject": subject,
        "_scenario": scenario,
        "_flow_id": f"flow-{method}-{path[-8:]}",
        "request": {
            "method": method,
            "scheme": "https",
            "host": "chatgpt.com",
            "port": 443,
            "path": path,
            "body": _addon_body(payload),
        },
        "response": {"status": 200},
    }


def _mitm_ws_row(run_id: str, subject: str, scenario: str, from_client: bool, payload: dict) -> dict:
    return {
        "_run_id": run_id,
        "_subject": subject,
        "_scenario": scenario,
        "_websocket": True,
        "from_client": from_client,
        "path": RESPONSES,
        "json": payload,
        "text": json.dumps(payload),
    }


def _turn_events(path: Path, completed: int) -> None:
    rows = [{"type": "thread.started"}]
    for _ in range(completed):
        rows.append({"type": "turn.started"})
        rows.append({"type": "turn.completed"})
    _write_jsonl(path, rows)


class ProvenanceFixture:
    """一个宿主数据根，含 capture、compact、relay 三类证据与一个 Formal Campaign。"""

    def __init__(self, root: Path, campaign_id: str = "c1") -> None:
        self.root = root
        self.data = root / "data"
        self.runs = self.data / "runs"
        self.campaign_id = campaign_id
        self.campaign_dir = self.data / "evidence" / "campaigns" / campaign_id

    def build(self, *, capture_turns_over_requests: bool = False) -> None:
        # capture 类：mitm 分支 codex-http/s4 一个 turn 两个请求；codex-ws/s1 一个 turn 两个 response.create。
        capture = self.runs / "oauth-run"
        cases = []
        for evidence in ("mitm", "direct"):
            cases.append({"evidence": evidence, "subject": "codex-http", "scenario": "s4", "scenario_result": {"turn_count": 1}})
            cases.append({"evidence": evidence, "subject": "codex-ws", "scenario": "s1", "scenario_result": {"turn_count": 1}})
            _turn_events(capture / "results" / evidence / "codex-http" / "s4" / "turn1-events.jsonl", 3 if capture_turns_over_requests else 1)
            _turn_events(capture / "results" / evidence / "codex-ws" / "s1" / "turn1-events.jsonl", 1)
        _write_json(capture / "manifest.json", {"schema_version": "official-client-capture/v1", "case_results": cases})
        _write_jsonl(
            capture / "mitm" / "codex-http" / "s4" / "codex-http.jsonl",
            [
                _mitm_http_row("oauth-run", "codex-http", "s4", "GET", "/backend-api/models"),
                _mitm_http_row("oauth-run", "codex-http", "s4", "POST", RESPONSES, "gpt-5.5"),
                _mitm_http_row("oauth-run", "codex-http", "s4", "POST", RESPONSES + "?x=1", "gpt-5.5"),
            ],
        )
        _write_jsonl(
            capture / "mitm" / "codex-ws" / "s1" / "codex-ws.jsonl",
            [
                _mitm_ws_row("oauth-run", "codex-ws", "s1", True, {"type": "response.create", "model": "gpt-6-astra"}),
                _mitm_ws_row("oauth-run", "codex-ws", "s1", False, {"type": "response.created"}),
                _mitm_ws_row("oauth-run", "codex-ws", "s1", True, {"type": "response.create", "model": "gpt-6-astra"}),
                _mitm_ws_row("oauth-run", "codex-ws", "s1", True, {"type": "codex.ping"}),
            ],
        )
        # direct-only 的 ws-repeat 根：没有 mitm 分支，只能靠倍率估计。
        repeat = self.runs / "oauth-run-ws-repeat"
        repeat_cases = [
            {"evidence": "direct", "subject": "codex-ws", "scenario": "s1", "scenario_result": {"turn_count": 1}},
            {"evidence": "direct", "subject": "codex-ws", "scenario": "s2", "scenario_result": {"turn_count": 2}},
        ]
        _write_json(repeat / "manifest.json", {"schema_version": "official-client-capture/v1", "case_results": repeat_cases})
        _turn_events(repeat / "results" / "direct" / "codex-ws" / "s1" / "turn1-events.jsonl", 1)
        _turn_events(repeat / "results" / "direct" / "codex-ws" / "s2" / "turn1-events.jsonl", 1)
        _turn_events(repeat / "results" / "direct" / "codex-ws" / "s2" / "turn2-events.jsonl", 1)
        # compact 类：两个分支各 2 个 turn，mitm 分支 2 个 POST。
        compact = self.runs / "c-compact"
        for evidence in ("mitm", "direct"):
            _write_json(compact / "result" / evidence / "summary.json", {"schema_version": "codex-compact-capture/v1", "turn_completed_count": 2})
        _write_jsonl(
            compact / "mitm" / "codex-compact" / "codex-http.jsonl",
            [
                _mitm_http_row("c-compact", "codex-compact", "", "POST", RESPONSES + "/compact", "gpt-5.5"),
                _mitm_http_row("c-compact", "codex-compact", "", "POST", RESPONSES, "gpt-5.5"),
            ],
        )
        # 请求前失败的 compact attempt：turn 0，只有 direct 分支与 pcap。
        failed = self.runs / "c-compact-fail.failed-attempt1"
        _write_json(
            failed / "result" / "direct" / "summary.json",
            {"schema_version": "codex-compact-capture/v1", "turn_completed_count": 0, "protocol_record_count": 0, "error_type": "RuntimeError"},
        )
        (failed / "direct" / "codex-compact").mkdir(parents=True, mode=0o700)
        (failed / "direct" / "codex-compact" / "egress.pcap").write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 20)
        # relay 类：conn001 两个 HTTP POST 加一个 GET；conn002 WS 两个 response.create 加一个其他。
        relay = self.runs / "c-relay" / "relay"
        relay.mkdir(parents=True, mode=0o700)
        (relay / "conn001.client_to_upstream.bin").write_bytes(
            _h1_get("/backend-api/models") + _h1_post(RESPONSES, {"model": "gpt-5.5"}) + _h1_post(RESPONSES, {"model": "gpt-5.5"})
        )
        (relay / "conn002.client_to_upstream.bin").write_bytes(
            _ws_connection([
                {"type": "response.create", "model": "gpt-6-astra"},
                {"type": "codex.ping"},
                {"type": "response.create", "model": "gpt-6-astra"},
            ])
        )
        manifest = {
            "campaign_id": self.campaign_id,
            "campaign_mode": "formal",
            "target_version": "0.154.0",
            "created_at_utc": "2026-09-14T23:14:19Z",
            "configuration": {"capture_root": "/capture"},
            "jobs": [
                {"id": "official-core", "phase": "official", "evidence_roots": ["/capture/runs/oauth-run"]},
                {"id": "official-ws-handshake-repeat", "phase": "official", "evidence_roots": ["/capture/runs/oauth-run-ws-repeat"]},
                {"id": "official-compact", "phase": "official", "evidence_roots": ["/capture/runs/c-compact"]},
                {"id": "official-compact-failed", "phase": "official", "evidence_roots": ["/capture/runs/c-compact-fail"]},
                {"id": "official-relay-ws", "phase": "official", "evidence_roots": ["/capture/runs/c-relay"]},
                {"id": "official-pending", "phase": "official", "evidence_roots": ["/capture/runs/missing"]},
                {"id": "candidate-x", "phase": "candidate", "evidence_roots": []},
            ],
        }
        raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.campaign_dir.mkdir(parents=True, mode=0o700)
        (self.campaign_dir / "campaign.json").write_bytes(raw)
        (self.campaign_dir / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", "ascii")
        _chmod_tree(self.data)

    def collect(self, policy: str = "none") -> dict:
        return provenance.collect_campaign_provenance(
            self.campaign_dir, formal_campaign_id=self.campaign_id, estimation_policy=policy
        )


class LiveRequestProvenanceTests(unittest.TestCase):
    def test_pcap_trust_reuses_frozen_tcpdump_owner_boundary(self) -> None:
        """pcap 不要求归当前用户，但必须命中封存阶段的固定属主合同。"""

        with tempfile.TemporaryDirectory() as directory:
            pcap = Path(directory).resolve() / "egress.pcap"
            pcap.write_bytes(b"pcap" * 8)
            pcap.chmod(0o600)
            with mock.patch.object(
                provenance.evidence_permissions,
                "_owner_allowed",
                return_value=True,
            ) as owner_allowed:
                self.assertEqual(
                    provenance._trusted_pcap(pcap, "测试 pcap"),
                    pcap,
                )
            owner_allowed.assert_called_once()
            with (
                mock.patch.object(
                    provenance.evidence_permissions,
                    "_owner_allowed",
                    return_value=False,
                ),
                self.assertRaisesRegex(provenance.ProvenanceError, "属主"),
            ):
                provenance._trusted_pcap(pcap, "测试 pcap")

    def test_real_candidate_layouts_are_counted_and_cross_root_paired(self) -> None:
        """真实 v7 的四种产物形状必须闭合，frozen 重试不能因逻辑 run_id 相同而合并。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build()

            direct = fixture.runs / "v7-candidate-direct-core"
            pcap = direct / "direct" / "codex-http-s1" / "egress.pcap"
            pcap.parent.mkdir(parents=True, mode=0o700)
            pcap.write_bytes(b"pcap" * 8)
            _write_json(
                direct / "result" / "codex-http" / "s1" / "summary.json",
                {"scenario": "s1", "valid": True, "turn_count": 1},
            )
            _turn_events(
                direct / "result" / "codex-http" / "s1" / "turn1-events.jsonl",
                1,
            )
            _write_json(
                direct / "run-summary.json",
                {
                    "schema_version": "sub2api-direct-capture/v1",
                    "run_id": direct.name,
                    "status": "complete",
                    "cases": [
                        {
                            "subject": "codex-http",
                            "scenario": "s1",
                            "valid": True,
                            "pcap_bytes": pcap.stat().st_size,
                            "pcap_sha256": hashlib.sha256(pcap.read_bytes()).hexdigest(),
                        }
                    ],
                },
            )

            mitm = fixture.runs / "v7-candidate-mitm-core-codex-http-s1-a1-run"
            http_path = mitm / "mitm" / "codex-http" / "codex-http.jsonl"
            _write_jsonl(
                http_path,
                [
                    _mitm_http_row(
                        mitm.name,
                        "codex-http",
                        "s1",
                        "POST",
                        RESPONSES,
                        "gpt-5.5",
                    )
                ],
            )
            http_raw = http_path.read_bytes()
            _write_json(
                mitm / "result" / "s1" / "summary.json",
                {"scenario": "s1", "valid": True, "turn_count": 1},
            )
            _turn_events(mitm / "result" / "s1" / "turn1-events.jsonl", 1)
            _write_json(
                mitm / "run-summary.json",
                {
                    "schema_version": "sub2api-openai-mitm-scenario/v2",
                    "run_id": mitm.name,
                    "status": "complete",
                    "driver_return_code": 0,
                    "subject": "codex-http",
                    "scenario": "s1",
                    "scenario_result": {"valid": True, "error_type": ""},
                    "jsonl": [
                        {
                            "path": "mitm/codex-http/codex-http.jsonl",
                            "bytes": len(http_raw),
                            "records": 1,
                            "sha256": hashlib.sha256(http_raw).hexdigest(),
                        }
                    ],
                },
            )

            frozen_logical = "v7-candidate-frozen-core"
            frozen_roots = []
            for attempt in (1, 2):
                frozen = fixture.runs / f"{frozen_logical}.failed-attempt{attempt}"
                frozen_roots.append(frozen)
                scenario_root = frozen / "scenarios" / "A03"
                frozen_pcap = scenario_root / "egress.pcap"
                frozen_pcap.parent.mkdir(parents=True, mode=0o700)
                frozen_pcap.write_bytes((f"attempt-{attempt}".encode("ascii")) * 4)
                relay = scenario_root / "relay"
                relay.mkdir(mode=0o700)
                (relay / "conn001.client_to_upstream.bin").write_bytes(
                    _h1_post(RESPONSES, {"model": "gpt-5.5"})
                )
                _write_json(
                    frozen / "run-summary.json",
                    {
                        "schema_version": "candidate-core-capture/v1",
                        "codex_version": "0.154.0",
                        "run_id": frozen_logical,
                        "status": "failed",
                        "explicit_gate": True,
                        "production_forwarding_enabled": False,
                        "scenarios": [
                            {
                                "scenario_id": "A03",
                                "actions": {"responses_http_success": 1},
                                "production_forwarded": False,
                                "pcap_bytes": frozen_pcap.stat().st_size,
                                "pcap_sha256": hashlib.sha256(
                                    frozen_pcap.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    },
                )

            h1 = fixture.runs / "v7-candidate-h1"
            _write_json(
                h1 / "h1-wire.json",
                {
                    "schema_version": "h1-wire-probe/v1",
                    "requests": [
                        {
                            "request_line": (
                                "GET /backend-api/codex/models?client_version=0.154.0 "
                                "HTTP/1.1"
                            )
                        },
                        {
                            "request_line": (
                                "GET /backend-api/codex/responses HTTP/1.1"
                            )
                        },
                        {
                            "request_line": (
                                "POST /backend-api/codex/images/generations HTTP/1.1"
                            )
                        },
                    ],
                },
            )

            attempt = (
                fixture.campaign_dir
                / "candidates"
                / "c0154-candidate-v7"
                / "attempts"
                / "20260916T124216Z-f5194abd8934f5af"
            )
            _write_json(attempt / "reservation.json", {})
            _write_json(
                attempt / "job-candidate-x.json",
                {
                    "id": "candidate-x",
                    "evidence_roots": [
                        "/capture/runs/v7-candidate-direct-core",
                        "/capture/runs/v7-candidate-mitm-core-codex-http-s1-a1-run",
                        "/capture/runs/v7-candidate-frozen-core.failed-attempt2",
                        "/capture/runs/v7-candidate-h1",
                    ],
                },
            )
            _chmod_tree(fixture.data)

            receipt = fixture.collect("upper_bound_from_sibling_or_turn_ratio")
            candidate = next(
                item for item in receipt["jobs"] if item["job_id"] == "candidate-x"
            )
            self.assertEqual(candidate["status"], "estimated")
            self.assertEqual(candidate["precise_count"], 3)
            self.assertEqual(candidate["estimated_count"], 1)
            self.assertEqual(len(candidate["roots"]), 5)
            frozen_requests = [
                request
                for request in receipt["requests"]
                if request["producer_run_id"].startswith(frozen_logical)
            ]
            self.assertEqual(len(frozen_requests), 2)
            self.assertEqual(
                len({request["identity_key"] for request in frozen_requests}), 2
            )

    def test_candidate_job_uses_attempt_receipt_and_counts_all_retry_archives_once(self) -> None:
        """Candidate 模板根不能替代实际根，全部失败归档与最终根各计一次。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build()
            logical = fixture.runs / "candidate-v7-a15"
            for name in (
                "candidate-v7-a15.failed-attempt1",
                "candidate-v7-a15.failed-attempt2",
                "candidate-v7-a15",
            ):
                relay = fixture.runs / name / "relay"
                relay.mkdir(parents=True, mode=0o700)
                (relay / "conn001.client_to_upstream.bin").write_bytes(
                    _h1_post(RESPONSES, {"model": "gpt-5.5"})
                )
            attempt = (
                fixture.campaign_dir
                / "candidates"
                / "c0154-candidate-v7"
                / "attempts"
                / "20260916T124216Z-f5194abd8934f5af"
            )
            _write_json(attempt / "reservation.json", {})
            _write_json(
                attempt / "job-candidate-x.json",
                {
                    "id": "candidate-x",
                    "evidence_roots": [
                        "/capture/runs/candidate-v7-a15.failed-attempt2"
                    ],
                },
            )
            _chmod_tree(fixture.data)

            receipt = fixture.collect("upper_bound_from_sibling_or_turn_ratio")
            candidate = next(
                item for item in receipt["jobs"] if item["job_id"] == "candidate-x"
            )
            self.assertEqual(candidate["phase"], "candidate")
            self.assertEqual(candidate["status"], "resolved")
            self.assertEqual(candidate["precise_count"], 3)
            self.assertEqual(len(candidate["roots"]), 3)
            self.assertEqual(receipt["precise_total"], 13)
            self.assertEqual(fixture.collect("upper_bound_from_sibling_or_turn_ratio")["precise_total"], 13)

    def test_unified_unit_counts_requests_not_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.collect("none")
            self.assertEqual(receipt["counting_rule"], provenance.COUNTING_RULE)
            # capture mitm 2 + 2，compact mitm 2，relay 2 + 2。
            self.assertEqual(receipt["precise_total"], 10)
            self.assertEqual(receipt["estimated_total"], 0)
            self.assertEqual(receipt["status"], "accounting_unresolved")
            self.assertEqual(
                sorted(receipt["unresolved_job_ids"]),
                ["official-compact", "official-core", "official-ws-handshake-repeat"],
            )
            self.assertEqual(receipt["pending_job_ids"], ["official-pending"])
            by_job = {j["job_id"]: j for j in receipt["jobs"]}
            self.assertEqual(by_job["official-relay-ws"]["status"], "resolved")
            self.assertEqual(by_job["official-relay-ws"]["precise_count"], 4)
            kinds = {r["source_kind"] for r in receipt["requests"]}
            self.assertEqual(kinds, {"capture_mitm", "compact_mitm", "relay"})
            ws = [r for r in receipt["requests"] if r["native_coordinate"].get("transport") == "ws"]
            self.assertEqual(len(ws), 4)
            self.assertTrue(all(r["model"] == "gpt-6-astra" for r in ws))

    def test_estimation_policy_marks_direct_branches_as_upper_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.collect("upper_bound_from_sibling")
            # ws-repeat 根只有 direct 分支，没有同根 sibling，本级政策下仍未决。
            self.assertEqual(receipt["status"], "accounting_unresolved")
            self.assertEqual(receipt["unresolved_job_ids"], ["official-ws-handshake-repeat"])
            self.assertEqual(receipt["precise_total"], 10)
            # capture direct 2 + 2，compact direct 2。
            self.assertEqual(receipt["estimated_total"], 6)
            by_job = {j["job_id"]: j for j in receipt["jobs"]}
            self.assertEqual(by_job["official-core"]["status"], "estimated")
            branches = by_job["official-core"]["roots"][0]["branches"]
            direct = [b for b in branches if b["evidence"] == "direct"]
            self.assertTrue(all(b["estimation"] == "upper_bound_from_sibling" for b in direct))
            self.assertEqual(sum(b["estimated_count"] for b in direct), 4)

    def test_turn_ratio_policy_estimates_direct_only_roots(self) -> None:
        """没有同根 sibling 时，用同 Campaign 同 subject 的最大请求／turn 倍率作上界。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.collect("upper_bound_from_sibling_or_turn_ratio")
            self.assertEqual(receipt["status"], "complete")
            by_job = {j["job_id"]: j for j in receipt["jobs"]}
            failed = by_job["official-compact-failed"]
            self.assertEqual(failed["status"], "resolved")
            self.assertEqual(failed["precise_count"], 0)
            self.assertEqual(
                failed["roots"][0]["branches"][0]["authority_source"],
                "compact_driver_pre_request_failure",
            )
            repeat = by_job["official-ws-handshake-repeat"]
            self.assertEqual(repeat["status"], "estimated")
            # core 的 mitm codex-ws/s1 是 2 请求 / 1 turn，倍率 2；ws-repeat 共 3 个 turn。
            self.assertEqual(repeat["estimated_count"], 6)
            branches = repeat["roots"][0]["branches"]
            self.assertTrue(all(b["estimation"] == "upper_bound_from_turn_ratio" for b in branches))
            self.assertEqual(receipt["estimated_total"], 12)
            self.assertEqual(receipt["precise_total"], 10)

    def test_candidate_trace_test_root_is_zero_request_authority(self) -> None:
        """candidate-trace-test 的 run-summary 是零请求权威来源；日志摘要或判定不符则失败关闭。"""

        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs" / "c1-candidate-trace-test"
            root.mkdir(parents=True)
            log = root / "candidate-go-test.jsonl"
            log.write_bytes(b'{"Action":"run","Test":"TestA"}\n{"Action":"pass","Test":"TestA"}\n')
            summary = {
                "schema_version": "candidate-trace-test/v1",
                "command": ["go", "test", "-json", "-count=1", "-run", "^(TestA)$", "./internal/service"],
                "go_flags": "-mod=mod",
                "exit_code": 0,
                "verdict": "pass",
                "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            }
            _write_json(root / "run-summary.json", summary)
            kind, branches = provenance._root_branches(root)
            self.assertEqual(kind, "candidate_trace_test")
            self.assertEqual(len(branches), 1)
            self.assertEqual(branches[0]["status"], "resolved")
            self.assertEqual(branches[0]["requests"], [])
            self.assertEqual(branches[0]["authority_source"], "candidate_trace_test_run_summary")
            # 日志漂移 → 失败关闭
            log.write_bytes(log.read_bytes() + b'{"Action":"output"}\n')
            with self.assertRaisesRegex(provenance.ProvenanceError, "日志摘要"):
                provenance._root_branches(root)
            # verdict 非 pass → 失败关闭
            summary["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
            summary["verdict"] = "fail missing=[]"
            _write_json(root / "run-summary.json", summary)
            with self.assertRaisesRegex(provenance.ProvenanceError, "零请求的离线 go test"):
                provenance._root_branches(root)

    def test_frozen_candidate_summary_binds_campaign_target_version(self) -> None:
        """frozen 采集摘要按本 Campaign 目标版本校验，不再写死 0.154.0；缺目标版本失败关闭。"""

        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs" / "c01561-candidate-frozen-core"
            pcap = root / "scenarios" / "A03" / "egress.pcap"
            pcap.parent.mkdir(parents=True)
            pcap.write_bytes(b"frozen-pcap-bytes" * 4)
            relay = pcap.parent / "relay"
            relay.mkdir()
            (relay / "conn001.client_to_upstream.bin").write_bytes(
                _h1_post(RESPONSES, {"model": "gpt-5.5"})
            )
            _write_json(
                root / "run-summary.json",
                {
                    "schema_version": "candidate-core-capture/v1",
                    "codex_version": "0.156.1",
                    "run_id": root.name,
                    "status": "complete",
                    "explicit_gate": True,
                    "production_forwarding_enabled": False,
                    "scenarios": [
                        {
                            "scenario_id": "A03",
                            "actions": {"responses_http_success": 1},
                            "production_forwarded": False,
                            "pcap_bytes": pcap.stat().st_size,
                            "pcap_sha256": hashlib.sha256(pcap.read_bytes()).hexdigest(),
                        }
                    ],
                },
            )
            kind, branches = provenance._root_branches(root, target_version="0.156.1")
            self.assertEqual(kind, "candidate_frozen")
            self.assertTrue(branches)
            for version in ("0.154.0", None):
                with self.subTest(target_version=version):
                    with self.assertRaisesRegex(provenance.ProvenanceError, "run-summary 形状或状态非法"):
                        provenance._root_branches(root, target_version=version)

    def test_compact_driver_fact_without_error_type_stays_unresolved(self) -> None:
        """turn 0 但驱动没有记录错误类型或协议记录数时，不能当零。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build()
            summary = fixture.runs / "c-compact-fail.failed-attempt1" / "result" / "direct" / "summary.json"
            _write_json(summary, {"schema_version": "codex-compact-capture/v1", "turn_completed_count": 0})
            summary.chmod(0o600)
            receipt = fixture.collect("upper_bound_from_sibling_or_turn_ratio")
            self.assertEqual(receipt["unresolved_job_ids"], ["official-compact-failed"])

    def test_identity_key_is_stable_across_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = ProvenanceFixture(root)
            fixture.build()
            first = fixture.collect()
            copy = ProvenanceFixture(root / "elsewhere", campaign_id="c1")
            shutil.copytree(fixture.data, copy.data)
            _chmod_tree(copy.data)
            second = copy.collect()
            self.assertEqual(
                sorted(r["identity_key"] for r in first["requests"]),
                sorted(r["identity_key"] for r in second["requests"]),
            )
            self.assertNotEqual(first["requests"][0]["source_file"], second["requests"][0]["source_file"])
            self.assertEqual(first["identity_keys_sha256"], second["identity_keys_sha256"])

    def test_requests_below_completed_turns_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build(capture_turns_over_requests=True)
            with self.assertRaisesRegex(provenance.ProvenanceError, "小于完成 turn 数"):
                fixture.collect("none")

    def test_root_with_two_authority_sources_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build()
            relay = fixture.runs / "oauth-run" / "relay"
            relay.mkdir(mode=0o700)
            (relay / "relay.json").write_text('{"schema_version":"byte-relay/v1","connections":[]}\n', "utf-8")
            _chmod_tree(fixture.data)
            with self.assertRaisesRegex(provenance.ProvenanceError, "多种权威来源"):
                fixture.collect("upper_bound_from_sibling")

    def test_project_audit_deduplicates_shared_roots_across_campaigns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = ProvenanceFixture(root)
            fixture.build()
            # 第二个 Campaign 引用同一个 relay run 目录。
            second = ProvenanceFixture(root, campaign_id="c2")
            manifest = {
                "campaign_id": "c2",
                "campaign_mode": "formal",
                "created_at_utc": "2026-09-15T02:34:48Z",
                "configuration": {"capture_root": "/capture"},
                "jobs": [
                    {"id": "official-relay-ws", "phase": "official", "evidence_roots": ["/capture/runs/c-relay"]},
                ],
            }
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            second.campaign_dir.mkdir(parents=True, mode=0o700)
            (second.campaign_dir / "campaign.json").write_bytes(raw)
            (second.campaign_dir / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", "ascii")
            early = ProvenanceFixture(root, campaign_id="c0")
            manifest_early = dict(manifest, campaign_id="c0", created_at_utc="2026-09-10T00:00:00Z")
            raw_early = (json.dumps(manifest_early, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            early.campaign_dir.mkdir(parents=True, mode=0o700)
            (early.campaign_dir / "campaign.json").write_bytes(raw_early)
            (early.campaign_dir / "campaign.sha256").write_text(hashlib.sha256(raw_early).hexdigest() + "\n", "ascii")
            _chmod_tree(fixture.data)
            blocked = provenance.audit_project(
                fixture.data,
                since_utc="2026-09-13T03:17:00Z",
                estimation_policy="upper_bound_from_sibling",
            )
            self.assertEqual(blocked["status"], "accounting_unresolved")
            self.assertEqual(blocked["unresolved_campaign_ids"], ["c1"])
            audit = provenance.audit_project(
                fixture.data,
                since_utc="2026-09-13T03:17:00Z",
                estimation_policy="upper_bound_from_sibling_or_turn_ratio",
            )
            self.assertEqual([c["campaign_id"] for c in audit["campaigns"]], ["c1", "c2"])
            self.assertEqual(audit["unresolved_campaign_ids"], [])
            self.assertEqual(audit["precise_total"], 10)
            self.assertEqual(len(audit["duplicates"]), 4)
            self.assertTrue(all(d["first_campaign_id"] == "c1" for d in audit["duplicates"]))
            self.assertEqual(audit["status"], "complete")
            self.assertEqual(audit["identity_key_count"], 10)

    def test_cli_writes_receipt_once_and_signals_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProvenanceFixture(Path(directory).resolve())
            fixture.build()
            output = fixture.root / "out" / "receipt.json"
            code = provenance.main([
                "collect-campaign",
                "--campaign-dir", str(fixture.campaign_dir),
                "--campaign-id", "c1",
                "--output", str(output),
            ])
            self.assertEqual(code, 3)
            payload = json.loads(output.read_text("utf-8"))
            self.assertEqual(payload["status"], "accounting_unresolved")
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(provenance.ProvenanceError, "不得覆盖"):
                provenance.write_receipt(payload, output)

    def test_mitm_body_summary_and_turn_metadata_are_parsed(self) -> None:
        """mitm 正文是摘要对象：模型取自 body.json 或 body.text，线程元数据取自 client_metadata。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            system_metadata = json.dumps({"thread_source": "system", "request_kind": "prewarm"})
            rows = [
                _mitm_http_row(
                    "r", "s1", "sc", "POST", RESPONSES,
                    payload={"model": "gpt-5.6-luna", "client_metadata": {"x-codex-turn-metadata": system_metadata}},
                ),
                _mitm_http_row("r", "s1", "sc", "POST", RESPONSES, model="gpt-5.5"),
                _mitm_http_row("r", "s1", "sc", "POST", RESPONSES, model="gpt-5.5"),
                _mitm_http_row("r", "s1", "sc", "POST", RESPONSES),
                _mitm_http_row("r", "s1", "sc", "POST", RESPONSES),
                _mitm_http_row("r", "s1", "sc", "GET", "/backend-api/codex/models", model="gpt-5.5"),
            ]
            # 第三条只剩 text 可解析；第四条 text 不是 JSON；第五条把 model 伪造在 body 顶层。
            rows[2]["request"]["body"]["json"] = None
            rows[3]["request"]["body"]["text"] = "not-json"
            rows[4]["request"]["body"] = {"model": "gpt-5.5"}
            path = root / "codex-http.jsonl"
            _write_jsonl(path, rows)
            ws_path = root / "codex-ws.jsonl"
            _write_jsonl(ws_path, [
                _mitm_ws_row("r", "s1", "sc", True, {
                    "type": "response.create", "model": "gpt-6-astra",
                    "client_metadata": {"x-codex-turn-metadata": json.dumps({"thread_source": "user", "request_kind": "turn"})},
                }),
                _mitm_ws_row("r", "s1", "sc", True, {"type": "response.create", "model": "gpt-6-astra", "client_metadata": {"x-codex-turn-metadata": "{broken"}}),
                _mitm_ws_row("r", "s1", "sc", False, {"type": "response.created"}),
            ])
            _chmod_tree(root)
            prefix = {"evidence": "mitm", "subject": "s1", "scenario": "sc"}
            http = provenance._mitm_http_requests(path, producer_run_id="r", source_kind="mitm", coordinate_prefix=prefix)
            self.assertEqual(
                [(r["model"], r["thread_source"], r["request_kind"]) for r in http],
                [("gpt-5.6-luna", "system", "prewarm"), ("gpt-5.5", None, None), ("gpt-5.5", None, None), (None, None, None), (None, None, None)],
            )
            ws = provenance._mitm_ws_requests(ws_path, producer_run_id="r", source_kind="mitm", coordinate_prefix=prefix)
            self.assertEqual(
                [(r["model"], r["thread_source"], r["request_kind"]) for r in ws],
                [("gpt-6-astra", "user", "turn"), ("gpt-6-astra", None, None)],
            )


if __name__ == "__main__":
    unittest.main()
