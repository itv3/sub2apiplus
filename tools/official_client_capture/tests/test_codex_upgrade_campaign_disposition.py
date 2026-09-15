"""历史 Campaign 处置清单：四种处置、复用来源合格判定、账本绑定与失败关闭。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_campaign_disposition as disposition
from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_official_attempt_audit as attempt_audit
from tools.official_client_capture import codex_upgrade_timing_ledger as ledger


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
    path.chmod(0o600)


def _chmod_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)


class DispositionFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"

    def campaign(self, campaign_id: str, *, mode: str, attempts: list[tuple[str, str, dict[str, int]]] = ()) -> None:
        campaign_dir = self.data / "evidence" / "campaigns" / campaign_id
        _write_json(
            campaign_dir / "campaign.json",
            {
                "campaign_id": campaign_id,
                "campaign_mode": mode,
                "campaign_purpose": "production_replacement",
                "target_version": "0.154.0",
                "created_at_utc": "2026-09-14T23:14:19Z",
                "tool_identity": {"files_sha256": "f" * 64},
            },
        )
        for attempt_id, status, counts in attempts:
            results = []
            for result_status, count in counts.items():
                results.extend({"id": f"job-{result_status}-{i}", "status": result_status} for i in range(count))
            _write_json(
                campaign_dir / "official" / "attempts" / attempt_id / "attempt.json",
                {"attempt_id": attempt_id, "status": status, "results": results, "started_at_utc": "2026-09-14T23:20:51Z", "completed_at_utc": "2026-09-14T23:44:51Z"},
            )

    def ledger(self, name: str, *, formal_campaign_ids: list[str], close: bool) -> None:
        ledger_dir = self.data / "control" / name
        ledger_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ledger.create_ledger(
            ledger_dir,
            upgrade_id=f"upgrade-{name}",
            baseline_version="0.151.0",
            target_version="0.154.0",
            campaign_purpose="production_replacement",
            evidence_decision="recapture",
            started_at_utc=(datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(timespec="seconds"),
        )
        ledger.append_event(ledger_dir, event_id="vc0-done", phase="VC-0", event_type="stage_completed", next_action="启动 VC-1")
        ledger.append_event(ledger_dir, event_id="vc1-start", phase="VC-1", event_type="stage_started", next_action="派发")
        for campaign_id in formal_campaign_ids:
            (ledger_dir / "receipts" / "vc0-closeout" / campaign_id).mkdir(parents=True, mode=0o700)
        if close:
            ledger.append_event(ledger_dir, event_id="abandon", phase="VC-1", event_type="stage_abandoned", root_cause_id="rc1-x", next_action="停")
            ledger.append_event(ledger_dir, event_id="stop", phase="VC-1", event_type="stop_the_line", root_cause_id="rc1-x", next_action="停")

    def project_audit(self, entries: dict[str, str]) -> Path:
        path = self.root / "project-audit.json"
        _write_json(
            path,
            {
                "schema_version": provenance.PROJECT_SCHEMA_VERSION,
                "status": "complete",
                "campaigns": [
                    {"campaign_id": cid, "status": status, "precise_count_after_dedup": 140, "estimated_count": 30, "unresolved_job_ids": []}
                    for cid, status in entries.items()
                ],
            },
        )
        return path

    def attempt_audit(self, campaign_id: str, attempt_id: str, status: str) -> Path:
        path = self.root / f"attempt-audit-{campaign_id}.json"
        _write_json(path, {"schema_version": attempt_audit.SCHEMA_VERSION, "campaign_id": campaign_id, "attempt_id": attempt_id, "status": status, "failed_sections": [] if status == "passed" else ["models"]})
        return path

    def build(self) -> None:
        self.campaign("c-preflight", mode="preflight_only")
        self.campaign("c-fresh", mode="formal", attempts=[("20260914T232051Z-f735b7996999027b", "awaiting_receipts", {"complete": 29})])
        self.campaign("c-new-window", mode="formal", attempts=[("20260914T102852Z-04996800fbbe4e94", "awaiting_receipts", {"complete": 29})])
        self.campaign("c-failed", mode="formal", attempts=[("20260913T232116Z-ef68b7989e951d13", "failed", {"failed": 2, "complete": 2})])
        self.campaign("c-empty", mode="formal")
        self.ledger("fresh-timing-ledger", formal_campaign_ids=["c-fresh"], close=False)
        self.ledger("failed-timing-ledger", formal_campaign_ids=["c-failed"], close=True)
        _chmod_tree(self.root)


class CampaignDispositionTests(unittest.TestCase):
    def test_four_dispositions_and_ledger_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = DispositionFixture(Path(directory).resolve())
            fixture.build()
            audit = fixture.project_audit({"c-fresh": "complete", "c-new-window": "complete", "c-failed": "complete"})
            verdicts = [fixture.attempt_audit("c-fresh", "20260914T232051Z-f735b7996999027b", "passed")]
            _chmod_tree(fixture.root)
            receipt = disposition.build_disposition(
                fixture.data,
                primary_reuse_source="c-fresh",
                backup_reuse_source="c-new-window",
                project_audit=audit,
                attempt_audits=verdicts,
            )
            by_id = {entry["campaign_id"]: entry for entry in receipt["campaigns"]}
            self.assertEqual(by_id["c-preflight"]["disposition"], "preflight_archive")
            self.assertEqual(by_id["c-fresh"]["disposition"], "reuse_primary")
            self.assertTrue(by_id["c-fresh"]["eligible"])
            self.assertEqual(by_id["c-fresh"]["attempt_audit"]["status"], "passed")
            self.assertEqual(by_id["c-fresh"]["ledgers"][0]["ledger_id"], "control/fresh-timing-ledger")
            self.assertEqual(by_id["c-fresh"]["ledgers"][0]["status"], "active")
            self.assertEqual(by_id["c-new-window"]["disposition"], "reuse_backup")
            self.assertTrue(by_id["c-new-window"]["eligible"])
            self.assertEqual(by_id["c-failed"]["disposition"], "read_only_archive")
            self.assertEqual(by_id["c-failed"]["ledgers"][0]["status"], "stopped")
            self.assertEqual(by_id["c-empty"]["reasons"], ["formal Campaign 没有 official attempt"])
            self.assertEqual(receipt["summary"]["dispositions"], {"reuse_primary": 1, "reuse_backup": 1, "read_only_archive": 2, "preflight_archive": 1})
            self.assertEqual([l["ledger_id"] for l in receipt["ledgers_to_close"]], ["control/fresh-timing-ledger"])
            self.assertTrue(receipt["primary_reuse_eligible"])

    def test_primary_source_ineligibility_is_reported_not_silently_switched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = DispositionFixture(Path(directory).resolve())
            fixture.build()
            audit = fixture.project_audit({"c-fresh": "accounting_unresolved", "c-new-window": "complete"})
            verdicts = [fixture.attempt_audit("c-fresh", "20260914T232051Z-f735b7996999027b", "failed")]
            _chmod_tree(fixture.root)
            receipt = disposition.build_disposition(fixture.data, primary_reuse_source="c-fresh", backup_reuse_source="c-new-window", project_audit=audit, attempt_audits=verdicts)
            fresh = [e for e in receipt["campaigns"] if e["campaign_id"] == "c-fresh"][0]
            self.assertEqual(fresh["disposition"], "reuse_primary")
            self.assertFalse(fresh["eligible"])
            self.assertEqual(len(fresh["reasons"]), 2)
            self.assertFalse(receipt["primary_reuse_eligible"])
            failed_source = disposition.build_disposition(fixture.data, primary_reuse_source="c-failed")
            entry = [e for e in failed_source["campaigns"] if e["campaign_id"] == "c-failed"][0]
            self.assertFalse(entry["eligible"])
            self.assertIn("不是 awaiting_receipts", entry["reasons"][0])

    def test_rejects_unknown_or_duplicate_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = DispositionFixture(Path(directory).resolve())
            fixture.build()
            with self.assertRaisesRegex(disposition.DispositionError, "不在目标版本"):
                disposition.build_disposition(fixture.data, primary_reuse_source="c-missing")
            with self.assertRaisesRegex(disposition.DispositionError, "不得与主来源相同"):
                disposition.build_disposition(fixture.data, primary_reuse_source="c-fresh", backup_reuse_source="c-fresh")

    def test_cli_writes_receipt_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = DispositionFixture(Path(directory).resolve())
            fixture.build()
            output = fixture.root / "out" / "disposition.json"
            code = disposition.main(["build-campaign-disposition", "--data-root", str(fixture.data), "--primary-reuse-source", "c-fresh", "--backup-reuse-source", "c-new-window", "--output", str(output)])
            self.assertEqual(code, 0)
            payload = json.loads(output.read_text("utf-8"))
            self.assertEqual(payload["schema_version"], disposition.SCHEMA_VERSION)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            self.assertEqual(disposition.main(["build-campaign-disposition", "--data-root", str(fixture.data), "--primary-reuse-source", "c-fresh", "--output", str(output)]), 2)


if __name__ == "__main__":
    unittest.main()
