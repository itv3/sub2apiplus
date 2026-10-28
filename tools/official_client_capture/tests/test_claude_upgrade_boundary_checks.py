"""Claude 候选 attempt 级边界核对（Claude 升级控制面设计 B4-01 f 段）。

不起 Docker、不联网，沿用候选 run 的夹具（``test_claude_upgrade_candidate_capture.CandidateFixture``：临时数据根、docker
替身、宿主命令边界替身、网关事实文件、正式入口门替身，真实的 Campaign 租约与共享监督器）先跑出一个等待封存的候选
attempt，再执行 ``capture-candidate boundary-check``。另加：

* 宿主脚本替身（与候选 run 同一套控制键，按 probe 制造前检拒绝、恢复失败、报告缺失与网关漂移），负例模式的恢复报告
  多 ``account_tokens_unchanged``（控制键 ``tokens_changed`` 造令牌被刷新）；
* 中继替身的 ``ingress-negative`` 分支：核对宿主运行凭据后按控制键写负例清单（``accepted_cases`` 造负例被放行、
  ``relay_connection`` 造监听端收到连接）。

覆盖：参数与入口门（真实请求确认、Codex 项待实现、只认 awaiting_receipts、网关未就绪零写入）、第三方负例通过即写收据且
校验入口与 seal 同一判据、收据已存在时零写入复用、每类失败不写收据且可重跑、恢复失败与网关漂移判环境污染（Campaign
标记不带 attempt_id）、校验入口逐项拒绝篡改，以及中继 ``ingress-negative`` 子命令本身（真实监听端与真实 HTTP 网关替身）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_fw_e_relay as relay
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_boundary_checks as boundary_checks
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_boundary_receipt_fixture
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests

CANDIDATE = candidate_tests.CANDIDATE
THIRD_PARTY = boundary_checks.CHECK_THIRD_PARTY

NEGATIVE_HOST = r'''#!/usr/bin/env python3
"""claude_run_candidate_capture.sh 的离线替身（含负例模式）：记下环境，执行中继命令并按控制文件写恢复报告。"""
import json, os, subprocess, sys
script, args = sys.argv[1], sys.argv[2:]
control = json.load(open(os.environ["FAKE_CANDIDATE_CONTROL"], encoding="utf-8"))
probe = os.environ["PROBE_ID"]
trigger = os.environ["GATEWAY_TRIGGER"]
with open(os.environ["FAKE_CANDIDATE_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "script": script, "probe": probe, "target": args[2], "action": args[5], "trigger": trigger,
        "capture_container": os.environ.get("CAPTURE_CONTAINER"), "gate": os.environ.get("CLAUDE_CANDIDATE_CAPTURE"),
        "account": os.environ.get("ACCOUNT_ID"), "api_key": os.environ.get("API_KEY_ID"), "run_id": os.environ.get("RUN_ID"),
        "output_root": os.environ.get("OUTPUT_ROOT"), "runtime_dir": os.environ.get("CANDIDATE_RUNTIME_DIR"),
        "ca_cert": os.environ.get("CA_CERT"), "relay_port": os.environ.get("RELAY_PORT"),
    }) + "\n")
if "--dry-run" in args:
    os.execvp(args[0], args)
if probe in control.get("precheck_fail", []):
    print("只读前检拒绝", file=sys.stderr)
    sys.exit(1)
code = subprocess.run(args).returncode
restored = probe not in control.get("restoration_fail", [])
if probe in control.get("gateway_drift", []):
    path = os.environ["FAKE_GATEWAY_FILE"]
    facts = json.load(open(path, encoding="utf-8"))
    facts["residual_candidate_proxies"] = 1
    json.dump(facts, open(path, "w", encoding="utf-8"))
if probe not in control.get("no_report", []):
    report_dir = os.path.join(os.path.dirname(os.environ["OUTPUT_ROOT"]), "candidate-gateway")
    os.makedirs(report_dir, mode=0o700, exist_ok=True)
    status = "restoration_failed" if not restored else ("complete" if code == 0 else "capture_failed")
    report = {
        "schema_version": "claude-candidate-gateway-restoration/v1", "status": status, "run_id": os.environ["RUN_ID"],
        "probe_id": probe, "account_id": int(os.environ["ACCOUNT_ID"]), "api_key_id": int(os.environ["API_KEY_ID"]),
        "trigger": trigger, "relay_port": int(os.environ["RELAY_PORT"]), "capture_exit_code": code,
        "account_proxy_original": "3|NULL", "account_proxy_restored_equal": restored, "account_gate_restored_equal": True,
        "hosts_sha256_equal": True, "ca_bundle_sha256_equal": True, "keeper_was_running": False, "runtime_secrets_removed": True,
        "started_at_utc": "2026-10-07T00:00:00Z", "ended_at_utc": "2026-10-07T00:00:01Z",
    }
    if trigger == "ingress-negative":
        report["account_tokens_unchanged"] = not control.get("tokens_changed", False)
    with open(os.path.join(report_dir, probe + ".json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle)
sys.exit(97 if not restored else code)
'''

NEGATIVE_RELAY_HEAD = r'''#!/usr/bin/env python3
"""中继替身：ingress-negative 分支按控制文件写负例清单；其余参数落到候选 capture 替身。"""
import hashlib, json, os, sys
_args = sys.argv[1:]
if _args and _args[0] == "ingress-negative":
    def _value(flag):
        return _args[_args.index(flag) + 1] if flag in _args else None
    _control = json.load(open(os.environ["FAKE_CANDIDATE_CONTROL"], encoding="utf-8"))
    _container = os.environ.get("FAKE_DOCKER_CONTAINER")
    with open(os.environ["FAKE_RELAY_CALLS"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"action": "ingress-negative", "execute": "--execute" in _args, "container": _container,
                                 "run_id": _value("--run-id"), "model": _value("--model"), "version": _value("--expected-version")}) + "\n")
    _receipt = _value("--host-runtime-receipt")
    _digest = hashlib.sha256(open(_receipt, "rb").read()).hexdigest()
    if _digest != _value("--host-runtime-receipt-sha256") or json.load(open(_receipt, encoding="utf-8"))["container"]["name"] != _container:
        sys.exit(2)
    _output = _value("--output-root")
    os.makedirs(_output, mode=0o700)
    _cases = []
    for _case_id, _expected in __CASES__:
        _status = 200 if _case_id in _control.get("accepted_cases", []) else _expected[0]
        _cases.append({"case_id": _case_id, "http_status": _status, "expected_status": _expected, "rejected": _status in _expected,
                       "transport_error": None, "error_type": "invalid_request_error"})
    _connections = [{"method": "CONNECT", "connect_authority": "api.anthropic.com:443"}] if _control.get("relay_connection") else []
    _ok = all(item["rejected"] for item in _cases) and not _connections
    _manifest = {
        "schema_version": "claude-code-fw-e-ingress-negative/v1", "side": "candidate", "run_id": _value("--run-id"),
        "probe_id": _value("--probe-id"), "status": "complete" if _ok else "failed",
        "candidate_gateway": {"account_id": 7, "api_key_id": 9, "trigger": "ingress-negative", "oauth_credentials_read": False},
        "runtime": {"host_runtime_receipt": {"sha256": _digest, "run_nonce": _value("--run-nonce")}},
        "upstream": {"live_requests": False, "production_forwarding_enabled": False},
        "cases": _cases, "relay_connections": _connections, "secret_scan": {"passed": True}, "m_binding": {"complete": _ok},
    }
    with open(os.path.join(_output, "ingress-negative-manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(_manifest, handle)
    sys.exit(0 if _ok else 2)
'''.replace("__CASES__", json.dumps([[case["case_id"], list(case["expected_status"])] for case in relay.INGRESS_NEGATIVE_CASES]))


class BoundaryFixture(candidate_tests.CandidateFixture):
    """候选 run 夹具 + 负例模式的宿主脚本与中继替身；``formal_attempt`` 先跑出一个等待封存的候选 attempt。"""

    def setUp(self) -> None:
        super().setUp()
        (self.tool_copy / "claude_fw_e_relay.py").write_text(NEGATIVE_RELAY_HEAD + candidate_tests.CANDIDATE_RELAY, encoding="utf-8")
        self.fake_host.write_text(NEGATIVE_HOST, encoding="utf-8")

    @contextlib.contextmanager
    def boundary_patched(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.candidate_patched())
            stack.enter_context(
                mock.patch.object(candidate_capture, "verify_service_image", lambda *_a, **_k: candidate_tests.SERVICE_IMAGE_ID)
            )
            yield

    def formal_attempt(self) -> tuple[Path, str, Path]:
        campaign = self.write_candidate_campaign()
        code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual((code, result.get("status")), (2, "awaiting_receipts"), stderr)
        attempt_root = campaign / "candidates" / CANDIDATE / "attempts" / result["attempt_id"]
        self.set_candidate_control({})
        return campaign, result["attempt_id"], attempt_root

    def run_check(self, campaign: Path, attempt_id: str, *extra: str, check: str = THIRD_PARTY) -> tuple[int, dict, str]:
        with self.boundary_patched():
            code, result, stderr = self.run_main(
                [
                    "capture-candidate", "boundary-check", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE,
                    "--attempt-id", attempt_id, "--check", check, *extra,
                ]
            )
        return code, result or {}, stderr

    def runs(self, attempt_root: Path, check: str = THIRD_PARTY) -> list[Path]:
        directory = attempt_root / boundary_checks.CLIENT_EVIDENCE_DIRNAME / check / boundary_checks.RUNS_DIRNAME
        return sorted(directory.iterdir()) if directory.is_dir() else []

    def ledgers(self, campaign: Path) -> str:
        return "".join(path.read_text(encoding="utf-8") for path in sorted((campaign / ".supervisor").glob("run-*/events.ndjson")))


class ThirdPartyPassTests(BoundaryFixture):
    def test_pass_writes_receipt_verified_like_seal(self) -> None:
        campaign, attempt_id, attempt_root = self.formal_attempt()
        code, result, stderr = self.run_check(campaign, attempt_id)
        self.assertEqual((code, result.get("status")), (0, "passed"), stderr)
        self.assertEqual((result["live_request_count"], result["failure_codes"]), (0, []))
        receipt_file = boundary_checks.receipt_path(attempt_root, THIRD_PARTY)
        self.assertEqual(result["receipt"], str(receipt_file))
        receipt = boundary_checks.verify_boundary_receipt(attempt_root, THIRD_PARTY, receipt_file)
        attempt = json.loads((attempt_root / "attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {name: receipt[name] for name in ("check_id", "campaign_id", "candidate_id", "attempt_id", "run_nonce", "result")},
            {
                "check_id": THIRD_PARTY, "campaign_id": attempt["campaign_id"], "candidate_id": CANDIDATE, "attempt_id": attempt_id,
                "run_nonce": attempt["run_nonce"], "result": "pass",
            },
        )
        self.assertEqual(receipt["candidate"]["candidate_revision"], 1)
        self.assertEqual(receipt["live_requests"]["count"], 0)
        self.assertEqual({entry["role"] for entry in receipt["evidence"]}, boundary_checks.EVIDENCE_ROLES[THIRD_PARTY])
        self.assertEqual([case["case_id"] for case in receipt["facts"]["cases"]], list(boundary_checks.INGRESS_NEGATIVE_CASE_IDS))
        self.assertEqual((receipt["facts"]["relay_connection_count"], receipt["facts"]["account_tokens_unchanged"]), (0, True))
        # 宿主脚本以负例模式执行：同一网关隔离开关、负例触发方式、固定 probe，目标是抓包容器。
        calls = [call for call in self.candidate_calls() if call["trigger"] == "ingress-negative"]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual((call["action"], call["probe"], call["target"]), ("ingress-negative", "ingress-negative", candidate_tests.CAPTURE))
        self.assertEqual((call["account"], call["api_key"], call["relay_port"]), ("7", "9", "18443"))
        self.assertEqual(call["run_id"], receipt["boundary_run"]["run_id"])
        self.assertTrue(call["output_root"].startswith(str(attempt_root / "client-evidence" / THIRD_PARTY / "runs")))
        self.assertEqual(call["ca_cert"], "/opt/mitm/mitmproxy-ca-cert.pem")
        relay_calls = [item for item in self.relay_calls() if item.get("action") == "ingress-negative"]
        self.assertEqual([(item["execute"], item["container"], item["model"]) for item in relay_calls], [(True, candidate_tests.CAPTURE, "claude-sonnet-5")])
        # 监督器执行：操作名是 job:boundary-<check>:…（出口守护下照常建立采集停止绑定）。
        self.assertIn(f'"job:boundary-{THIRD_PARTY}:step-1:attempt-1"', self.ledgers(campaign))
        # 运行结论与收据同一份计数；seal 同一校验入口（模块级函数）。
        run_root = attempt_root / receipt["boundary_run"]["path"]
        verdict = json.loads((run_root / boundary_checks.VERDICT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual((verdict["result"], verdict["environment_contaminated"]), ("pass", False))
        self.assertFalse((campaign / capture.CONTAMINATION_MARKER).exists())
        # 再跑：收据已存在且合法，零写入复用。
        before = self.runs(attempt_root)
        code, again, stderr = self.run_check(campaign, attempt_id)
        self.assertEqual((code, again.get("status")), (0, "already_passed"), stderr)
        self.assertEqual(self.runs(attempt_root), before)
        self.assertEqual(again["receipt_sha256"], result["receipt_sha256"])


class ThirdPartyFailureTests(BoundaryFixture):
    def assert_failed(self, attempt_root: Path, result: dict, code: int, *failure_codes: str) -> dict:
        self.assertEqual((code, result.get("status")), (1, "failed"), result)
        for failure in failure_codes:
            self.assertIn(failure, result["failure_codes"])
        self.assertFalse(boundary_checks.receipt_path(attempt_root, THIRD_PARTY).exists())
        verdict = json.loads((Path(result["run"]) / boundary_checks.VERDICT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual((verdict["result"], verdict["environment_contaminated"]), ("fail", False))
        return verdict

    def test_failures_leave_no_receipt_and_can_rerun(self) -> None:
        campaign, attempt_id, attempt_root = self.formal_attempt()
        for control, failures in (
            ({"accepted_cases": ["openai-responses"]}, ("case-not-rejected", "relay-incomplete", "relay-step-failed")),
            ({"relay_connection": True}, ("relay-connections",)),
            ({"tokens_changed": True}, ("account-tokens-changed",)),
            ({"precheck_fail": ["ingress-negative"]}, ("host-precheck-refused", "relay-manifest-missing")),
        ):
            with self.subTest(control=control):
                self.set_candidate_control(control)
                code, result, stderr = self.run_check(campaign, attempt_id)
                self.assert_failed(attempt_root, result, code, *failures)
                self.assertIn("不作废候选 attempt", result["next_gate"])
        self.assertFalse((campaign / capture.CONTAMINATION_MARKER).exists())
        self.assertEqual(len(self.runs(attempt_root)), 4)
        # 修好后重跑通过：候选 attempt 不受影响，仍可封存。
        self.set_candidate_control({})
        code, result, stderr = self.run_check(campaign, attempt_id)
        self.assertEqual((code, result.get("status")), (0, "passed"), stderr)
        self.assertEqual(len(self.runs(attempt_root)), 5)
        attempt = json.loads((attempt_root / "attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(attempt["status"], "awaiting_receipts")

    def test_restoration_failure_and_gateway_drift_contaminate(self) -> None:
        for index, (control, failure) in enumerate(
            (
                ({"restoration_fail": ["ingress-negative"]}, "restoration-failed"),
                ({"no_report": ["ingress-negative"]}, "restoration-report-missing"),
                ({"gateway_drift": ["ingress-negative"]}, "gateway-drifted:residual_candidate_proxies"),
            )
        ):
            with self.subTest(control=control):
                self.gateway_file.write_text(json.dumps(candidate_tests.BASE_GATEWAY), encoding="utf-8")
                campaign, attempt_id, attempt_root = self.formal_attempt_named(f"claude-code-2_1_285-c{index}")
                self.set_candidate_control(control)
                code, result, stderr = self.run_check(campaign, attempt_id)
                self.assertEqual((code, result.get("status")), (1, "environment_contaminated"), stderr)
                self.assertIn(failure, result["failure_codes"])
                self.assertIn("environment-cleanup", result["next_gate"])
                self.assertFalse(boundary_checks.receipt_path(attempt_root, THIRD_PARTY).exists())
                marker = json.loads((campaign / capture.CONTAMINATION_MARKER).read_text(encoding="utf-8"))
                self.assertEqual((marker["phase"], marker["candidate_id"]), ("candidate", CANDIDATE))
                self.assertNotIn("attempt_id", marker)
                self.assertEqual((marker["boundary_check"]["check_id"], marker["boundary_check"]["attempt_id"]), (THIRD_PARTY, attempt_id))
                # 污染未隔离之前任何候选写入（含再跑本项）都被拒。
                self.set_candidate_control({})
                code, _again, stderr = self.run_check(campaign, attempt_id)
                self.assertEqual(code, 1)

    def formal_attempt_named(self, campaign_id: str) -> tuple[Path, str, Path]:
        campaign = self.write_candidate_campaign(campaign_id=campaign_id)
        code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual((code, result.get("status")), (2, "awaiting_receipts"), stderr)
        return campaign, result["attempt_id"], campaign / "candidates" / CANDIDATE / "attempts" / result["attempt_id"]


class BoundaryGateTests(BoundaryFixture):
    def test_argument_and_entry_gates_refuse_without_writes(self) -> None:
        campaign, attempt_id, attempt_root = self.formal_attempt()
        for extra, check, message in (
            (("--acknowledge-live-requests",), THIRD_PARTY, "零上游请求"),
            (("--codex-account-id", "3"), THIRD_PARTY, "不接受 --codex-account-id"),
            ((), boundary_checks.CHECK_CODEX, "必须显式确认 --acknowledge-live-requests"),
        ):
            with self.subTest(check=check, extra=extra):
                code, _result, stderr = self.run_check(campaign, attempt_id, *extra, check=check)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
        code, _result, stderr = self.run_check(campaign, "no-such-attempt")
        self.assertEqual(code, 1)
        # 网关未就绪（残留临时代理）：预检拒绝，不建运行目录。
        self.gateway_file.write_text(json.dumps({**candidate_tests.BASE_GATEWAY, "residual_candidate_proxies": 2}), encoding="utf-8")
        code, _result, stderr = self.run_check(campaign, attempt_id)
        self.assertEqual(code, 1)
        self.assertIn("候选网关环境未就绪", stderr)
        self.assertEqual(self.runs(attempt_root), [])
        self.assertFalse([call for call in self.candidate_calls() if call["trigger"] == "ingress-negative"])

    def test_only_awaiting_receipts_attempts(self) -> None:
        campaign = self.write_candidate_campaign()
        self.set_candidate_control({"precheck_fail": ["v4-native-tls-baseline"]})
        code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual(result.get("status"), "failed", stderr)
        self.set_candidate_control({})
        code, _result, stderr = self.run_check(campaign, result["attempt_id"])
        self.assertEqual(code, 1)
        self.assertIn("awaiting_receipts", stderr)

    def test_existing_untrusted_receipt_is_never_overwritten(self) -> None:
        campaign, attempt_id, attempt_root = self.formal_attempt()
        path = claude_boundary_receipt_fixture.write_boundary_receipt(attempt_root, THIRD_PARTY, mutate=lambda item: item.update(result="fail"))
        original = path.read_bytes()
        code, _result, stderr = self.run_check(campaign, attempt_id)
        self.assertEqual(code, 1)
        self.assertIn("已存在但不可信", stderr)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.runs(attempt_root), [self.runs(attempt_root)[0]])


class ReceiptVerificationTests(BoundaryFixture):
    """校验入口逐项拒绝：字段闭集、身份、结论范围、候选事实、运行目录、证据角色与摘要、时刻、计数。"""

    def test_rejections(self) -> None:
        _campaign, _attempt_id, attempt_root = self.formal_attempt()
        cases: list[tuple[str, Any, str]] = [
            ("extra-field", lambda item: item.update(extra=1), "字段闭集"),
            ("candidate-id", lambda item: item.update(candidate_id="other"), "与 attempt 或合同不一致"),
            ("run-nonce", lambda item: item.update(run_nonce="0" * 64), "与 attempt 或合同不一致"),
            ("scope", lambda item: item.update(conclusion_scope="全部通过"), "与 attempt 或合同不一致"),
            ("candidate-facts", lambda item: item["candidate"].update(build_id="other"), "候选事实"),
            ("run-path", lambda item: item["boundary_run"].update(path="client-evidence/other/runs/x"), "运行目录不在"),
            ("issued-early", lambda item: item.update(issued_at_utc="2000-01-01T00:00:00Z"), "早于 attempt 完成"),
            ("role-missing", lambda item: item["evidence"].pop(0), "证据角色"),
            ("digest", lambda item: item["evidence"][0].update(sha256="0" * 64), "缺失或摘要漂移"),
            ("live-count", lambda item: item["live_requests"].update(count=1, by_endpoint={"POST /x": 1}), "真实上游请求必须为 0"),
            ("live-sum", lambda item: item["live_requests"].update(by_endpoint={"POST /x": 3}), "分端点之和"),
            ("escape", lambda item: item["evidence"][0].update(path="../../../../attempt.json"), "规范相对路径"),
        ]
        for name, mutate, message in cases:
            with self.subTest(name=name):
                shutil_rmtree(attempt_root / boundary_checks.CLIENT_EVIDENCE_DIRNAME)
                path = claude_boundary_receipt_fixture.write_boundary_receipt(attempt_root, THIRD_PARTY, mutate=mutate)
                with self.assertRaises(ConfigurationError) as caught:
                    boundary_checks.verify_boundary_receipt(attempt_root, THIRD_PARTY, path)
                self.assertIn(message, str(caught.exception))
        shutil_rmtree(attempt_root / boundary_checks.CLIENT_EVIDENCE_DIRNAME)
        path = claude_boundary_receipt_fixture.write_boundary_receipt(attempt_root, THIRD_PARTY)
        self.assertEqual(boundary_checks.verify_boundary_receipt(attempt_root, THIRD_PARTY, path)["result"], "pass")
        with self.assertRaises(ConfigurationError):
            boundary_checks.verify_boundary_receipt(attempt_root, boundary_checks.CHECK_CODEX, path)
        verdict = next((attempt_root / boundary_checks.CLIENT_EVIDENCE_DIRNAME).rglob(boundary_checks.VERDICT_FILENAME))
        payload = json.loads(verdict.read_text(encoding="utf-8"))
        payload["result"] = "fail"
        verdict.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            boundary_checks.verify_boundary_receipt(attempt_root, THIRD_PARTY, path)


def shutil_rmtree(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path)


class ContractConstantTests(unittest.TestCase):
    def test_constants_agree_with_relay(self) -> None:
        self.assertEqual(boundary_checks.INGRESS_NEGATIVE_CASE_IDS, tuple(relay.ingress_negative_case_ids()))
        self.assertEqual(boundary_checks.INGRESS_NEGATIVE_PROBE_ID, relay.INGRESS_NEGATIVE_PROBE_ID)
        self.assertEqual(boundary_checks.INGRESS_NEGATIVE_TRIGGER, relay.CANDIDATE_TRIGGER_INGRESS_NEGATIVE)
        self.assertEqual(boundary_checks.INGRESS_NEGATIVE_MANIFEST, relay.INGRESS_NEGATIVE_MANIFEST)
        self.assertEqual(boundary_checks.INGRESS_NEGATIVE_SCHEMA, relay.SCHEMA_INGRESS_NEGATIVE)
        self.assertEqual(set(boundary_checks.CHECK_IDS), set(boundary_checks.CONCLUSION_SCOPES))
        self.assertEqual(set(boundary_checks.CHECK_IDS), set(boundary_checks.EVIDENCE_ROLES))

    def test_module_is_control_layer(self) -> None:
        """边界核对编排在 control 层；中继与宿主脚本仍是 wire 层（负例模式让 Claude wire 摘要变化，按 c2 先例接受）。"""

        from tools.official_client_capture import claude_tool_identity_policy as claude_identity

        policy = claude_identity.load_policy()
        self.assertEqual(claude_identity.classify_path(policy, "official_client_capture/claude_upgrade_boundary_checks.py"), "control")
        self.assertEqual(claude_identity.classify_path(policy, "official_client_capture/claude_fw_e_relay.py"), "wire_producer")


# ---------------------------------------------------------------------------
# 中继 ingress-negative 子命令本身：真实监听端＋真实 HTTP 网关替身
# ---------------------------------------------------------------------------


class _Gateway:
    """候选网关替身：按路径与凭据回固定状态码；``leak`` 为真时第一个第三方 /v1/messages 先经账号代理（connect 端口）出站。"""

    def __init__(self, relay_port: int, *, leak: bool = False, accept: str | None = None) -> None:
        self.seen: list[tuple[str, str, str]] = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                key = self.headers.get("x-api-key") or self.headers.get("Authorization", "")
                gateway.seen.append((self.path, self.headers.get("User-Agent", ""), key))
                if not key or "sk-ant-oat01-" in key:
                    status, body = 401, b'{"code":"INVALID_API_KEY","message":"Invalid API key"}'
                elif accept is not None and self.path.startswith(accept):
                    status, body = 200, b'{"ok":true,"echo":"sk-candidate-key"}'
                else:
                    if leak and self.path == "/v1/messages" and not gateway.leaked:
                        gateway.leaked = True
                        # 仿真泄漏：经账号代理发起上游出站（不等应答，避免与单线程的中继互等）。
                        with socket.create_connection(("127.0.0.1", relay_port), timeout=5) as upstream:
                            upstream.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n")
                    status, body = 400, b'{"type":"error","error":{"type":"invalid_request_error","message":"Request is outside the Claude Code SupportEnvelope"}}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                return

        self.leaked = False
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RelayIngressNegativeTests(unittest.TestCase):
    def run_relay(self, *, leak: bool = False, accept: str | None = None) -> tuple[dict, Path, _Gateway]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        port = _free_port()
        gateway = _Gateway(port, leak=leak, accept=accept)
        self.addCleanup(gateway.close)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        payload = {
            "schema_version": relay.CANDIDATE_RUNTIME_SCHEMA, "run_id": "cid-tpir-1", "probe_id": relay.INGRESS_NEGATIVE_PROBE_ID,
            "base_url": gateway.base_url, "account_id": 7, "api_key_id": 9, "relay_port": port,
            "trigger": relay.CANDIDATE_TRIGGER_INGRESS_NEGATIVE,
        }
        for name, content in (
            ("gateway.json", json.dumps(payload)),
            ("api-key", "sk-candidate-key\n"),
            ("known-secrets.json", json.dumps({"gateway_account_access_token": "tok-access"})),
        ):
            (runtime / name).write_text(content, encoding="utf-8")
            (runtime / name).chmod(0o600)
        output = root / "out" / "relay"
        (root / "out").mkdir(mode=0o700)
        argv = [
            "ingress-negative", "--execute", "--run-id", "cid-tpir-1", "--probe-id", relay.INGRESS_NEGATIVE_PROBE_ID,
            "--output-root", str(output), "--expected-version", "2.1.285", "--model", "claude-sonnet-5",
            "--host-runtime-receipt", str(root / "receipt.json"), "--host-runtime-receipt-sha256", "a" * 64, "--run-nonce", "b" * 64,
            "--runtime-image", "registry.local/capture@sha256:" + "c" * 64, "--lock-file", str(root / "relay.lock"), "--timeout", "10",
            "--candidate-runtime-dir", str(runtime), "--candidate-relay-port", str(port),
        ]
        identity = {"execution_sources": {"sha256": "e" * 64}, "relay": {}, "scrubber": {}, "openssl": {}}
        receipt = {"container_runtime_binding": {"verified": True}, "sha256": "a" * 64, "run_nonce": "b" * 64}
        with mock.patch.object(relay, "_tool_identity", return_value=identity), mock.patch.object(relay, "_host_receipt", return_value=receipt), \
                mock.patch.object(relay, "INGRESS_NEGATIVE_GRACE_SECONDS", 0.2), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = relay.main(argv)
        manifest = json.loads((output / relay.INGRESS_NEGATIVE_MANIFEST).read_text(encoding="utf-8"))
        return {"code": code, **manifest}, output, gateway

    def test_all_rejected_and_zero_connections(self) -> None:
        manifest, output, gateway = self.run_relay()
        self.assertEqual((manifest["code"], manifest["status"]), (0, "complete"))
        self.assertEqual([case["case_id"] for case in manifest["cases"]], relay.ingress_negative_case_ids())
        self.assertTrue(all(case["rejected"] for case in manifest["cases"]))
        self.assertEqual(manifest["relay_connections"], [])
        self.assertEqual(manifest["verdict"]["live_request_count"], 0)
        statuses = {case["case_id"]: case["http_status"] for case in manifest["cases"]}
        self.assertEqual((statuses["messages-missing-api-key"], statuses["openai-responses"]), (401, 400))
        # 凭据形态：第三方与官方 UA、缺 key、伪造 OAuth 形态 key；清单不含任何凭据值。
        self.assertEqual(len(gateway.seen), len(relay.INGRESS_NEGATIVE_CASES))
        self.assertIn(("/v1/messages", "claude-cli/2.1.285 (external, cli)", "sk-candidate-key"), gateway.seen)
        raw = "".join(path.read_text(encoding="utf-8", errors="replace") for path in output.rglob("*") if path.is_file())
        self.assertNotIn("sk-candidate-key", raw)
        self.assertNotIn("tok-access", raw)
        self.assertTrue(manifest["secret_scan"]["passed"])

    def test_leak_and_acceptance_fail_closed(self) -> None:
        manifest, _output, _gateway = self.run_relay(leak=True)
        self.assertEqual((manifest["code"], manifest["status"]), (2, "failed"))
        self.assertEqual(len(manifest["relay_connections"]), 1)
        self.assertEqual(manifest["relay_connections"][0]["connect_authority"], "api.anthropic.com:443")
        self.assertIn("zero_relay_connections", manifest["m_binding"]["limitations"])
        manifest, output, _gateway = self.run_relay(accept="/v1/chat/completions")
        self.assertEqual((manifest["code"], manifest["status"]), (2, "failed"))
        self.assertEqual(manifest["verdict"]["failed_cases"], ["openai-chat-completions"])
        # 应答里回显的网关 API Key 已替换，不进证据。
        self.assertNotIn("sk-candidate-key", (output / relay.INGRESS_NEGATIVE_MANIFEST).read_text(encoding="utf-8"))

    def test_dry_run_plan_is_zero_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = relay.execute(
                relay._build_parser().parse_args(
                    [
                        "ingress-negative", "--dry-run", "--run-id", "x", "--probe-id", relay.INGRESS_NEGATIVE_PROBE_ID,
                        "--output-root", str(Path(directory) / "relay"), "--expected-version", "2.1.285", "--model", "claude-sonnet-5",
                        "--host-runtime-receipt", "/tmp/r.json", "--host-runtime-receipt-sha256", "a" * 64, "--run-nonce", "b" * 64,
                        "--runtime-image", "registry.local/capture@sha256:" + "c" * 64, "--candidate-runtime-dir", "/tmp/rt",
                        "--candidate-relay-port", "18443",
                    ]
                )
            )
            self.assertFalse((Path(directory) / "relay").exists())
        self.assertEqual((plan["live_requests"], plan["production_changes"], plan["relay_mode"]), (False, False, "connect-reject"))
        self.assertEqual([case["case_id"] for case in plan["cases"]], relay.ingress_negative_case_ids())
        with self.assertRaises(ConfigurationError):
            relay.execute(
                relay._build_parser().parse_args(
                    [
                        "ingress-negative", "--dry-run", "--run-id", "x", "--probe-id", "v4-replay-baseline", "--output-root", "/tmp/x/relay",
                        "--expected-version", "2.1.285", "--model", "m", "--host-runtime-receipt", "/tmp/r", "--host-runtime-receipt-sha256",
                        "a" * 64, "--run-nonce", "b" * 64, "--runtime-image", "r@sha256:" + "c" * 64, "--candidate-runtime-dir", "/tmp/rt",
                        "--candidate-relay-port", "18443",
                    ]
                )
            )


if __name__ == "__main__":
    unittest.main()
