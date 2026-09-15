"""逐请求 provenance v2：统一计量单位、估计政策、身份键去重与来源唯一性。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

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


def _mitm_http_row(run_id: str, subject: str, scenario: str, method: str, path: str, model: str | None = None) -> dict:
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
            "body": {"model": model} if model else {},
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


if __name__ == "__main__":
    unittest.main()
