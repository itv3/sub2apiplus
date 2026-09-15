"""升级时间对账：六类可信边界、确定性分类、失败关闭与人工分类收据。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_time_reconciliation as recon
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts

T0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(raw)
    path.chmod(0o600)


def _write_json(path: Path, value: object) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    _write(path, raw)
    return raw


def _chmod_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _ledger_event(sequence: int, time: datetime, phase: str, event_type: str, **extra: object) -> dict:
    return {
        "schema_version": timing_ledger.EVENT_SCHEMA,
        "sequence": sequence,
        "event_id": f"e{sequence}",
        "recorded_at_utc": _iso(time),
        "phase": phase,
        "event_type": event_type,
        "attempt_id": extra.get("attempt_id"),
        "root_cause_id": extra.get("root_cause_id"),
        "live_request_count": 0,
        "receipts": [],
        "next_action": None,
        "previous_event_sha256": None,
    }


def _write_ledger(root: Path, created: datetime, events: list[dict]) -> None:
    plan = {
        "schema_version": timing_ledger.PLAN_SCHEMA,
        "upgrade_id": f"upgrade-{root.name}",
        "created_at_utc": _iso(created),
        "started_at_utc": _iso(created),
        "baseline_version": "0.151.0",
        "target_version": "0.154.0",
        "campaign_purpose": "production_replacement",
        "evidence_decision": "recapture",
        "total_budget_minutes": 360,
        "stage_budgets_minutes": {},
        "same_root_cause_retry_limit": 2,
        "producer": {},
    }
    _write_json(root / "ledger.json", plan)
    previous: str | None = None
    for event in events:
        event = dict(event)
        event["previous_event_sha256"] = previous
        raw = _write_json(root / "events" / f"{event['sequence']:06d}.json", event)
        previous = _sha256(raw)


def _supervisor_record(sequence: int, time: datetime, *, campaign_id: str, phase: str, event_type: str, previous: str | None) -> dict:
    unsigned = {
        "schema_version": recon.SUPERVISOR_EVENT_SCHEMA,
        "sequence": sequence,
        "recorded_at_utc": _iso(time),
        "recorded_at_epoch": time.timestamp(),
        "event_type": event_type,
        "operation": "supervisor:start" if sequence == 1 else "supervisor:stop",
        "campaign_id": campaign_id,
        "phase": phase,
        "owner_pid": 1,
        "owner_nonce": "n" * 8,
        "job_id": None,
        "status": "running",
        "reason": None,
        "started_at_epoch": None,
        "ended_at_epoch": None,
        "metadata": {},
        "previous_event_sha256": previous,
    }
    record = dict(unsigned)
    record["event_sha256"] = _sha256(vc_artifacts.canonical_bytes(unsigned))
    return record


def _write_supervisor_run(run_dir: Path, *, campaign_id: str, phase: str, start: datetime, end: datetime) -> None:
    first = _supervisor_record(1, start, campaign_id=campaign_id, phase=phase, event_type="command-started", previous=None)
    second = _supervisor_record(2, end, campaign_id=campaign_id, phase=phase, event_type="stopped", previous=first["event_sha256"])
    lines = [vc_artifacts.canonical_bytes(record) for record in (first, second)]
    raw = b"".join(line if line.endswith(b"\n") else line + b"\n" for line in lines)
    _write(run_dir / "events.ndjson", raw)


class ReconciliationFixture:
    """一个宿主数据根：账本、监督器 run、部署收据、Campaign、审计目录与 git 清单。

    时间轴（分钟，相对 10:00Z）：
    0～30 账本 VC-0 执行；30～45 账本 VC-1 执行；45 stop_the_line（此后一直停线）；
    60 修复提交；65 部署收据；65.5 bootstrap run；70 preflight Campaign 创建；
    72～80 VC-0 closeout 审计；85 formal Campaign 创建；90～110 VC-1 监督器 run；
    110～240 无事件。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"

    def build(self) -> None:
        ledger = self.data / "evidence" / "control" / "ledger-a"
        _write_ledger(
            ledger,
            _at(0),
            [
                _ledger_event(1, _at(0), "VC-0", "stage_started"),
                _ledger_event(2, _at(30), "VC-0", "stage_completed"),
                _ledger_event(3, _at(30), "VC-1", "stage_started"),
                _ledger_event(4, _at(45), "VC-1", "stop_the_line", root_cause_id="rc-a"),
            ],
        )
        _write_json(
            self.data / "control" / "codex-0154-supervisor-enable-20260914t110500z.json",
            {"schema_version": recon.DEPLOY_RECEIPT_SCHEMA, "created_at_utc": _iso(_at(65)), "status": "passed", "campaign_id": "c0154-supervisor-enable-x", "tool_files_sha256": "a" * 64},
        )
        _write_supervisor_run(
            self.data / "control" / ("run-" + "b" * 64),
            campaign_id="c0154-supervisor-enable-x",
            phase="bootstrap",
            start=_at(65.5),
            end=_at(65.5) + timedelta(seconds=1),
        )
        _write_json(self.data / "evidence" / "campaigns" / "c-preflight" / "campaign.json", {"campaign_id": "c-preflight", "campaign_mode": "preflight_only", "created_at_utc": _iso(_at(70))})
        _write_json(self.data / "audit" / "c-formal-vc0-closeout" / "request.json", {"formal_campaign_id": "c-formal", "requested_at_utc": _iso(_at(72))})
        _write_json(self.data / "audit" / "c-formal-vc0-closeout" / "receipt.json", {"schema_version": "codex-upgrade-vc0-closeout-receipt/v1", "status": "passed", "completed_at_utc": _iso(_at(80))})
        _write_json(self.data / "evidence" / "campaigns" / "c-formal" / "campaign.json", {"campaign_id": "c-formal", "campaign_mode": "formal", "created_at_utc": _iso(_at(85))})
        _write_supervisor_run(
            self.data / "control" / "c-formal-vc1-supervisor-0001" / ("run-" + "c" * 64),
            campaign_id="c-formal",
            phase="VC-1",
            start=_at(90),
            end=_at(110),
        )
        self.git_log = self.root / "git-log.tsv"
        _write(self.git_log, (f"{'1' * 40}\t{_iso(_at(60))}\tfix(codex): 修一处\n").encode("utf-8"))
        _chmod_tree(self.root)

    def run(self, **kwargs: object) -> dict:
        options = {"since_utc": _iso(_at(0)), "until_utc": _iso(_at(240)), "git_log_file": self.git_log}
        options.update(kwargs)
        return recon.reconcile_upgrade_time(self.data, **options)


def _totals_minutes(receipt: dict) -> dict[str, float]:
    return {key: round(value / 60, 3) for key, value in receipt["totals_seconds"].items()}


class UpgradeTimeReconciliationTests(unittest.TestCase):
    def test_mixed_sources_reconcile_into_contiguous_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.run()
            self.assertEqual(receipt["status"], "unclassified_present")
            self.assertEqual(receipt["wall_clock_seconds"], 240 * 60)
            self.assertEqual(
                _totals_minutes(receipt),
                {
                    "campaign_creation": 16.5,
                    "deployment": 5.5,
                    "tool_repair": 15.0,
                    "unclassified": 130.0,
                    "vc0_execution": 38.0,
                    "vc1_execution": 35.0,
                },
            )
            self.assertEqual(receipt["basis_seconds"], {"evidence": 73 * 60, "inferred": 37 * 60, "manual": 0, "none": 130 * 60})
            intervals = receipt["intervals"]
            self.assertEqual(intervals[0]["start_utc"], receipt["since_utc"])
            self.assertEqual(intervals[-1]["end_utc"], receipt["until_utc"])
            for previous, current in zip(intervals, intervals[1:]):
                self.assertEqual(previous["end_utc"], current["start_utc"])
            # 停线后账本仍记 VC-1 active，但停线期间不算执行。
            repair = [i for i in intervals if i["category"] == "tool_repair"][0]
            self.assertTrue(repair["ledger_stopped"])
            self.assertEqual(repair["basis"], {"kind": "inferred", "refs": ["git:" + "1" * 40]})
            self.assertEqual(receipt["sources"]["ledgers"][0]["event_count"], 4)
            self.assertEqual(receipt["sources"]["supervisor_runs"], 2)
            self.assertEqual(receipt["sources"]["git"]["mode"], "file")

    def test_idle_threshold_turns_long_repair_gap_into_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.run(idle_threshold_minutes=10)
            totals = _totals_minutes(receipt)
            self.assertEqual(totals["idle"], 15.0)
            self.assertNotIn("tool_repair", totals)

    def test_ledger_chain_break_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            path = fixture.data / "evidence" / "control" / "ledger-a" / "events" / "000003.json"
            event = json.loads(path.read_text("utf-8"))
            event["previous_event_sha256"] = "f" * 64
            _write_json(path, event)
            with self.assertRaisesRegex(recon.TimeReconciliationError, "摘要链断裂"):
                fixture.run()

    def test_supervisor_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            path = fixture.data / "control" / "c-formal-vc1-supervisor-0001" / ("run-" + "c" * 64) / "events.ndjson"
            lines = [line for line in path.read_bytes().splitlines() if line.strip()]
            record = json.loads(lines[1])
            record["status"] = "tampered"
            lines[1] = vc_artifacts.canonical_bytes(record).rstrip(b"\n")
            _write(path, b"\n".join(lines) + b"\n")
            with self.assertRaisesRegex(recon.TimeReconciliationError, "摘要不一致"):
                fixture.run()

    def test_manual_classification_requires_bound_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            doc = fixture.root / "plan.md"
            _write(doc, "方案草稿\n".encode("utf-8"))
            manual = fixture.root / "manual.json"

            def write_manual(entries: list[dict]) -> Path:
                if manual.exists():
                    manual.unlink()
                _write_json(manual, {"schema_version": recon.MANUAL_SCHEMA_VERSION, "entries": entries})
                return manual

            good = {
                "start_utc": _iso(_at(120)), "end_utc": _iso(_at(180)), "category": "analysis_and_planning",
                "evidence": [{"path": "plan.md", "sha256": _sha256(doc.read_bytes())}], "note": "写方案",
            }
            receipt = fixture.run(manual_classification=write_manual([good]), evidence_root=fixture.root)
            totals = _totals_minutes(receipt)
            self.assertEqual(totals["analysis_and_planning"], 60.0)
            self.assertEqual(totals["unclassified"], 70.0)
            self.assertEqual(receipt["basis_seconds"]["manual"], 60 * 60)
            manual_interval = [i for i in receipt["intervals"] if i["category"] == "analysis_and_planning"][0]
            self.assertEqual(manual_interval["basis"]["kind"], "manual")
            self.assertIn("manual:0", manual_interval["basis"]["refs"])

            with self.assertRaisesRegex(recon.TimeReconciliationError, "证据摘要漂移"):
                fixture.run(manual_classification=write_manual([{**good, "evidence": [{"path": "plan.md", "sha256": "0" * 64}]}]), evidence_root=fixture.root)
            with self.assertRaisesRegex(recon.TimeReconciliationError, "至少一份支撑证据"):
                fixture.run(manual_classification=write_manual([{**good, "evidence": []}]), evidence_root=fixture.root)
            with self.assertRaisesRegex(recon.TimeReconciliationError, "覆盖了证据区间"):
                fixture.run(manual_classification=write_manual([{**good, "start_utc": _iso(_at(95)), "end_utc": _iso(_at(100))}]), evidence_root=fixture.root)
            with self.assertRaisesRegex(recon.TimeReconciliationError, "类别非法"):
                fixture.run(manual_classification=write_manual([{**good, "category": "unclassified"}]), evidence_root=fixture.root)
            with self.assertRaisesRegex(recon.TimeReconciliationError, "重叠"):
                fixture.run(manual_classification=write_manual([good, {**good, "start_utc": _iso(_at(150)), "end_utc": _iso(_at(200))}]), evidence_root=fixture.root)

    def test_window_clips_spans_and_keeps_ledger_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.run(since_utc=_iso(_at(75)), until_utc=_iso(_at(100)))
            totals = _totals_minutes(receipt)
            # 审计区间被裁剪到 75～80，监督器 run 裁剪到 90～100，其余按推断。
            self.assertEqual(totals["vc0_execution"], 5.0)
            self.assertEqual(totals["vc1_execution"], 10.0)
            self.assertEqual(totals["campaign_creation"], 10.0)
            self.assertEqual(receipt["wall_clock_seconds"], 25 * 60)
            self.assertTrue(all(i["ledger_stopped"] for i in receipt["intervals"]))

    def test_cli_writes_receipt_once_and_signals_unclassified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            output = fixture.root / "out" / "time.json"
            code = recon.main([
                "reconcile-upgrade-time",
                "--data-root", str(fixture.data),
                "--since", _iso(_at(0)),
                "--until", _iso(_at(240)),
                "--git-log-file", str(fixture.git_log),
                "--output", str(output),
            ])
            self.assertEqual(code, 3)
            payload = json.loads(output.read_text("utf-8"))
            self.assertEqual(payload["schema_version"], recon.SCHEMA_VERSION)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            code = recon.main([
                "reconcile-upgrade-time",
                "--data-root", str(fixture.data),
                "--since", _iso(_at(0)),
                "--until", _iso(_at(240)),
                "--output", str(output),
            ])
            self.assertEqual(code, 2)

    @unittest.skipUnless(shutil.which("git"), "需要 git")
    def test_git_repo_mode_reads_committer_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            repo = fixture.root / "repo"
            repo.mkdir(mode=0o700)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
            for minutes, subject in ((60, "fix(codex): 修一处"), (100, "chore(codex): 登记后继")):
                (repo / "f.txt").write_text(subject, "utf-8")
                subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True, env=env)
                stamp = _iso(_at(minutes))
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "-q", "-m", subject],
                    check=True,
                    env={**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
                )
            receipt = fixture.run(git_log_file=None, git_repo=repo)
            kinds = sorted(e["kind"] for e in receipt["events"] if e["source"] == "git")
            self.assertEqual(kinds, ["commit_registration", "commit_repair"])
            self.assertEqual(receipt["sources"]["git"]["mode"], "repo")
            # 100 分钟处的登记提交落在 VC-1 监督器 run 内，证据区间优先于推断。
            covering = [i for i in receipt["intervals"] if i["start_utc"] <= _iso(_at(100)) < i["end_utc"]]
            self.assertEqual(covering[0]["category"], "vc1_execution")

    def test_gap_before_deployment_respects_thresholds(self) -> None:
        """没有提交时，停线到部署之间的空档按创建阈值与空闲阈值三分。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = ReconciliationFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.run(git_log_file=None)
            self.assertEqual(_totals_minutes(receipt)["deployment"], 20.5)
            receipt = fixture.run(git_log_file=None, campaign_creation_threshold_minutes=10)
            totals = _totals_minutes(receipt)
            self.assertEqual(totals["deployment"], 0.5)
            self.assertEqual(totals["unclassified"], 150.0)
            receipt = fixture.run(git_log_file=None, campaign_creation_threshold_minutes=10, idle_threshold_minutes=15)
            totals = _totals_minutes(receipt)
            # 停线到部署的 20 分钟长于空闲阈值记 idle；窗口尾部没有终点事件，仍是 unclassified。
            self.assertEqual(totals["idle"], 20.0)
            self.assertEqual(totals["unclassified"], 130.0)
            self.assertEqual(receipt["status"], "unclassified_present")


if __name__ == "__main__":
    unittest.main()
