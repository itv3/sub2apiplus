"""close-campaign-ledger：按统一计量口径关闭账本，顺序固定、幂等、失败关闭。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_timing_ledger as ledger


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _write_provenance(path: Path, *, campaign_id: str = "c-formal", precise: int = 140, estimated: int = 30, status: str = "complete", schema: str = ledger.PROVENANCE_RECEIPT_SCHEMA) -> Path:
    payload = {
        "schema_version": schema,
        "formal_campaign_id": campaign_id,
        "status": status,
        "precise_total": precise,
        "estimated_total": estimated,
        "estimation_policy": "upper_bound_from_sibling",
        "counting_rule": "codex_model_requests/v2",
        "identity_keys_sha256": "a" * 64,
        "unresolved_job_ids": [] if status == "complete" else ["official-core"],
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
    path.chmod(0o600)
    return path


class CloseCampaignLedgerTests(unittest.TestCase):
    def _ledger(self, root: Path, *, started_minutes_ago: int) -> Path:
        ledger_dir = root / "ledger"
        started = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
        ledger.create_ledger(
            ledger_dir,
            upgrade_id="codex-0151-to-0154-test",
            baseline_version="0.151.0",
            target_version="0.154.0",
            campaign_purpose="production_replacement",
            evidence_decision="recapture",
            started_at_utc=_iso(started),
        )
        return ledger_dir

    def _open_vc1(self, ledger_dir: Path, *, live: int = 0) -> None:
        ledger.append_event(ledger_dir, event_id="vc0-done", phase="VC-0", event_type="stage_completed", next_action="启动 VC-1")
        ledger.append_event(ledger_dir, event_id="vc1-start", phase="VC-1", event_type="stage_started", next_action="派发首批")
        if live:
            ledger.append_event(ledger_dir, event_id="vc1-accounted", phase="VC-1", event_type="receipt_passed", live_request_count=live, next_action="继续")

    def test_close_active_ledger_appends_abandon_and_stop_with_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_dir = self._ledger(root, started_minutes_ago=5)
            self._open_vc1(ledger_dir, live=144)
            receipt = _write_provenance(root / "prov.json", precise=140, estimated=30)
            result = ledger.close_campaign_ledger(ledger_dir, root_cause_id="rc1-abc", provenance_receipt=receipt)
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["appended_event_ids"], ["close-stage-abandoned-VC-1", "close-stop-the-line"])
            self.assertEqual(result["previous_count"], 144)
            self.assertEqual(result["unaccounted_delta"], 26)
            self.assertEqual(result["resulting_total"], 170)
            summary = ledger.inspect_ledger(ledger_dir)
            self.assertEqual(summary["status"], "stopped")
            self.assertEqual(summary["total_live_request_count"], 170)
            close_path = ledger_dir / result["ledger_close_receipt"]["path"]
            close_receipt = json.loads(close_path.read_text("utf-8"))
            self.assertEqual(close_receipt["schema_version"], ledger.LEDGER_CLOSE_SCHEMA)
            self.assertEqual(close_receipt["recomputed_total"], 170)
            self.assertFalse(close_receipt["delta_clamped"])
            self.assertEqual(close_receipt["provenance_receipt_sha256"], hashlib.sha256(receipt.read_bytes()).hexdigest())
            events = ledger._load_events(ledger_dir)
            stop = events[-1][0]
            self.assertEqual([r["role"] for r in stop["receipts"]], ["ledger_close", "provenance"])
            self.assertEqual(stop["live_request_count"], 26)
            # 再次关闭是幂等的
            again = ledger.close_campaign_ledger(ledger_dir, root_cause_id="rc1-abc", provenance_receipt=receipt)
            self.assertEqual(again["status"], "already-closed")

    def test_stop_required_ledger_closes_active_attempt_metadata_only_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_dir = self._ledger(root, started_minutes_ago=5)
            self._open_vc1(ledger_dir)
            ledger.append_event(ledger_dir, event_id="att-1", phase="VC-1", event_type="attempt_started", attempt_id="20260915T000000Z-aaaa", next_action="执行")
            # 让总预算过期：改写 plan 的开始时间不可行（只写一次），改用 stage 预算耗尽的方式：
            # 直接以未来时间检查会报错，所以构造 recorded_at 使 VC-1 阶段超过其预算。
            stage_budget = ledger.DEFAULT_STAGE_BUDGETS["VC-1"]
            future = datetime.now(timezone.utc) + timedelta(minutes=stage_budget + 1)
            self.assertEqual(ledger.inspect_ledger(ledger_dir, now=_iso(future))["status"], "stop_required")
            with self.assertRaisesRegex(ledger.TimingLedgerError, "禁止继续追加执行事件"):
                ledger.append_event(ledger_dir, event_id="bad", phase="VC-1", event_type="attempt_failed", attempt_id="20260915T000000Z-aaaa", root_cause_id="rc1-x", live_request_count=1, recorded_at_utc=_iso(future))
            receipt = _write_provenance(root / "prov.json", precise=10, estimated=0)
            result = ledger.close_campaign_ledger(ledger_dir, root_cause_id="rc1-x", provenance_receipt=receipt, recorded_at_utc=_iso(future))
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["appended_event_ids"][0], "close-attempt-failed-20260915T000000Z-aaaa")
            events = [e for e, _ in ledger._load_events(ledger_dir)]
            failed = [e for e in events if e["event_type"] == "attempt_failed"][0]
            self.assertEqual(failed["receipts"], [])
            self.assertEqual(failed["live_request_count"], 0)
            self.assertEqual([e["event_type"] for e in events[-3:]], ["attempt_failed", "stage_abandoned", "stop_the_line"])

    def test_negative_delta_is_clamped_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_dir = self._ledger(root, started_minutes_ago=5)
            self._open_vc1(ledger_dir, live=200)
            receipt = _write_provenance(root / "prov.json", precise=140, estimated=30)
            result = ledger.close_campaign_ledger(ledger_dir, root_cause_id="rc1-abc", provenance_receipt=receipt)
            self.assertEqual(result["unaccounted_delta"], -30)
            self.assertEqual(result["resulting_total"], 200)
            close_receipt = json.loads((ledger_dir / result["ledger_close_receipt"]["path"]).read_text("utf-8"))
            self.assertTrue(close_receipt["delta_clamped"])
            self.assertEqual(close_receipt["live_request_count_recorded"], 0)

    def test_interrupted_close_resumes_from_first_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_dir = self._ledger(root, started_minutes_ago=5)
            self._open_vc1(ledger_dir)
            ledger.append_event(ledger_dir, event_id="close-stage-abandoned-VC-1", phase="VC-1", event_type="stage_abandoned", root_cause_id="rc1-abc", next_action="等待关闭")
            receipt = _write_provenance(root / "prov.json", precise=5, estimated=0)
            result = ledger.close_campaign_ledger(ledger_dir, root_cause_id="rc1-abc", provenance_receipt=receipt)
            self.assertEqual(result["appended_event_ids"], ["close-stop-the-line"])
            self.assertEqual(ledger.inspect_ledger(ledger_dir)["status"], "stopped")

    def test_rejects_wrong_schema_and_unresolved_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_dir = self._ledger(root, started_minutes_ago=5)
            self._open_vc1(ledger_dir)
            bad = _write_provenance(root / "bad.json", schema="live-request-provenance/v1")
            with self.assertRaisesRegex(ledger.TimingLedgerError, "schema"):
                ledger.close_campaign_ledger(ledger_dir, root_cause_id="rc1-abc", provenance_receipt=bad)
            unresolved = _write_provenance(root / "unres.json", precise=7, estimated=0, status="accounting_unresolved")
            result = ledger.close_campaign_ledger(ledger_dir, root_cause_id="rc1-abc", provenance_receipt=unresolved)
            close_receipt = json.loads((ledger_dir / result["ledger_close_receipt"]["path"]).read_text("utf-8"))
            self.assertEqual(close_receipt["provenance_status"], "accounting_unresolved")
            self.assertEqual(close_receipt["unresolved_job_ids"], ["official-core"])

    def test_cli_closes_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_dir = self._ledger(root, started_minutes_ago=5)
            self._open_vc1(ledger_dir)
            receipt = _write_provenance(root / "prov.json", precise=3, estimated=0)
            code = ledger.main([
                "close-campaign-ledger", "--ledger-dir", str(ledger_dir), "--root-cause", "rc1-abc", "--provenance-receipt", str(receipt),
            ])
            self.assertEqual(code, 0)
            self.assertEqual(ledger.inspect_ledger(ledger_dir)["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
