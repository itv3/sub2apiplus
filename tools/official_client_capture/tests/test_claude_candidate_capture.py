"""Claude 候选取证（Claude 升级控制面设计 B4-01a）：中继候选模式与宿主侧候选采集脚本。

* 中继候选模式（``claude_fw_e_relay.py capture --candidate-runtime-dir … --candidate-relay-port …``）：只接受 FW-F v5
  probe，不读官方 OAuth 凭据；私有运行目录的权限、字段与本次 probe／run-id 逐项核对；connect 模式的中继完整性
  （隧道请求行、零连接、messages 期望与合成计划闭合只记观测、合成生产隔离失败关闭）；候选 TUI 全局状态去掉官方
  oauthAccount；账号刷新触发调用网关管理接口且结果文件不含任何凭据。
* 宿主侧 ``claude_run_candidate_capture.sh``：用仿真 docker（数据库查询、容器 hosts／CA、keeper、重启）端到端跑一遍——
  只读前检失败零修改；执行期账号代理已绑定、CA 已装、keeper 已停、私有运行目录 0700／0600；退出后账号 proxy／调度门、
  hosts、CA、keeper 精确恢复、秘密文件删除并写恢复报告；恢复失败固定退出 97；取证失败照常恢复并透传退出码。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_fw_e_relay as relay
from tools.official_client_capture import claude_upgrade_plan as plan

ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = ROOT / "tools" / "official_client_capture"
SCRIPT = TOOL_ROOT / "claude_run_candidate_capture.sh"
V5_CATALOG = TOOL_ROOT / "capturelib" / "claude_fw_f_v5.py"
V5_FACTS = TOOL_ROOT / "capturelib" / "claude_fw_f_v5_version_facts_2_1_285.json"


def _write(path: Path, data: bytes | str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    path.chmod(mode)


def _runtime_dir(root: Path, *, probe: str = "v4-replay-baseline", run_id: str = "cid-cand-v4-replay-baseline",
                 trigger: str = "client", admin: bool = False, **overrides) -> Path:
    directory = root / "runtime"
    directory.mkdir(mode=0o700)
    gateway = {
        "schema_version": relay.CANDIDATE_RUNTIME_SCHEMA,
        "run_id": run_id,
        "probe_id": probe,
        "base_url": "http://sub2apiplus:8080",
        "account_id": 7,
        "api_key_id": 9,
        "relay_port": 18443,
        "trigger": trigger,
        **overrides,
    }
    _write(directory / "gateway.json", json.dumps(gateway))
    _write(directory / "api-key", "sk-candidate-key\n")
    _write(directory / "known-secrets.json", json.dumps({"gateway_account_access_token": "tok-access"}))
    if admin:
        _write(directory / "admin-token", "admin.jwt.token\n")
    return directory


class CandidateRuntimeTests(unittest.TestCase):
    """私有运行目录：权限、字段与本次取证逐项核对；秘密值只在内存里。"""

    def load(self, directory: Path, **options) -> dict:
        arguments = {"probe_id": "v4-replay-baseline", "run_id": "cid-cand-v4-replay-baseline", "relay_port": 18443, "trigger": "client"}
        arguments.update(options)
        return relay._load_candidate_runtime(directory, **arguments)

    def test_valid_runtime_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = self.load(_runtime_dir(Path(directory)))
            self.assertEqual(loaded["base_url"], "http://sub2apiplus:8080")
            self.assertEqual(loaded["api_key"], "sk-candidate-key")
            self.assertEqual(loaded["known_secrets"], {"gateway_account_access_token": "tok-access"})
            self.assertIsNone(loaded["admin_token"])
            self.assertEqual(loaded["gateway"]["account_id"], 7)

    def test_rejections(self) -> None:
        cases = {
            "运行目录权限放宽": (lambda d: d.chmod(0o755), {}, "0700"),
            "API Key 文件权限放宽": (lambda d: (d / "api-key").chmod(0o644), {}, "权限不安全"),
            "probe 不符": (lambda d: None, {"probe_id": "v4-bash"}, "probe／run-id"),
            "端口不符": (lambda d: None, {"relay_port": 18444}, "中继端口或触发方式"),
            "触发方式不符": (lambda d: None, {"trigger": "gateway-account-refresh"}, "中继端口或触发方式"),
            "缺 API Key": (lambda d: (d / "api-key").unlink(), {}, "缺少网关 API Key"),
            "API Key 含空白": (lambda d: _write(d / "api-key", "sk a"), {}, "API Key 非法"),
            "待扫描令牌键名未登记": (lambda d: _write(d / "known-secrets.json", json.dumps({"x": "y"})), {}, "字段非法"),
            "非刷新触发带管理凭据": (lambda d: _write(d / "admin-token", "t"), {}, "只有账号刷新触发"),
        }
        for label, (change, options, message) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                runtime = _runtime_dir(Path(directory))
                change(runtime)
                with self.assertRaisesRegex(relay.ConfigurationError, message):
                    self.load(runtime, **options)
        bad_gateway = {
            "base_url 带路径": {"base_url": "http://sub2apiplus:8080/v1"},
            "base_url 是 https": {"base_url": "https://sub2apiplus:8080"},
            "base_url 缺端口": {"base_url": "http://sub2apiplus"},
            "base_url 带凭据": {"base_url": "http://user:pw@sub2apiplus:8080"},
            "账号 ID 不是整数": {"account_id": "7"},
        }
        for label, overrides in bad_gateway.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(relay.ConfigurationError):
                    self.load(_runtime_dir(Path(directory), **overrides))

    def test_refresh_trigger_requires_admin_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime_dir(Path(directory), probe="v4-oauth-refresh", run_id="r", trigger="gateway-account-refresh")
            with self.assertRaisesRegex(relay.ConfigurationError, "必须由宿主脚本提供管理凭据"):
                self.load(runtime, probe_id="v4-oauth-refresh", run_id="r", trigger="gateway-account-refresh")
            _write(runtime / "admin-token", "admin.jwt.token\n")
            loaded = self.load(runtime, probe_id="v4-oauth-refresh", run_id="r", trigger="gateway-account-refresh")
            self.assertEqual(loaded["admin_token"], "admin.jwt.token")

    def test_trigger_rule_agrees_with_plan(self) -> None:
        from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

        for probe_id in v5.PROBE_IDS:
            with self.subTest(probe=probe_id):
                self.assertEqual(relay.candidate_trigger_for_probe(v5.get_probe(probe_id).driver), plan.candidate_trigger(probe_id))


class CandidateArgumentTests(unittest.TestCase):
    """候选模式的命令行：dry-run 回显侧别与 connect 模式；不读官方凭据；只接受 v5 probe。"""

    def argv(self, probe_id: str, output: Path, *, live: bool = True, extra: tuple[str, ...] = ()) -> list[str]:
        argv = [
            "capture", "--dry-run", "--run-id", f"cid-{probe_id}", "--probe-id", probe_id, "--fw-f-v5-probe", probe_id,
            "--output-root", str(output), "--claude-bin", "/opt/claude", "--expected-version", "2.1.285",
            "--expected-sha256", "a" * 64, "--ca-signing-pem", "/run/ca.pem", "--ca-cert", "/run/ca.crt",
            "--host-runtime-receipt", "/run/receipt.json", "--host-runtime-receipt-sha256", "b" * 64, "--run-nonce", "c" * 64,
            "--fw-f-v5-catalog", str(V5_CATALOG), "--fw-f-v5-version-facts", str(V5_FACTS),
            "--fw-f-v5-version-facts-sha256", hashlib.sha256(V5_FACTS.read_bytes()).hexdigest(),
            "--candidate-runtime-dir", "/capture/runtime/claude-candidate/x", "--candidate-relay-port", "18443", *extra,
        ]
        if live:
            argv += ["--upstream-ip", "160.79.104.10"]
        return argv

    def run_capture(self, argv: list[str]) -> dict:
        return relay._capture(relay._build_parser().parse_args(argv))

    def test_dry_run_reports_candidate_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live = self.run_capture(self.argv("v4-replay-baseline", Path(directory) / "a"))
            self.assertEqual((live["side"], live["relay_mode"], live["relay_listen_port"]), ("candidate", "connect", 18443))
            self.assertEqual((live["candidate_trigger"], live["live_requests"]), ("client", True))
            refresh = self.run_capture(self.argv("v4-oauth-refresh", Path(directory) / "b", live=False))
            self.assertEqual((refresh["candidate_trigger"], refresh["upstream_host"]), ("gateway-account-refresh", "platform.claude.com"))
            self.assertFalse(refresh["production_forwarding_enabled"])

    def test_rejections(self) -> None:
        cases = {
            "读取官方凭据": (("--claude-credentials-file", "/root/.claude/.credentials.json"), "不得读取官方 OAuth 凭据"),
            "端口越界": (("--candidate-relay-port", "80"), "1024"),
            "改官方端口": (("--port", "8443"), "--candidate-relay-port"),
            "额外注入环境": (("--inject-env", "DISABLE_TELEMETRY=1"), "--inject-env"),
        }
        for label, (extra, message) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                argv = self.argv("v4-replay-baseline", Path(directory) / "a")
                if extra[0] in argv:
                    argv[argv.index(extra[0]) + 1] = extra[1]
                else:
                    argv += list(extra)
                with self.assertRaisesRegex(relay.ConfigurationError, message):
                    self.run_capture(argv)
        with tempfile.TemporaryDirectory() as directory:
            argv = self.argv("v4-replay-baseline", Path(directory) / "a")
            position = argv.index("--candidate-runtime-dir")
            del argv[position:position + 2]
            with self.assertRaisesRegex(relay.ConfigurationError, "同时给出"):
                self.run_capture(argv)
            argv = self.argv("v4-replay-baseline", Path(directory) / "b")
            argv[argv.index("--fw-f-v5-probe")] = "--fw-f-v4-probe"
            with self.assertRaisesRegex(relay.ConfigurationError, "只接受 --fw-f-v5-probe"):
                self.run_capture(argv)

    def test_official_mode_still_requires_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            argv = self.argv("v4-replay-baseline", Path(directory) / "a")
            for flag in ("--candidate-runtime-dir", "--candidate-relay-port"):
                position = argv.index(flag)
                del argv[position:position + 2]
            with self.assertRaisesRegex(relay.ConfigurationError, "必须提供 --claude-credentials-file"):
                self.run_capture(argv)

    def test_execute_never_reads_official_credentials(self) -> None:
        """候选 --execute 从不调用官方凭据读取：运行目录不合格时在任何进程启动前失败。"""

        with tempfile.TemporaryDirectory() as directory:
            argv = self.argv("v4-replay-baseline", Path(directory) / "a")
            argv[argv.index("--dry-run")] = "--execute"
            argv.append("--acknowledge-live-requests")
            with (
                mock.patch.object(relay, "load_claude_credentials", side_effect=AssertionError("不得读取凭据")) as credentials,
                mock.patch.object(relay.subprocess, "Popen", side_effect=AssertionError("不得启动进程")),
            ):
                with self.assertRaisesRegex(relay.ConfigurationError, "--candidate-runtime-dir 必须是已存在"):
                    self.run_capture(argv)
            credentials.assert_not_called()


def _connect_relay(root: Path, *, requests: list[bytes], synthetic: dict | None = None, connect_line: bool = True) -> None:
    connections = []
    for index, request in enumerate(requests, 1):
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        (root / f"conn{index:03d}.client_to_upstream.bin").write_bytes(request)
        (root / f"conn{index:03d}.upstream_to_client.bin").write_bytes(response)
        item = {
            "connection_id": index,
            "client_alpn": "http/1.1",
            "upstream_alpn": "http/1.1",
            "upstream_alpn_offer": ["http/1.1"],
            "valid": True,
            "bytes": {"client_to_upstream": len(request), "upstream_to_client": len(response)},
            "sha256": {
                "client_to_upstream": hashlib.sha256(request).hexdigest(),
                "upstream_to_client": hashlib.sha256(response).hexdigest(),
            },
        }
        if connect_line:
            item["connect_request_line"] = "CONNECT api.anthropic.com:443 HTTP/1.1"
        connections.append(item)
    manifest = {
        "schema_version": "byte-relay/v1",
        "mode": "connect",
        "upstream_host": "api.anthropic.com",
        "mirror_selected_alpn": True,
        "credential_scrubbing": {"byte_offsets_preserved": True},
        "connections": connections,
    }
    if synthetic:
        manifest.update(
            synthetic_profile="claude-fw-f-v4", claude_fault_plan=synthetic["plan"], production_forwarding_enabled=False
        )
        (root / "intervention.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "type": "synthetic_claude_response", "profile": "claude-fw-f-v4", "plan": synthetic["plan"],
                        "action": action, "connection_id": index, "message_ordinal": index,
                        "production_forwarded": synthetic.get("forwarded", False),
                    }
                ) + "\n"
                for index, action in enumerate(synthetic["actions"], 1)
            ),
            encoding="utf-8",
        )
    else:
        manifest["production_forwarding_enabled"] = True
    (root / "relay.json").write_text(json.dumps(manifest), encoding="utf-8")


MESSAGES = b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: api.anthropic.com\r\nContent-Length: 2\r\n\r\n{}"


class CandidateIntegrityTests(unittest.TestCase):
    """connect 模式的中继完整性：结构与生产隔离失败关闭，网关行为只记观测。"""

    def check(self, root: Path, **options) -> dict:
        return relay._validate_relay_integrity(root, relay_mode="connect", candidate=True, **options)

    def test_connect_connections_validate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _connect_relay(root, requests=[MESSAGES, MESSAGES])
            result = self.check(root)
            self.assertEqual((result["connection_count"], result["messages_request_count"]), (2, 2))
            self.assertTrue(result["message_request_expectation_met"])
            self.assertEqual(len(result["connect_request_lines"]), 2)

    def test_missing_connect_line_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _connect_relay(root, requests=[MESSAGES], connect_line=False)
            with self.assertRaisesRegex(relay.RelayEvidenceError, "CONNECT"):
                self.check(root)

    def test_zero_connections_and_missing_messages_are_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _connect_relay(root, requests=[])
            result = self.check(root, message_request_expectation="zero")
            self.assertEqual((result["connection_count"], result["message_request_expectation_met"]), (0, True))
            missing = self.check(root)
            self.assertFalse(missing["message_request_expectation_met"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _connect_relay(root, requests=[])
            # 官方侧照旧失败关闭：connect 模式只属于候选。
            with self.assertRaisesRegex(relay.RelayEvidenceError, "模式与取证侧别"):
                relay._validate_relay_integrity(root, relay_mode="connect")

    def test_synthetic_isolation_is_fatal_but_closure_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _connect_relay(root, requests=[MESSAGES, MESSAGES, MESSAGES],
                           synthetic={"plan": "retry-529", "actions": ["retry-529_fault", "retry-529_fault", "retry-529_success"]})
            result = self.check(root, synthetic_plan="retry-529", synthetic_profile="claude-fw-f-v4")
            # 网关多重试了一次：官方口径的「恰好两次尝试」不闭合，候选侧只记观测。
            self.assertFalse(result["synthetic_plan_closed"])
            self.assertIn("两次尝试", result["synthetic_plan"]["closure_error"])
            self.assertFalse(result["synthetic_isolation"]["production_forwarding_enabled"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _connect_relay(root, requests=[MESSAGES],
                           synthetic={"plan": "retry-529", "actions": ["retry-529_fault"], "forwarded": True})
            with self.assertRaisesRegex(relay.RelayEvidenceError, "production_forwarded"):
                self.check(root, synthetic_plan="retry-529", synthetic_profile="claude-fw-f-v4")


class CandidateClientStateTests(unittest.TestCase):
    def test_global_state_drops_oauth_account(self) -> None:
        state = {"oauthAccount": {"accountUuid": "u"}, "theme": "light", "projects": {"/w": {}}}
        encoded, normalized = relay._candidate_global_state(json.dumps(state).encode(), "2.1.285")
        copied = json.loads(encoded)
        self.assertNotIn("oauthAccount", copied)
        self.assertEqual((copied["theme"], copied["hasCompletedOnboarding"], copied["lastOnboardingVersion"]), ("dark", True, "2.1.285"))
        self.assertEqual(copied["projects"], {"/w": {}})
        self.assertEqual(normalized, ["oauthAccount", "theme", "hasCompletedOnboarding", "lastOnboardingVersion"])

    def test_tui_home_copies_no_credentials_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = root / "shm"
            memory.mkdir()
            global_state = root / ".claude.json"
            _write(global_state, json.dumps({"oauthAccount": {"x": 1}}))
            with relay._candidate_tui_home(global_state, expected_version="2.1.285", trust_paths=["/work"], memory_root=memory) as (
                home, receipt,
            ):
                self.assertFalse((home / ".claude" / ".credentials.json").exists())
                self.assertNotIn("oauthAccount", json.loads((home / ".claude.json").read_text()))
                self.assertTrue(receipt["oauth_account_removed"])
            self.assertTrue(receipt["removed"])
            self.assertFalse(home.exists())
            self.assertEqual((receipt["credentials_copied"], receipt["global_state_copied"]), (False, True))


class GatewayTriggerTests(unittest.TestCase):
    """账号刷新触发：请求打到网关管理接口，结果只记状态与摘要，应答里的秘密被替换。"""

    def test_refresh_trigger_records_status_without_secrets(self) -> None:
        seen: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                seen.append((self.path, self.headers.get("Authorization", "")))
                body = b'{"error":"refresh rejected","echo":"admin.jwt.token"}'
                self.send_response(400)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        runtime = {
            "gateway": {"account_id": 7},
            "base_url": f"http://127.0.0.1:{server.server_address[1]}",
            "api_key": "sk-candidate-key",
            "admin_token": "admin.jwt.token",
            "known_secrets": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            result = relay._trigger_gateway_account_refresh(runtime, Path(directory), 30)
            written = (Path(directory) / "gateway-trigger.json").read_text(encoding="utf-8")
        self.assertEqual(seen, [("/api/v1/admin/accounts/7/refresh", "Bearer admin.jwt.token")])
        self.assertEqual((result["http_status"], result["valid"]), (400, True))
        self.assertNotIn("admin.jwt.token", written)
        self.assertNotIn("sk-candidate-key", written)


# ---------------------------------------------------------------------------
# 宿主侧候选采集脚本：仿真 docker 端到端
# ---------------------------------------------------------------------------

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""仿真 docker：只实现候选采集脚本用到的子命令；状态存于 FAKE_DOCKER_STATE。"""
import hashlib, json, os, re, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_DOCKER_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(args)


def save():
    state_path.write_text(json.dumps(state))


def done(output="", code=0):
    save()
    if output:
        sys.stdout.write(output)
    sys.exit(code)


def sql(query):
    q = " ".join(query.split())
    if "select platform ||" in q:
        return state["account_shape"]
    if "length(coalesce(credentials->>'access_token',''))" in q:
        return "true"
    if "select status || '|' || coalesce(group_id::text" in q:
        return "active|5|live"
    if "from account_groups ag" in q:
        return state["eligible"]
    if "select coalesce(proxy_id::text,'NULL')" in q:
        return f"{state['proxy'] or 'NULL'}|{state['fallback'] or 'NULL'}"
    if "encode(convert_to(coalesce(temp_unschedulable_until" in q:
        return state["gate"]
    if q.startswith("update accounts set temp_unschedulable_until"):
        state["gate"] = "|"
        return ""
    if q.startswith("insert into proxies"):
        state["proxies"][str(state["next_proxy"])] = q
        return str(state["next_proxy"])
    match = re.match(r"update accounts set proxy_id = (\w+), proxy_fallback_origin_id = (\w+)", q)
    if match:
        value = None if match.group(1) == "NULL" else match.group(1)
        if value != str(state["next_proxy"]) and state.get("fail_proxy_restore"):
            return ""  # 仿真恢复写入失败：账号仍挂着临时代理
        state["proxy"], state["fallback"] = value, (None if match.group(2) == "null" or match.group(2) == "NULL" else match.group(2))
        return ""
    if q.startswith("delete from proxies"):
        state["proxies"] = {}
        return ""
    if "select key from api_keys" in q:
        return "sk-candidate-key"
    if "coalesce(credentials->>'access_token','') from accounts" in q:
        return "tok-access"
    if "coalesce(credentials->>'refresh_token','') from accounts" in q:
        return "tok-refresh"
    raise SystemExit(f"未仿真的查询：{q}")


command = args[0]
if command == "inspect":
    fmt, target = args[2], args[3]
    if "Config.Env" in fmt:
        done("POSTGRES_USER=sub2api\nPOSTGRES_DB=sub2api\n" if target == "sub2apiplus-postgres" else "SERVER_PORT=8080\n")
    if fmt == "{{.Id}}":
        done(state["service_id"] + "\n")
    if fmt == "{{.State.Running}}":
        done(("true" if state["keeper_running"] else "false") + "\n")
    if "State.Health" in fmt:
        done("healthy\n")
    raise SystemExit(f"未仿真的 inspect：{args}")
if command == "exec":
    container, rest = args[1], args[2:]
    if rest[0] == "psql":
        done(sql(rest[-1]) + "\n")
    if rest[:2] == ["getent", "hosts"]:
        done("172.30.0.10 x\n")
    if rest[0] == "test":
        present = state["custom_ca_present"]
        done(code=0 if (present if rest[1] == "-e" else not present) else 1)
    if rest[0] == "update-ca-certificates":
        done()
    if rest[:2] == ["rm", "-f"]:
        state["custom_ca_present"] = False
        done()
    if rest[0] == "sh" and "claude-candidate-hosts.restore" in rest[2]:
        state["hosts"] = state["staged_hosts"]
        done()
    if rest[0] == "sha256sum":
        content = state["hosts"] if rest[1] == "/etc/hosts" else state["ca_bundle"]
        done(hashlib.sha256(content.encode()).hexdigest() + "  " + rest[1] + "\n")
    if rest[0] == "python3" and rest[1].endswith("/claude_fw_e_relay.py"):
        runtime = Path(os.environ["FAKE_RUNTIME_HOST"])
        files = sorted(item.name for item in runtime.iterdir() if item.is_file())
        state["capture_observed"] = {
            "proxy": state["proxy"], "custom_ca_present": state["custom_ca_present"], "keeper_running": state["keeper_running"],
            "gate": state["gate"], "files": files, "dir_mode": oct(runtime.stat().st_mode & 0o777),
            "file_modes": sorted({oct(item.stat().st_mode & 0o777) for item in runtime.iterdir() if item.is_file()}),
            "gateway": json.loads((runtime / "gateway.json").read_text()),
            "api_key": (runtime / "api-key").read_text().strip(),
            "known": json.loads((runtime / "known-secrets.json").read_text()),
            "admin": (runtime / "admin-token").read_text().strip() if (runtime / "admin-token").exists() else None,
        }
        done(code=int(os.environ.get("FAKE_CAPTURE_RC", "0")))
    raise SystemExit(f"未仿真的 exec：{args}")
if command == "cp":
    source, target = args[1], args[2]
    if source.startswith("sub2apiplus:"):
        content = state["hosts"] if source.endswith("/etc/hosts") else state["ca_bundle"]
        Path(target).write_text(content)
        done()
    if source.startswith("capture-cli:"):
        Path(target).write_text("CAPTURE-CA\n")
        done()
    if target.startswith("sub2apiplus:"):
        if target.endswith(".restore") and "hosts" in target:
            state["staged_hosts"] = Path(source).read_text()
        elif target.endswith(".crt"):
            state["custom_ca_present"] = True
        done()
    raise SystemExit(f"未仿真的 cp：{args}")
if command == "restart":
    state["hosts"] = "127.0.0.1 localhost\n172.30.0.20 regenerated\n"
    done()
if command == "stop":
    state["keeper_running"] = False
    done()
if command == "start":
    state["keeper_running"] = True
    done()
raise SystemExit(f"未仿真的 docker 子命令：{args}")
'''

FAKE_SUPERVISOR = r'''#!/usr/bin/env python3
"""仿真共享监督器的 egress-transition：只执行 -- 之后的命令。"""
import subprocess, sys

args = sys.argv[1:]
assert args[0] == "egress-transition"
sys.exit(subprocess.run(args[args.index("--") + 1:]).returncode)
'''

FAKE_SHA256SUM = r'''#!/usr/bin/env python3
import hashlib, sys
for name in sys.argv[1:]:
    print(hashlib.sha256(open(name, "rb").read()).hexdigest() + "  " + name)
'''


class CandidateScriptTests(unittest.TestCase):
    """宿主侧候选采集脚本端到端（仿真 docker）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        self.campaign = self.data / "evidence" / "campaigns" / "cid"
        self.campaign.mkdir(parents=True)
        tools = self.root / "tools"
        tools.mkdir()
        shutil.copy2(SCRIPT, tools / SCRIPT.name)
        self.script = tools / SCRIPT.name
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("sha256sum", FAKE_SHA256SUM), ("flock", "#!/bin/sh\nexit 0\n")):
            _write(bin_dir / name, body, 0o755)
        _write(tools / "codex_upgrade_supervisor.py", FAKE_SUPERVISOR, 0o755)
        self.state_path = self.root / "docker-state.json"
        self.original_hosts = "127.0.0.1 localhost\n172.30.0.20 abc123\n"
        self.state = {
            "calls": [], "account_shape": "anthropic|oauth|NULL|live", "eligible": "7", "proxy": "3", "fallback": None,
            "gate": "|", "proxies": {}, "next_proxy": 77, "service_id": "svc-1", "keeper_running": True,
            "custom_ca_present": False, "hosts": self.original_hosts, "staged_hosts": "", "ca_bundle": "BUNDLE\n",
        }
        self.probe = "v4-replay-baseline"
        self.runtime_container = f"/capture/runtime/claude-candidate/cid-{self.probe}"
        self.runtime_host = self.data / "runtime" / "claude-candidate" / f"cid-{self.probe}"
        self.output_container = f"/capture/runs/claude-candidate/cid/candidate-transport-baseline/{self.probe}"
        self.environment = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_DOCKER_STATE": str(self.state_path),
            "FAKE_RUNTIME_HOST": str(self.runtime_host),
            "CLAUDE_CANDIDATE_CAPTURE": plan.CANDIDATE_CAPTURE_GATE,
            "CAMPAIGN_DIR": str(self.campaign),
            "CAPTURE_ROOT": "/capture",
            "CAPTURE_CONTAINER": "capture-cli",
            "SERVICE_CONTAINER": "sub2apiplus",
            "KEEPER_CONTAINER": "sub2apiplus-keeper",
            "POSTGRES_CONTAINER": "sub2apiplus-postgres",
            "ACCOUNT_ID": "7",
            "API_KEY_ID": "9",
            "RUN_ID": f"cid-{self.probe}",
            "PROBE_ID": self.probe,
            "OUTPUT_ROOT": self.output_container,
            "CANDIDATE_RUNTIME_DIR": self.runtime_container,
            "RELAY_PORT": "18443",
            "GATEWAY_TRIGGER": "client",
            "CA_CERT": "/opt/mitm/mitmproxy-ca-cert.pem",
        }

    def command(self) -> list[str]:
        return [
            "docker", "exec", "capture-cli", "python3", "/root/oauth-capture/tools/official_client_capture/claude_fw_e_relay.py",
            "capture", "--execute", "--acknowledge-live-requests", "--run-id", f"cid-{self.probe}", "--probe-id", self.probe,
            "--fw-f-v5-probe", self.probe, "--output-root", self.output_container, "--ca-cert", "/opt/mitm/mitmproxy-ca-cert.pem",
            "--candidate-runtime-dir", self.runtime_container, "--candidate-relay-port", "18443",
        ]

    def run_script(self, *, environment: dict | None = None, command: list[str] | None = None, **state) -> subprocess.CompletedProcess:
        self.state_path.write_text(json.dumps({**self.state, **state}))
        return subprocess.run(
            ["bash", str(self.script), *(command or self.command())],
            env={**self.environment, **(environment or {})}, capture_output=True, text=True, timeout=120,
        )

    def final_state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def report(self) -> dict:
        path = self.data / "runs" / "claude-candidate" / "cid" / "candidate-transport-baseline" / "candidate-gateway" / f"{self.probe}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_restored(self, state: dict) -> None:
        self.assertEqual((state["proxy"], state["fallback"], state["proxies"]), ("3", None, {}))
        self.assertEqual((state["gate"], state["keeper_running"], state["custom_ca_present"]), ("|", True, False))
        self.assertEqual(state["hosts"], self.original_hosts)

    def test_complete_run_isolates_and_restores_exactly(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.final_state()
        observed = state["capture_observed"]
        self.assertEqual((observed["proxy"], observed["custom_ca_present"], observed["keeper_running"]), ("77", True, False))
        self.assertEqual(observed["files"], ["api-key", "gateway.json", "known-secrets.json"])
        self.assertEqual((observed["dir_mode"], observed["file_modes"]), ("0o700", ["0o600"]))
        self.assertEqual(observed["gateway"]["base_url"], "http://sub2apiplus:8080")
        self.assertEqual(
            {key: observed["gateway"][key] for key in ("account_id", "api_key_id", "relay_port", "trigger", "probe_id")},
            {"account_id": 7, "api_key_id": 9, "relay_port": 18443, "trigger": "client", "probe_id": self.probe},
        )
        self.assertEqual(set(observed["gateway"]), relay.CANDIDATE_RUNTIME_FIELDS)
        self.assertEqual(observed["api_key"], "sk-candidate-key")
        self.assertEqual(observed["known"], {"gateway_account_access_token": "tok-access", "gateway_account_refresh_token": "tok-refresh"})
        self.assertIsNone(observed["admin"])
        self.assert_restored(state)
        self.assertFalse(self.runtime_host.exists())
        report = self.report()
        self.assertEqual((report["status"], report["capture_exit_code"]), ("complete", 0))
        self.assertTrue(all(report[key] for key in ("account_proxy_restored_equal", "account_gate_restored_equal",
                                                    "hosts_sha256_equal", "ca_bundle_sha256_equal", "runtime_secrets_removed")))
        self.assertNotIn("sk-candidate-key", result.stdout + result.stderr)
        # 服务容器重启全部经监督器出口维护声明：开 CA 一次、撤 CA 一次。
        self.assertEqual([call for call in state["calls"] if call[0] == "restart"], [["restart", "sub2apiplus"]] * 2)

    def test_capture_failure_restores_and_passes_exit_code(self) -> None:
        result = self.run_script(environment={"FAKE_CAPTURE_RC": "3"})
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assert_restored(self.final_state())
        self.assertEqual((self.report()["status"], self.report()["capture_exit_code"]), ("capture_failed", 3))
        self.assertFalse(self.runtime_host.exists())

    def test_restoration_failure_exits_97_and_removes_secrets(self) -> None:
        result = self.run_script(fail_proxy_restore=True)
        self.assertEqual(result.returncode, 97, result.stderr)
        self.assertEqual(self.report()["status"], "restoration_failed")
        self.assertFalse(self.report()["account_proxy_restored_equal"])
        remaining = sorted(path.name for path in self.runtime_host.rglob("*") if path.is_file())
        self.assertEqual(remaining, ["ca-certificates.before", "capture-ca.crt", "hosts.before"])

    def test_read_only_prechecks_make_no_changes(self) -> None:
        for label, state, message in (
            ("不是单账号隔离分组", {"eligible": "7\n8"}, "单账号隔离分组"),
            ("账号不是 Anthropic OAuth", {"account_shape": "openai|oauth|NULL|live"}, "Anthropic OAuth"),
        ):
            with self.subTest(label=label):
                result = self.run_script(**state)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                final = self.final_state()
                self.assertEqual((final["proxy"], final["proxies"], final["keeper_running"]), ("3", {}, True))
                self.assertFalse(any(call[0] in {"stop", "restart"} for call in final["calls"]))
                self.assertFalse(self.runtime_host.exists())

    def test_argument_rejections(self) -> None:
        mismatched = self.command()
        mismatched[mismatched.index("--run-id") + 1] = "cid-other"
        cases = {
            "缺开关": ({"CLAUDE_CANDIDATE_CAPTURE": "yes"}, None, "CLAUDE_CANDIDATE_CAPTURE"),
            "账号 ID 为空": ({"ACCOUNT_ID": ""}, None, "ACCOUNT_ID"),
            "中继命令与环境不一致": ({}, mismatched, "--run-id 与环境变量不一致"),
            "带官方凭据": ({}, [*self.command(), "--claude-credentials-file", "/x"], "--claude-credentials-file"),
            "不经抓包容器": ({}, ["docker", "exec", "other", *self.command()[3:]], "docker exec"),
            "Campaign 目录不规范": ({"CAMPAIGN_DIR": str(self.root / "x" / "cid")}, None, "evidence/campaigns"),
            "刷新触发缺管理凭据": ({"GATEWAY_TRIGGER": "gateway-account-refresh"}, None, "管理凭据"),
        }
        for label, (environment, command, message) in cases.items():
            with self.subTest(label=label):
                result = self.run_script(environment=environment, command=command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.final_state()["calls"], [])

    def test_refresh_trigger_hands_admin_token_to_runtime_dir(self) -> None:
        token_file = self.root / "admin-token"
        payload = json.dumps({"exp": 4_000_000_000}).encode()
        import base64

        segment = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        _write(token_file, f"hdr.{segment}.sig")
        environment = {"GATEWAY_TRIGGER": "gateway-account-refresh", "ADMIN_BEARER_TOKEN_FILE": str(token_file)}
        result = self.run_script(environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = self.final_state()["capture_observed"]
        self.assertEqual(observed["admin"], f"hdr.{segment}.sig")
        self.assertEqual(observed["gateway"]["trigger"], "gateway-account-refresh")
        self.assertFalse(self.runtime_host.exists())


class CandidateScriptStaticTests(unittest.TestCase):
    """脚本源码约束：只读前检先于修改，恢复失败退出 97，秘密不进命令行参数。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_executable_and_gated(self) -> None:
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)
        self.assertIn("required_gate=YES_I_ACCEPT_CANDIDATE_GATEWAY_CAPTURE", self.source)
        self.assertEqual(plan.CANDIDATE_CAPTURE_GATE, "YES_I_ACCEPT_CANDIDATE_GATEWAY_CAPTURE")

    def test_mutations_follow_restore_trap(self) -> None:
        trap = self.source.index("trap restore_environment EXIT ERR INT TERM")
        for mutation in ('docker stop "$keeper_container"', "insert into proxies", 'update-ca-certificates >/dev/null', "restart_service\n"):
            self.assertLess(trap, self.source.index(mutation), mutation)
        for precheck in ("单账号隔离分组", "original_proxy_state=$(proxy_state)", "original_gate_state=$(account_gate_state)"):
            self.assertLess(self.source.index(precheck), trap, precheck)
        self.assertIn("exit 97", self.source)

    def test_secrets_stay_out_of_arguments(self) -> None:
        self.assertIn('CANDIDATE_API_KEY="$api_key"', self.source)
        self.assertNotIn('"$api_key" "$', self.source)
        self.assertIn("remove_runtime_secrets", self.source)


if __name__ == "__main__":
    unittest.main()
