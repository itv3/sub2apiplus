"""零请求 smoke（A0a-15）：在 staging 夹具上把 A0a 的全部新命令跑一遍，证明请求数为零。

部署到 ARM64 后执行一次，收据入 ``audit/``。夹具复用离线测试的夹具类构造在
``<data>/staging/<name>/`` 下；整个过程用 socket 守卫禁止任何网络连接，任何连接
尝试都会让 smoke 失败关闭，因此收据里的 ``network_attempts = 0`` 是可复核的证明。

覆盖的命令：provenance collect-campaign／audit-project、attempt 审计、时间对账、处置
清单、账本关闭、fixture_only 项目总账（创建、注册、补齐、消费者门禁、修复收据）、
根因编码。所有步骤只读证据或写入 staging 夹具与收据，不触碰生产 Campaign。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from tools.official_client_capture import codex_upgrade_campaign_disposition as disposition
from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_official_attempt_audit as attempt_audit
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_root_cause as root_cause
from tools.official_client_capture import codex_upgrade_time_reconciliation as reconciliation
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
SCHEMA_VERSION = "zero-request-smoke/v1"


class SmokeError(RuntimeError):
    """smoke 步骤失败或出现网络连接尝试。"""


class _NetworkGuard:
    """把 socket.connect 换成失败关闭，记录任何连接尝试。"""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._original_connect = socket.socket.connect
        self._original_connect_ex = socket.socket.connect_ex

    def __enter__(self) -> "_NetworkGuard":
        guard = self

        def blocked(self_socket: socket.socket, address: Any) -> None:
            guard.attempts.append(repr(address))
            raise SmokeError(f"零请求 smoke 期间出现网络连接尝试：{address!r}")

        socket.socket.connect = blocked  # type: ignore[assignment]
        socket.socket.connect_ex = blocked  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc: object) -> None:
        socket.socket.connect = self._original_connect  # type: ignore[assignment]
        socket.socket.connect_ex = self._original_connect_ex  # type: ignore[assignment]


def _step(report: list[dict[str, Any]], name: str, action: Callable[[], Any]) -> Any:
    started = time.monotonic()
    try:
        result = action()
    except Exception as error:  # noqa: BLE001 - smoke 要如实记录失败步骤
        report.append({"step": name, "status": "failed", "error": f"{type(error).__name__}: {error}", "seconds": round(time.monotonic() - started, 3)})
        raise SmokeError(f"步骤 {name} 失败：{error}") from error
    report.append({"step": name, "status": "passed", "seconds": round(time.monotonic() - started, 3)})
    return result


def run_smoke(staging_root: Path, *, observed_at_utc: str | None = None) -> dict[str, Any]:
    # 夹具模块会反向导入 provenance 等生产模块；必须等生产模块初始化完成后
    # 再加载，避免 provenance → pre-a3 → smoke → 测试夹具的循环导入。
    from tools.official_client_capture.tests import (
        test_codex_upgrade_campaign_disposition as disposition_tests,
    )
    from tools.official_client_capture.tests import (
        test_codex_upgrade_live_request_provenance as provenance_tests,
    )
    from tools.official_client_capture.tests import (
        test_codex_upgrade_official_attempt_audit as audit_tests,
    )
    from tools.official_client_capture.tests import (
        test_codex_upgrade_time_reconciliation as reconciliation_tests,
    )
    from tools.official_client_capture.tests import (
        test_codex_upgrade_timing_ledger_close as close_tests,
    )

    staging_root = Path(staging_root)
    if project_ledger.STAGING_DIR_NAME not in staging_root.resolve(strict=False).parts:
        raise SmokeError("smoke 夹具根必须位于 staging 目录树内")
    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_root.chmod(0o700)
    report: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    with _NetworkGuard() as guard:
        # provenance：Campaign 级与项目级
        prov_fixture = provenance_tests.ProvenanceFixture(staging_root / "provenance")
        _step(report, "provenance.fixture", prov_fixture.build)
        prov_receipt = _step(
            report,
            "provenance.collect-campaign",
            lambda: provenance.collect_campaign_provenance(prov_fixture.campaign_dir, formal_campaign_id="c1", estimation_policy="upper_bound_from_sibling_or_turn_ratio"),
        )
        summary["provenance"] = {"status": prov_receipt["status"], "precise_total": prov_receipt["precise_total"], "estimated_total": prov_receipt["estimated_total"]}
        project_receipt = _step(
            report,
            "provenance.audit-project",
            lambda: provenance.audit_project(prov_fixture.root / "data", since_utc="2026-01-01T00:00:00Z", estimation_policy="upper_bound_from_sibling_or_turn_ratio"),
        )
        summary["project_audit"] = {"status": project_receipt["status"], "campaigns": len(project_receipt["campaigns"])}
        # attempt 审计
        audit_fixture = audit_tests.AttemptAuditFixture(staging_root / "attempt-audit")
        _step(report, "attempt-audit.fixture", audit_fixture.build)
        audit_receipt = _step(report, "attempt-audit.audit-official-attempt", lambda: audit_fixture.run(expected_track_models={"main": "gpt-5.5", "lite": "gpt-6-astra"}))
        summary["attempt_audit"] = {"status": audit_receipt["status"], "failed_sections": audit_receipt["failed_sections"]}
        # 时间对账
        recon_fixture = reconciliation_tests.ReconciliationFixture(staging_root / "time")
        _step(report, "time.fixture", recon_fixture.build)
        recon_receipt = _step(report, "time.reconcile-upgrade-time", recon_fixture.run)
        summary["time_reconciliation"] = {"status": recon_receipt["status"], "wall_clock_seconds": recon_receipt["wall_clock_seconds"], "unclassified_seconds": recon_receipt["unclassified_seconds"]}
        # 处置清单
        disp_fixture = disposition_tests.DispositionFixture(staging_root / "disposition")
        _step(report, "disposition.fixture", disp_fixture.build)
        disp_receipt = _step(report, "disposition.build-campaign-disposition", lambda: disposition.build_disposition(disp_fixture.data, primary_reuse_source="c-fresh", backup_reuse_source="c-new-window"))
        summary["disposition"] = disp_receipt["summary"]
        # 账本关闭
        close_root = staging_root / "ledger-close"
        close_root.mkdir(mode=0o700, exist_ok=True)

        def close_ledger() -> dict[str, Any]:
            ledger_dir = close_root / "ledger"
            timing_ledger.create_ledger(ledger_dir, upgrade_id="smoke-upgrade", baseline_version="0.151.0", target_version="0.154.0", campaign_purpose="production_replacement", evidence_decision="recapture", started_at_utc=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"))
            timing_ledger.append_event(ledger_dir, event_id="vc0-done", phase="VC-0", event_type="stage_completed", next_action="启动 VC-1")
            timing_ledger.append_event(ledger_dir, event_id="vc1-start", phase="VC-1", event_type="stage_started", next_action="派发")
            receipt = close_tests._write_provenance(close_root / "prov.json", precise=3, estimated=1)
            return timing_ledger.close_campaign_ledger(ledger_dir, root_cause_id=root_cause.structured_root_cause(component="supervisor", stable_error_code="campaign-run.action-failed", failed_step="smoke", stable_dimensions={"phase": "VC-1"}), provenance_receipt=receipt)

        close_result = _step(report, "ledger.close-campaign-ledger", close_ledger)
        summary["ledger_close"] = {"status": close_result["status"], "resulting_total": close_result["resulting_total"]}
        # fixture_only 项目总账全链
        ledger_data = staging_root / "project-ledger"
        ledger_data.mkdir(mode=0o700, exist_ok=True)

        def project_ledger_chain() -> dict[str, Any]:
            ledger_root = project_ledger.create_fixture_ledger(ledger_data)
            campaign_dir = ledger_data / "campaigns" / "smoke-campaign"
            campaign_dir.mkdir(parents=True, mode=0o700)
            (ledger_data / "campaigns").chmod(0o700)
            manifest_path = campaign_dir / "campaign.json"
            manifest_path.write_text(json.dumps({"campaign_id": "smoke-campaign", "campaign_mode": "formal", "target_version": "0.154.0"}, sort_keys=True) + "\n", "utf-8")
            manifest_path.chmod(0o600)
            registration = project_ledger.register_existing_campaign(campaign_dir)
            admitted = {command: project_ledger.assert_campaign_admitted(campaign_dir, command=command, require=True) is not None for command in sorted(project_ledger.CONSUMER_COMMANDS)}
            with project_ledger.campaign_ledger_lock(campaign_dir) as ledger_dir:
                project_ledger.write_batch(ledger_dir, operation_id="smoke-rec-1", event_type="reconciliation_committed", payload={"campaign_id": "smoke-campaign", "request": {"status": "resolved", "identity_keys": ["smoke-k1"], "estimated_delta": 0}, "root_cause": {"root_cause_id": "rc1-smoke"}}, source={"kind": "campaign_event", "sha256": "0" * 64})
            reconciled = project_ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)
            repair = project_ledger.record_root_cause_repair(ledger_root, root_cause_id="rc1-smoke", kind="code", bindings={"fix_commit_sha": "0" * 40, "regression_receipt_sha256": "1" * 64, "deployment_receipt_sha256": "2" * 64})
            head = project_ledger.replay_head(ledger_root)
            return {"registration": registration["status"], "admitted": admitted, "reconciled": reconciled["head_sequence"], "repair": repair["operation_id"], "precise_total": head["precise_total"], "blocked": head["blocked"]}

        summary["project_ledger"] = _step(report, "project-ledger.chain", project_ledger_chain)
        codes = _step(report, "root-cause.load-codes", root_cause.load_codes)
        summary["root_cause_codes_sha256"] = codes["codes_sha256"]
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at_utc": observed_at_utc or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "staging_root": str(staging_root),
        "network_guard": "socket.connect / connect_ex 被替换为失败关闭",
        "network_attempts": len(guard.attempts),
        "live_request_count": 0,
        "steps": report,
        "summary": summary,
        "status": "passed" if all(step["status"] == "passed" for step in report) and not guard.attempts else "failed",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="零请求 smoke：在 staging 夹具上跑全部 A0a 新命令。")
    parser.add_argument("--staging-root", type=Path, required=True, help="staging 树内的夹具根，必须尚不存在或为空")
    parser.add_argument("--output", type=Path, required=True, help="收据路径（如 <data>/audit/zero-request-smoke-<stamp>.json）")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.staging_root.exists() and any(arguments.staging_root.iterdir()):
            raise SmokeError("smoke 夹具根必须为空目录或不存在")
        receipt = run_smoke(arguments.staging_root)
        provenance.write_receipt(receipt, arguments.output)
    except (SmokeError, OSError, provenance.ProvenanceError) as error:
        print(f"零请求 smoke 失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps({"status": receipt["status"], "network_attempts": receipt["network_attempts"], "steps": len(receipt["steps"]), "output": str(arguments.output)}, ensure_ascii=False))
    return 0 if receipt["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
