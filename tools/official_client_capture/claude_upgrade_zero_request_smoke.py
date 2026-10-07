"""Claude 零请求 smoke（Claude 升级控制面设计 B5a-09）：在 staging 夹具上把 Claude 入口要用的账务、账本与总账命令跑一遍，
证明请求数为零。

对照 Codex 的 ``codex_upgrade_zero_request_smoke``（A0a-15）：同一收据合同 ``zero-request-smoke/v1``（多一个
``client``）、同一种 socket 守卫（任何连接尝试都让 smoke 失败关闭，收据里的 ``network_attempts = 0`` 可复核）、同一
命令行与只写一次的收据。入口编排器（``entry.sh`` 的 ``zero-request-smoke`` 一步）按客户端表调用本模块。

覆盖的命令（全部是 Claude 升级实际走的那一份实现）：

* provenance（``claude_upgrade_live_request_provenance``）：一组证据根的核算（中继有效连接里的模型请求计入、token
  refresh 记辅助、GET 不计）与 Claude Formal Campaign 的核算；
* 计时账本（共享 ``codex_upgrade_timing_ledger``，Claude v2 计划）：创建、阶段事件，以及用上一步 Campaign 核算的
  ``live-request-provenance/v2`` 收据关闭账本；
* fixture_only 项目总账（共享 ``codex_upgrade_project_ledger``，Claude v2 plan）：创建、注册 Claude Campaign、消费者
  门禁、批次、补齐与修复收据；
* Claude 根因码表（共享加载器）：加载、按 client 区分根因 ID、Claude Campaign ID 作维度值被拒；
* 编排器只导入不调用：经 Claude 适配器的编排器门面解析 ``claude_upgrade``，只核对服务面里的模块坐标与异常类，
  不调用编排器的任何函数。

Codex smoke 里的 attempt 审计、时间对账与处置清单是 Codex 专属工具（Claude 身份策略忽略它们），Claude 没有对应实现，
不在本 smoke 里。夹具很小，在本模块内联构造，不导入测试目录。所有步骤只读夹具或写入 staging 夹具与收据，不触碰生产
Campaign；本模块按 ``claude_upgrade_`` 前缀归 Claude 身份的 control 层，不导入被 Claude 身份策略忽略的文件。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_root_cause as root_cause
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import upgrade_client_adapter

SCHEMA_VERSION = "zero-request-smoke/v1"
CLIENT = "claude"
ORCHESTRATOR_MODULE = "tools.official_client_capture.claude_upgrade"
# 夹具里的版本与 Campaign：只用于 staging 夹具，不对应任何真实升级。
BASELINE_VERSION = "2.1.226"
TARGET_VERSION = "2.1.285"
CAMPAIGN_ID = "claude-smoke-formal"
JOB_ID = "official-relay-smoke"


class SmokeError(RuntimeError):
    """smoke 步骤失败或出现网络连接尝试。"""


class _NetworkGuard:
    """把 socket.connect 换成失败关闭，记录任何连接尝试（与 Codex smoke 同一做法）。"""

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
        report.append({"step": name, "status": "failed", "error": f"{type(error).__name__}: {error}",
                       "seconds": round(time.monotonic() - started, 3)})
        raise SmokeError(f"步骤 {name} 失败：{error}") from error
    report.append({"step": name, "status": "passed", "seconds": round(time.monotonic() - started, 3)})
    return result


def _write_private(path: Path, raw: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


# --------------------------------------------------------------------------- 夹具


def _http_request(method: str, target: str, host: str, body: bytes = b"") -> bytes:
    """一条 HTTP/1.1 请求的客户端字节（Content-Length 定长，keep-alive 连续发送）。"""

    head = f"{method} {target} HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode("ascii") + body


def _relay_fixture(root: Path) -> Path:
    """FW-E 中继证据根：连接 1 两次模型请求加一次 GET（不计），连接 2 一次 token refresh（辅助）。"""

    messages = _http_request("POST", "/v1/messages?beta=true", provenance.MODEL_HOST,
                             json.dumps({"model": "claude-sonnet-5", "messages": []}).encode("utf-8"))
    connections = {
        1: messages + _http_request("GET", "/api/hello", provenance.MODEL_HOST) + messages,
        2: _http_request("POST", "/v1/oauth/token", "platform.claude.com", b"grant_type=refresh_token"),
    }
    for connection_id, raw in connections.items():
        _write_private(root / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin", raw)
    manifest = {
        "schema_version": provenance.RELAY_MANIFEST_SCHEMA,
        "production_forwarding_enabled": True,
        "connections": [{"connection_id": key, "valid": True} for key in connections],
    }
    _write_private(root / "relay" / "relay.json", json.dumps(manifest, sort_keys=True).encode("utf-8"))
    return root


def _campaign_fixture(data: Path, relay_root: Path) -> Path:
    """Claude Formal Campaign 的最小布局：总计划与摘要、一个 official Job、一次登记了该证据根的 attempt。"""

    campaign_dir = data / "evidence" / "campaigns" / CAMPAIGN_ID
    manifest = {
        "schema_version": provenance.CAMPAIGN_SCHEMA,
        "client": CLIENT,
        "campaign_id": CAMPAIGN_ID,
        "campaign_mode": "formal",
        "target_version": TARGET_VERSION,
        "configuration": {"platform": "linux/arm64"},
        "jobs": [{"id": JOB_ID, "phase": "official", "evidence_roots": [str(relay_root)]}],
    }
    raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
    _write_private(campaign_dir / "campaign.json", raw)
    _write_private(campaign_dir / "campaign.sha256", (hashlib.sha256(raw).hexdigest() + "\n").encode("ascii"))
    attempt = campaign_dir / "official" / "attempts" / "a1"
    _write_private(attempt / "reservation.json", b"{}")
    _write_private(attempt / f"job-{JOB_ID}.json",
                   json.dumps({"id": JOB_ID, "evidence_roots": [str(relay_root)]}, sort_keys=True).encode("utf-8"))
    return campaign_dir


# --------------------------------------------------------------------------- smoke


def run_smoke(staging_root: Path, *, observed_at_utc: str | None = None) -> dict[str, Any]:
    # 账本关闭只收绝对路径的 provenance 收据：夹具根先取绝对路径（不解引用符号链接）。
    staging_root = Path(staging_root).absolute()
    if project_ledger.STAGING_DIR_NAME not in staging_root.resolve(strict=False).parts:
        raise SmokeError("smoke 夹具根必须位于 staging 目录树内")
    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_root.chmod(0o700)
    adapter = upgrade_client_adapter.adapter_for(CLIENT)
    report: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    with _NetworkGuard() as guard:
        # provenance：一组证据根与 Claude Formal Campaign
        data = staging_root / "provenance" / "data"
        relay_root = _step(report, "provenance.fixture", lambda: _relay_fixture(data / "captures" / JOB_ID))

        def collect_roots() -> dict[str, Any]:
            result = provenance.collect_root_provenance([relay_root])
            observed = (result["precise_total"], dict(result["auxiliary_by_category"]), list(result["unresolved_roots"]))
            _require(observed == (2, {"token_refresh": 1}, []),
                     f"证据根核算应为模型请求 2、token refresh 1、没有未决根，实际 {observed}")
            return result

        roots = _step(report, "provenance.collect-root", collect_roots)
        summary["provenance_roots"] = {"precise_total": roots["precise_total"],
                                       "model_requests_by_endpoint": roots["model_requests_by_endpoint"],
                                       "auxiliary_by_category": roots["auxiliary_by_category"]}
        campaign_dir = _step(report, "provenance.campaign-fixture", lambda: _campaign_fixture(data, relay_root))

        def collect_campaign() -> dict[str, Any]:
            result = provenance.collect_campaign_provenance(campaign_dir, formal_campaign_id=CAMPAIGN_ID,
                                                            estimation_policy="upper_bound_from_sibling")
            _require((result["client"], result["status"], result["precise_total"]) == (CLIENT, "complete", 2),
                     f"Campaign 核算应为 claude／complete／2，实际 {result['client']}／{result['status']}／{result['precise_total']}")
            return result

        campaign_receipt = _step(report, "provenance.collect-campaign", collect_campaign)
        summary["provenance_campaign"] = {"status": campaign_receipt["status"], "precise_total": campaign_receipt["precise_total"],
                                          "estimated_total": campaign_receipt["estimated_total"],
                                          "counting_rule": campaign_receipt["counting_rule"]}
        # Claude 根因码表（后两步要用它生成结构化根因 ID）
        codes = _step(report, "root-cause.load-codes", lambda: root_cause.load_codes(adapter.root_cause_codes_path))
        summary["root_cause_codes_sha256"] = codes["codes_sha256"]

        def root_cause_dimensions() -> dict[str, Any]:
            def identifier(client: str) -> str:
                return root_cause.structured_root_cause(
                    component="supervisor", stable_error_code="campaign-run.action-failed", failed_step="smoke",
                    stable_dimensions={"phase": "VC-1", "client": client}, codes=codes)

            _require(identifier("claude") != identifier("codex"), "同码同步骤的根因 ID 没有按 client 区分")
            try:
                adapter.reject_volatile_dimensions({"phase": "VC-1", "client": CLIENT, "scenario": "claude-code-2_1_285-formal-x"})
            except root_cause.RootCauseError:
                return {"root_cause_id": identifier("claude"), "volatile_campaign_id_rejected": True}
            raise SmokeError("Claude Campaign ID 作根因维度值没有被拒")

        dimensions = _step(report, "root-cause.client-dimensions", root_cause_dimensions)
        summary["root_cause"] = dimensions
        # 计时账本：Claude v2 计划，用 Campaign 核算收据关闭
        close_root = staging_root / "ledger-close"
        close_root.mkdir(mode=0o700, exist_ok=True)

        def close_ledger() -> dict[str, Any]:
            ledger_dir = close_root / "ledger"
            timing_ledger.create_ledger(
                ledger_dir, upgrade_id="claude-smoke-upgrade", baseline_version=BASELINE_VERSION,
                target_version=TARGET_VERSION, campaign_purpose="production_replacement", evidence_decision="recapture",
                started_at_utc=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"),
                client=CLIENT,
            )
            plan = json.loads((ledger_dir / "ledger.json").read_text(encoding="utf-8"))
            _require(timing_ledger.plan_client(plan) == CLIENT, "计时账本计划不是 Claude 的 v2 计划")
            timing_ledger.append_event(ledger_dir, event_id="vc0-done", phase="VC-0", event_type="stage_completed",
                                       next_action="启动 VC-1")
            timing_ledger.append_event(ledger_dir, event_id="vc1-start", phase="VC-1", event_type="stage_started",
                                       next_action="派发")
            receipt = _write_private(close_root / "provenance.json",
                                     (json.dumps(campaign_receipt, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            return timing_ledger.close_campaign_ledger(ledger_dir, root_cause_id=dimensions["root_cause_id"],
                                                       provenance_receipt=receipt)

        close_result = _step(report, "ledger.close-campaign-ledger", close_ledger)
        summary["ledger_close"] = {"status": close_result["status"], "resulting_total": close_result["resulting_total"]}
        # fixture_only 项目总账（Claude v2 plan）全链
        ledger_data = staging_root / "project-ledger"
        ledger_data.mkdir(mode=0o700, exist_ok=True)

        def project_ledger_chain() -> dict[str, Any]:
            ledger_root = project_ledger.create_fixture_ledger(ledger_data, client=CLIENT)
            _require(ledger_root.name == adapter.project_ledger_dir_name, "fixture 总账目录名不是 Claude 的总账目录名")
            smoke_campaign = ledger_data / "campaigns" / "claude-smoke-campaign"
            smoke_campaign.mkdir(parents=True, mode=0o700)
            (ledger_data / "campaigns").chmod(0o700)
            _write_private(smoke_campaign / "campaign.json", (json.dumps(
                {"client": CLIENT, "campaign_id": "claude-smoke-campaign", "campaign_mode": "formal",
                 "target_version": TARGET_VERSION}, sort_keys=True) + "\n").encode("utf-8"))
            registration = project_ledger.register_existing_campaign(smoke_campaign)
            admitted = {command: project_ledger.assert_campaign_admitted(smoke_campaign, command=command, require=True) is not None
                        for command in sorted(project_ledger.CONSUMER_COMMANDS)}
            with project_ledger.campaign_ledger_lock(smoke_campaign) as ledger_dir:
                project_ledger.write_batch(
                    ledger_dir, operation_id="claude-smoke-rec-1", event_type="reconciliation_committed",
                    payload={"campaign_id": "claude-smoke-campaign",
                             "request": {"status": "resolved", "identity_keys": ["claude-smoke-k1"], "estimated_delta": 0},
                             "root_cause": {"root_cause_id": dimensions["root_cause_id"]}},
                    source={"kind": "campaign_event", "sha256": "0" * 64},
                )
            reconciled = project_ledger.reconcile_project_ledger(ledger_root, campaign_dir=smoke_campaign)
            repair = project_ledger.record_root_cause_repair(
                ledger_root, root_cause_id=dimensions["root_cause_id"], kind="code",
                bindings={"fix_commit_sha": "0" * 40, "regression_receipt_sha256": "1" * 64, "deployment_receipt_sha256": "2" * 64},
            )
            head = project_ledger.replay_head(ledger_root)
            return {"registration": registration["status"], "admitted": admitted, "reconciled": reconciled["head_sequence"],
                    "repair": repair["operation_id"], "precise_total": head["precise_total"], "blocked": head["blocked"]}

        summary["project_ledger"] = _step(report, "project-ledger.chain", project_ledger_chain)

        def orchestrator_import_only() -> dict[str, Any]:
            module = importlib.import_module(ORCHESTRATOR_MODULE)
            # 只解析服务面里的模块坐标与异常类，不调用编排器的任何函数（按字面形式 adapter.orchestrator.<原名> 访问）。
            _require(Path(adapter.orchestrator.__file__).resolve() == Path(module.__file__).resolve(),
                     "适配器的编排器门面没有指向 claude_upgrade")
            _require(adapter.orchestrator.ConfigurationError is module.ConfigurationError, "编排器门面解析出的异常类与编排器模块不一致")
            _require(Path(module.__file__).name in adapter.orchestrator_basenames, "编排器文件名不在适配器登记的 CLI 基名里")
            return {"module": ORCHESTRATOR_MODULE, "sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}

        summary["orchestrator"] = _step(report, "orchestrator.import-only", orchestrator_import_only)
    return {
        "schema_version": SCHEMA_VERSION,
        "client": CLIENT,
        "observed_at_utc": observed_at_utc or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "staging_root": str(staging_root),
        "network_guard": "socket.connect / connect_ex 被替换为失败关闭",
        "network_attempts": len(guard.attempts),
        "live_request_count": 0,
        "steps": report,
        "summary": summary,
        "status": "passed" if all(step["status"] == "passed" for step in report) and not guard.attempts else "failed",
    }


def write_receipt(payload: dict[str, Any], path: Path) -> Path:
    """只写一次的收据（与 Codex smoke 的收据写法相同：已存在即拒绝，目录 0700、文件 0600）。"""

    path = Path(path)
    if path.exists() or path.is_symlink():
        raise SmokeError(f"收据已存在，不得覆盖：{path}")
    return _write_private(path, (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude 零请求 smoke：在 staging 夹具上跑 Claude 入口的账务、账本与总账命令。")
    parser.add_argument("--staging-root", type=Path, required=True, help="staging 树内的夹具根，必须尚不存在或为空")
    parser.add_argument("--output", type=Path, required=True, help="收据路径（如 <data>/audit/zero-request-smoke-<stamp>.json）")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.staging_root.exists() and any(arguments.staging_root.iterdir()):
            raise SmokeError("smoke 夹具根必须为空目录或不存在")
        if arguments.output.exists() or arguments.output.is_symlink():
            raise SmokeError(f"收据已存在，不得覆盖：{arguments.output}")
        receipt = run_smoke(arguments.staging_root)
        write_receipt(receipt, arguments.output)
    except (SmokeError, OSError) as error:
        print(f"零请求 smoke 失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps({"status": receipt["status"], "network_attempts": receipt["network_attempts"], "steps": len(receipt["steps"]),
                      "output": str(arguments.output)}, ensure_ascii=False))
    return 0 if receipt["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
