"""Claude 客户端启动探测（Claude 升级控制面设计 B2-10）：``client_launch_probe.py --client claude`` 与
``claude_launch_probe_runner.py``、受管驱动入口 ``claude_drive_tui_cli.py`` 的离线测试。

覆盖：
* 运行器纯函数：私有 HOME（合成凭据、隐私设置、全局状态规范化副本；只读全局状态文件，不碰凭据文件；文件不存在时也能
  建）、与中继同法预置工作目录信任、信任撤销验收变更、客户端参数与中继 ``_run_tui_probe`` 逐项相同、驱动入口命令行、
  替身应答与请求记录（只记主机、方法、路径与头部名称）、交互屏识别、运行判定、参数闭集；
* 宿主侧：B2-05 真实 2.1.285 清单的 TUI 组合展开与去重、运行器参数与运行器校验互认；以 docker 替身端到端跑 run／verify：
  全部通过、失败组合以 48×160 窗口复跑诊断、注入未确认信任目录被识别为交互屏且报告不能放行、``/root/.claude.json``
  指纹漂移判失败、受管驱动与数据根不一致判失败、未知验收参数拒绝；verify 认客户端、拒绝验收参数报告并按适配器绑定
  预检 Campaign；
* Claude 侧驱动（``capturelib/claude_fw_f_v3``，驱动入口与中继 v3／v4／v5 的 TUI 场景共用；2026-10-07 ARM64 实测 2.1.285
  的两处回归）：信任框按选项文字定位确认项的纯函数与状态机；主输入框判定认 2.1.285 auto 权限模式的页脚、忙碌页脚自带的
  「← for agents」不算空闲、2.1.226 页脚与共用状态机同判；用伪终端里的替身客户端端到端驱动（2.1.285 默认聚焦「No, exit」
  时选到 Yes 并在 auto 模式主界面提交；已信任时直接提交；多轮输入等页脚回到空闲才发下一轮；2.1.226 只发一个回车；共用状态机
  在 2.1.285 上选到「No, exit」、认不出主输入框作反向对照）；中继路径绑定同一份驱动，并端到端走一遍 v4 的 TUI 场景执行函数；
* 入口步骤与编排：Claude 的启动探测登记 Claude 运行器，Job 演练与启动探测的命令行追加 ``--client claude``。
"""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from tools.ci import client_table as CT
from tools.ci import entry_orchestrator as eo
from tools.ci import entry_steps as es
from tools.official_client_capture import claude_drive_tui_cli as drive_cli
from tools.official_client_capture import claude_fw_e_relay as relay
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import drive_claude_tui
from tools.official_client_capture.capturelib import claude_fw_f_v3 as v3
from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4
from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5
from tools.official_client_capture.capturelib import environment as capture_environment

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
TARGET = "2.1.285"
TOOL_ROOT = "/root/oauth-capture/tools/official_client_capture"
TOKEN = "ZQXPROBEABCDEFGHIJKLMNOPQRST"


def _load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(f"claude_launch_probe_test_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


probe = _load("client_launch_probe")
runner = _load("claude_launch_probe_runner")


def _context() -> dict[str, str]:
    return {
        "campaign_id": "claude-code-2_1_285-lp1", "campaign_dir": "/root/docker/capture-cli/data/evidence/campaigns/c",
        "target_version": TARGET, "target_sha256": "a" * 64,
        "runtime_image": "oauth-egress-capture-capture-cli@sha256:" + "b" * 64, "capture_root": "/capture",
        "capture_tool_root": TOOL_ROOT, "capture_container": "capture-cli", "synthetic_capture_container": "capture-cli-synthetic",
        "claude_bin": f"/opt/claude-{TARGET}/claude", "model": "claude-sonnet-5", "upstream_ip": "160.79.104.10",
        "credentials_file": "/root/.claude/.credentials.json", "ca_signing_pem": "/opt/mitm/ca.pem", "ca_cert": "/opt/mitm/ca-cert.pem",
    }


def _jobs() -> list:
    payload = json.loads(plan.scenario_manifest_path(TARGET).read_text(encoding="utf-8"))
    return plan.expand_scenario_jobs(payload, _context(), job_factory=claude_upgrade.Job, suite="full")


def _tui_probe_ids() -> list[str]:
    return [probe_id for probe_id in v5.PROBE_IDS if v5.get_probe(probe_id).driver == "tui"]


def _config(**overrides):
    combo = next(item for item in probe.claude_tui_combos(_jobs()) if "v4-tui-attachment" in item["probe_ids"])
    config = probe.claude_runner_config(combo, token=TOKEN, test_mutations=[], diagnostic_window=None)
    config.update(overrides)
    return config


# 2026-10-07 ARM64 实测（claude-code-2_1_285-b210-20261007t101832z-client-launch-probe 报告 tui_visible_tail，空格已被
# 光标定位吃掉）：2.1.285 的信任框把取消项排在前面并默认聚焦。
TRUST_SCREEN_285 = (
    "\n\n" + "─" * 80 + "\n\nAccessingworkspace:\n\n\n\n/work/probe-run/results/workspace\n\n\n\n"
    "Quicksafetycheck:Isthisaprojectyoucreatedoroneyoutrust?(Likeyour\n\nowncode,awell-knownopensourceproject,"
    "orworkfromyourteam).Ifnot,\n\ntakeamomenttoreviewwhat'sinthisfolderfirst.\n\n\n\n"
    "ClaudeCode'llbeabletoread,edit,andexecutefileshere.\n\n\n\nSecurityguide\n\n\n\n"
    "❯No,exit\n\nYes,Itrustthisfolder\n\n\n\nEntertoconfirm·Esctocancel\n\n"
)
# 2.1.226 真实采集（local-analysis/fw-f/claude-code-2.1.226/complete-v16-*/attempts/v4-tui-usage 的 tui-transcript.log）：
# 确认项在前、默认聚焦、带编号。
TRUST_SCREEN_226 = (
    "\n\nAccessingworkspace:\n\n\n\n/campaign/attempts/v4-tui-usage/attempt-001/results/workspace\n\n\n\n"
    "Quicksafetycheck:Isthisaprojectyoucreatedoroneyoutrust?\n\nSecurityguide\n\n\n\n"
    "❯1.Yes,Itrustthisfolder\n\n2.No,exit\n\n\n\nEntertoconfirm·Esctocancel\n\n"
)


def _client_trusts(state: dict, cwd: str) -> bool:
    """2.1.285 判定工作目录已信任的参照实现（冻结二进制 chunk-n1qqpa0t.js 的 ``sU``／``QS``／``ZS``，工作目录不在 git 仓库
    里时）：从启动目录起逐级向上，任一级 ``projects[<路径>].hasTrustDialogAccepted`` 为真即已信任，否则弹信任框。"""

    projects = state.get("projects") or {}
    path = PurePosixPath(cwd)
    return any((projects.get(str(candidate)) or {}).get("hasTrustDialogAccepted") for candidate in (path, *path.parents))


class RunnerPrivateHomeTests(unittest.TestCase):
    """私有 HOME：中继 TUI 临时 HOME 的同一布局，凭据合成、全局状态只读一份并规范化。"""

    def test_global_state_normalization_matches_the_relay(self) -> None:
        state = {"oauthAccount": {"accountUuid": "u-1", "emailAddress": "a@b.example"}, "theme": "light", "projects": {"/x": {}}}
        raw = json.dumps(state).encode("utf-8")
        payload, summary = runner.normalized_global_state(raw, TARGET)
        relay_payload, relay_fields = capture_environment._normalized_tui_global_state(raw, TARGET)
        # 中继在 TUI 临时 HOME 里对规范化副本再预置工作目录信任（claude_fw_e_relay._seed_tui_workspace_trust）。
        relay_state = json.loads(relay_payload)
        relay._seed_workspace_trust(relay_state, [str(runner.workspace_path())])
        relay_seeded = (json.dumps(relay_state, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.assertEqual(payload, relay_seeded, "有账号且不带验收变更时与中继的规范化加信任预置逐字相同")
        self.assertEqual(summary, {"source": "present", "normalized_fields": relay_fields, "oauth_account": "global_state",
                                   "trust_seeded_projects": [str(runner.workspace_path())], "untrusted_projects": []})
        absent, absent_summary = runner.normalized_global_state(None, TARGET)
        document = json.loads(absent)
        self.assertEqual((absent_summary["source"], absent_summary["oauth_account"]), ("absent", "synthetic"))
        self.assertEqual(document["oauthAccount"], runner.SYNTHETIC_OAUTH_ACCOUNT)
        self.assertEqual((document["theme"], document["hasCompletedOnboarding"], document["lastOnboardingVersion"]), ("dark", True, TARGET))
        for bad in (b"not json", b"[]"):
            with self.subTest(bad=bad), self.assertRaises(runner.ProbeError):
                runner.normalized_global_state(bad, TARGET)

    def test_untrust_mutation_revokes_the_workspace_and_its_parents(self) -> None:
        workspace = runner.workspace_path()
        self.assertEqual(str(workspace), "/work/probe-run/results/workspace")
        state = {"projects": {"/work": {"hasTrustDialogAccepted": True}, "/": {"hasTrustDialogAccepted": True},
                              "/srv": {"hasTrustDialogAccepted": True}}}
        payload, summary = runner.normalized_global_state(json.dumps(state).encode(), TARGET, mutations=["untrust:/work"])
        projects = json.loads(payload)["projects"]
        self.assertEqual(summary["trust_seeded_projects"], [str(workspace)])
        self.assertEqual(sorted(summary["untrusted_projects"]), ["/", "/work", str(workspace)],
                         "预置的工作目录信任也在链上，一并撤销")
        self.assertEqual({key: value["hasTrustDialogAccepted"] for key, value in projects.items()},
                         {"/work": False, "/": False, "/srv": True, str(workspace): False})
        for bad in ("untrust:/srv", "untrust:/work/probe-run/results/workspace/sub", "unack_migration:x", "untrust:relative"):
            with self.subTest(bad=bad), self.assertRaises(runner.ProbeError):
                runner.normalized_global_state(json.dumps(state).encode(), TARGET, mutations=[bad])

    def test_private_home_never_reads_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_state = root / "root" / ".claude"
            real_state.mkdir(parents=True)
            credentials = real_state / ".credentials.json"
            credentials.write_text('{"claudeAiOauth": {"accessToken": "REAL-SECRET"}}', encoding="utf-8")
            global_state = root / "root" / ".claude.json"
            global_state.write_text(json.dumps({"oauthAccount": {"accountUuid": "u"}}), encoding="utf-8")
            reads: list[Path] = []
            original = Path.read_bytes

            def recording(path: Path) -> bytes:
                reads.append(path)
                return original(path)

            for label, source in (("全局状态存在", global_state), ("全局状态不存在", root / "absent.json")):
                with self.subTest(label=label), mock.patch.object(Path, "read_bytes", recording):
                    reads.clear()
                    home = root / f"home-{len(list(root.iterdir()))}"
                    summary = runner.build_private_home(
                        home, global_state_file=source, target_version=TARGET, privacy_env=capture_environment.PRIVACY_ENV,
                        mutations=[], workspace=runner.workspace_path(),
                    )
                    self.assertEqual(reads, [source] if source.exists() else [], "只读全局状态文件，不读凭据")
                    self.assertEqual(json.loads((home / ".claude" / ".credentials.json").read_text(encoding="utf-8")), runner.SYNTHETIC_CREDENTIALS)
                    self.assertEqual(
                        (home / ".claude" / "settings.json").read_text(encoding="utf-8"),
                        json.dumps({"skipWebFetchPreflight": True, "env": capture_environment.PRIVACY_ENV}, ensure_ascii=False, indent=2) + "\n",
                        "隐私设置与中继 TUI 临时 HOME 写的那份逐字相同",
                    )
                    self.assertEqual(oct((home / ".claude" / ".credentials.json").stat().st_mode & 0o777), "0o600")
                    self.assertEqual(summary["credentials"], "synthetic")
                    self.assertNotIn("REAL-SECRET", json.dumps(summary))
            self.assertEqual(credentials.read_text(encoding="utf-8"), '{"claudeAiOauth": {"accessToken": "REAL-SECRET"}}')


class RunnerTrustTests(unittest.TestCase):
    """2026-10-07 ARM64 实测：私有 HOME 不预置工作目录信任时 2.1.285 弹信任框、共用驱动回车选到「No, exit」。"""

    def test_private_home_trusts_the_workspace_so_the_normal_path_has_no_dialog(self) -> None:
        workspace = str(runner.workspace_path())
        for label, raw in (("全局状态不存在（ARM64 现状）", None),
                           ("全局状态里只有别的目录", json.dumps({"oauthAccount": {"accountUuid": "u"}, "projects": {"/srv": {}}}).encode()),
                           ("全局状态里工作目录被显式拒绝", json.dumps({"projects": {workspace: {"hasTrustDialogAccepted": False, "allowedTools": []}}}).encode())):
            with self.subTest(label=label):
                payload, summary = runner.normalized_global_state(raw, TARGET)
                state = json.loads(payload)
                self.assertTrue(_client_trusts(state, workspace), "正常路径：客户端认为工作目录已信任，不弹信任框")
                self.assertEqual(summary["trust_seeded_projects"], [workspace])
                self.assertEqual(summary["untrusted_projects"], [])
                self.assertIs(state["projects"][workspace]["hasTrustDialogAccepted"], True)
        kept = json.loads(runner.normalized_global_state(
            json.dumps({"projects": {workspace: {"allowedTools": ["Bash"], "hasTrustDialogAccepted": False}}}).encode(), TARGET)[0])
        self.assertEqual(kept["projects"][workspace], {"allowedTools": ["Bash"], "hasTrustDialogAccepted": True}, "已有条目的其它键保留")

    def test_untrust_brings_the_dialog_back_for_every_directory_on_the_chain(self) -> None:
        workspace = runner.workspace_path()
        state = {"projects": {"/": {"hasTrustDialogAccepted": True}, "/work": {"hasTrustDialogAccepted": True}}}
        for directory in [str(workspace), *[str(parent) for parent in workspace.parents]]:
            with self.subTest(directory=directory):
                payload, summary = runner.normalized_global_state(json.dumps(state).encode(), TARGET, mutations=[f"untrust:{directory}"])
                self.assertFalse(_client_trusts(json.loads(payload), str(workspace)), "untrust: 下信任框必然出现")
                self.assertIn(str(workspace), summary["untrusted_projects"])

    def test_detected_screens_still_identify_both_trust_dialogs(self) -> None:
        self.assertEqual(runner.detect_screens(TRUST_SCREEN_285), ["trust_directory"])
        self.assertEqual(runner.detect_screens(TRUST_SCREEN_226), ["trust_directory"])

    def test_run_verdict(self) -> None:
        verdict = runner.run_verdict
        trust = ["trust_directory"]
        self.assertEqual(verdict(token_observed=True, auth_gate=False, cli_exit_code=0, screens=[], mutations=[]),
                         ("passed", "token_request_observed"))
        self.assertEqual(verdict(token_observed=True, auth_gate=False, cli_exit_code=0, screens=trust, mutations=[]),
                         ("failed", "trust_dialog_unexpected"), "正常路径出现信任框：预置信任失效，作业的驱动不会确认它")
        self.assertEqual(verdict(token_observed=False, auth_gate=False, cli_exit_code=1, screens=trust, mutations=[]),
                         ("failed", "trust_dialog_unexpected"))
        self.assertEqual(verdict(token_observed=True, auth_gate=False, cli_exit_code=0, screens=trust, mutations=["untrust:/work"]),
                         ("passed", "token_request_observed"), "untrust: 下信任框是预期的，驱动确认后照常判定")
        self.assertEqual(verdict(token_observed=False, auth_gate=False, cli_exit_code=1, screens=trust, mutations=["untrust:/work"]),
                         ("failed", "token_request_missing"))
        self.assertEqual(verdict(token_observed=False, auth_gate=True, cli_exit_code=3, screens=["login"], mutations=[]), ("failed", "auth_gate"))
        self.assertEqual(verdict(token_observed=False, auth_gate=False, cli_exit_code=None, screens=[], mutations=[]), ("failed", "timeout"))
        self.assertEqual(verdict(token_observed=False, auth_gate=False, cli_exit_code=1, screens=[], mutations=[]), ("failed", "token_request_missing"))


class RunnerLaunchTests(unittest.TestCase):
    """客户端参数、驱动入口命令行与启动身份：与中继 TUI 路径逐项相同。"""

    def test_launch_args_equal_the_relay_tui_probe(self) -> None:
        self.assertTrue(_tui_probe_ids())
        for probe_id in _tui_probe_ids():
            item = v5.get_probe(probe_id)
            with self.subTest(probe=probe_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                relay_out, probe_out = root / "relay", root / "probe"
                relay_out.mkdir()
                probe_out.mkdir()
                debug = root / "debug.log"
                debug.write_text("", encoding="utf-8")
                captured: dict = {}

                def fake_drive(claude, model, prompt, environment, **options):
                    captured.update(options, claude=claude, model=model)
                    return {"sent_prompt": True, "transcript": "", "steps": []}

                def fake_sequence(claude, model, inputs, environment, **options):
                    captured.update(options, claude=claude, model=model)
                    return {"all_inputs_sent": True, "sent_inputs": list(inputs), "transcript": "", "steps": []}

                with mock.patch.object(v4, "drive_tui", fake_drive), mock.patch.object(v4, "drive_tui_sequence", fake_sequence), \
                        mock.patch.object(v4, "_new_debug_path", return_value=debug if item.require_debug_log else None):
                    v4._run_tui_probe(claude_bin="/opt/claude/claude", model="m", probe=item, environment=dict(item.env_dict()),
                                      output_dir=relay_out, timeout=10, known_secrets={})
                workspace, fixture_args = v4._fixture_paths(probe_out, item)
                self.assertEqual(PurePosixPath(str(workspace).replace(str(probe_out), "/work/probe-run/results")), runner.workspace_path())
                expected = [str(value).replace(str(relay_out), str(probe_out)) for value in captured["extra_args"]]
                launch = runner.probe_launch(item)
                self.assertEqual(launch, probe.claude_launch_identity(item), "宿主侧组合身份与运行器同一口径")
                self.assertEqual(runner.tui_launch_args(launch, [str(value) for value in fixture_args],
                                                        str(debug) if item.require_debug_log else None), expected)

    def test_cli_argv_is_accepted_by_the_managed_drive_entry(self) -> None:
        config = _config()
        argv = runner.build_cli_argv(config, "/mnt/claude-window-shim", "/work/probe-run/results/workspace", "/mnt/tui-transcript.txt",
                                     ["--no-chrome", "--prompt-suggestions", "false"])
        self.assertEqual(argv[:3], ["python3", "-B", f"{TOOL_ROOT}/claude_drive_tui_cli.py"])
        with mock.patch.object(Path, "exists", return_value=False):
            arguments = drive_cli.parse_arguments(argv[3:])
        self.assertEqual((arguments.claude_bin, arguments.model, arguments.cwd), ("/mnt/claude-window-shim", config["model"], "/work/probe-run/results/workspace"))
        self.assertEqual((arguments.prompt, arguments.total_timeout, arguments.quiet_after), (TOKEN, 75, 5))
        self.assertEqual(arguments.extra_args, ["--no-chrome", "--prompt-suggestions", "false"])
        self.assertIsNone(arguments.term, "与中继 TUI 环境一致：不设 TERM")


class RunnerStubTests(unittest.TestCase):
    def test_link_names_and_hosts(self) -> None:
        self.assertEqual(runner.parse_link_names("1: lo: <LOOPBACK,UP>\n2: eth0@if3: <BROADCAST>\n"), ["lo", "eth0"])
        hosts = runner.build_hosts("127.0.0.1\tlocalhost\n1.2.3.4 api.anthropic.com\n", probe.CLAUDE_STUB_HOSTS)
        self.assertEqual(hosts.splitlines(), ["127.0.0.1\tlocalhost", "127.0.0.1 api.anthropic.com", "127.0.0.1 platform.claude.com"])

    def test_decide_response(self) -> None:
        self.assertEqual(runner.decide_response("POST", "/v1/messages")[0], 400)
        for method, path in (("POST", "/v1/messages/count_tokens"), ("GET", "/api/oauth/profile"), ("GET", "/v1/messages")):
            with self.subTest(path=path):
                self.assertEqual(runner.decide_response(method, path)[0], 404)

    def test_serve_request_detects_token_and_records_only_names(self) -> None:
        token = TOKEN.encode()
        body = gzip.compress(b'{"messages":[{"role":"user","content":"' + token + b'"}]}')
        request = (
            b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: api.anthropic.com:443\r\nAuthorization: Bearer sk-ant-oat01-secret-value\r\n"
            b"anthropic-beta: oauth-2025\r\nContent-Encoding: gzip\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        sent: list[bytes] = []
        record = runner.serve_request(io.BytesIO(request), sent.append, token=token)
        self.assertEqual(
            set(record), {"kind", "host", "method", "path", "header_names", "body_decoded", "token_found", "response_status"},
            "只记主机、方法、路径与头部名称（另有口令是否出现、正文能否解码与替身应答码）",
        )
        self.assertEqual((record["host"], record["method"], record["path"], record["response_status"]), ("api.anthropic.com", "POST", "/v1/messages", 400))
        self.assertTrue(record["token_found"] and runner.is_token_request(record))
        self.assertNotIn("secret-value", json.dumps(record))
        self.assertNotIn("oauth-2025", json.dumps(record))
        self.assertIn("authorization", record["header_names"])
        self.assertIn(b"HTTP/1.1 400 Bad Request", sent[0])
        chunked = b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nZQXPR\r\n17\r\nOBEABCDEFGHIJKLMNOPQRST\r\n0\r\n\r\n"
        self.assertTrue(runner.is_token_request(runner.serve_request(io.BytesIO(chunked), sent.append, token=token)))
        other = b"POST /v1/messages/count_tokens HTTP/1.1\r\nHost: api.anthropic.com\r\nContent-Length: 28\r\n\r\n" + token
        record = runner.serve_request(io.BytesIO(other), sent.append, token=token)
        self.assertTrue(record["token_found"])
        self.assertFalse(runner.is_token_request(record), "只有首个 turn 的 POST /v1/messages 算数")
        self.assertIsNone(runner.serve_request(io.BytesIO(b""), sent.append, token=token))

    def test_screens_and_redaction(self) -> None:
        trust = "╭──╮│ Do you trust the files in this folder? │ ❯ 1. Yes, proceed"
        self.assertEqual(runner.detect_screens(trust), ["trust_directory"])
        self.assertEqual(runner.detect_screens("Select login method:\n 1. Claude account"), ["login"])
        self.assertEqual(runner.detect_screens("Choose the text style that looks best"), ["theme"])
        self.assertEqual(runner.detect_screens("❯ \n? for shortcuts"), [])
        text = runner.redact("Logged in as a.b@example.com sk-ant-oat01-abcdefghijklmnopqrstu Bearer xyz")
        self.assertNotIn("example.com", text)
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertNotIn("xyz", text)

    def test_validate_config_is_closed(self) -> None:
        self.assertEqual(runner.validate_config(_config())["probe_id"], "v4-tui-attachment")
        launch = _config()["launch"]
        bad = {
            "多余键": {**_config(), "extra": 1},
            "缺键": {key: value for key, value in _config().items() if key != "launch"},
            "口令含数字": _config(token="ZQXPROBE1234567890ABCD"),
            "相对路径": _config(tool_root="tools/official_client_capture"),
            "全局状态相对路径": _config(global_state_file=".claude.json"),
            "版本": _config(target_version="2.1"),
            "信任撤销不在工作目录链上": _config(test_mutations=["untrust:/srv"]),
            "Codex 的验收变更": _config(test_mutations=["unack_migration:x"]),
            "覆盖层不含 /work": _config(overlay_targets=["/tmp"]),
            "诊断窗口": _config(diagnostic_window=[1, 2]),
            "launch 缺键": _config(launch={key: value for key, value in launch.items() if key != "fixture"}),
            "launch 类型": _config(launch={**launch, "safe_mode": "no"}),
            "等待非整数": _config(quiet_after_seconds=True),
        }
        for label, config in bad.items():
            with self.subTest(label=label), self.assertRaises(runner.ProbeError):
                runner.validate_config(config)


class HostComboTests(unittest.TestCase):
    """宿主侧：B2-05 真实 2.1.285 清单的 TUI 组合。"""

    def test_real_manifest_combos(self) -> None:
        combos = probe.claude_tui_combos(_jobs())
        # 主模型（official-tui）三个组合与只有主模型时相同；B2-07 补充项的两个非主模型能力 Job 各有一个 TUI 步骤，模型不同
        # 即启动身份不同，各成一个组合（真实清单 14 个 Job 时共 5 个组合、6 个 TUI 作业）。
        main = [combo for combo in combos if set(combo["job_ids"]) == {"official-tui"}]
        extra = [combo for combo in combos if set(combo["job_ids"]) != {"official-tui"}]
        self.assertEqual(sorted(sorted(combo["probe_ids"]) for combo in main),
                         [["v4-tui-attachment"], ["v4-tui-compact", "v4-tui-count-tokens"], ["v4-tui-usage"]],
                         "输入序列与等待时间不进启动身份：compact 与 count-tokens 同一组合")
        self.assertEqual(sorted((combo["model"], combo["probe_ids"], sorted(combo["job_ids"])) for combo in extra),
                         [("claude-fable-5", ["v4-tui-count-tokens"], ["official-model-capability-claude-fable-5"]),
                          ("claude-opus-5", ["v4-tui-count-tokens"], ["official-model-capability-claude-opus-5"])],
                         "非主模型能力 Job 的 TUI 步骤按模型各成一个组合")
        self.assertEqual(sum(len(combo["job_ids"]) for combo in combos), 6)
        for combo in combos:
            with self.subTest(combo=combo["combo_id"]):
                self.assertRegex(combo["combo_id"], r"^tui-[0-9a-f]{12}$")
                self.assertEqual((combo["container"], combo["tool_root"], combo["claude_bin"]), ("capture-cli", TOOL_ROOT, f"/opt/claude-{TARGET}/claude"))
                model = "claude-sonnet-5" if combo in main else combo["job_ids"][0].removeprefix("official-model-capability-")
                self.assertEqual((combo["model"], combo["target_version"], combo["global_state_file"]), (model, TARGET, "/root/.claude.json"))
                config = probe.claude_runner_config(combo, token=TOKEN, test_mutations=["untrust:/work"], diagnostic_window=probe.DIAGNOSTIC_WINDOW)
                self.assertEqual(runner.validate_config(config), config, "宿主生成的运行器参数过运行器自己的闭集校验")
        usage = next(combo for combo in combos if combo["probe_ids"] == ["v4-tui-usage"])
        self.assertTrue(usage["launch"]["debug_log"])
        self.assertEqual(sorted(probe.claude_tui_probe_ids()), sorted(_tui_probe_ids()))

    def test_relay_coordinates_are_pinned(self) -> None:
        parser = relay._build_parser()
        capture = next(action for action in parser._subparsers._group_actions[0].choices.values() if action.prog.endswith("capture"))
        defaults = {action.dest: action.default for action in capture._actions}
        self.assertEqual(str(defaults["claude_global_state_file"]), probe.CLAUDE_GLOBAL_STATE_DEFAULT)
        self.assertEqual(probe.CLAUDE_RELAY_SCRIPT, plan.RELAY_SCRIPT)
        self.assertEqual(probe.CLAUDE_PROBE_FLAG, plan.FW_F_PROBE_FLAG)
        self.assertEqual(probe.CLAUDE_STUB_HOSTS, ["api.anthropic.com", "platform.claude.com"])
        self.assertIn(probe.CLAUDE_GLOBAL_STATE_DEFAULT, probe.CLAUDE_FINGERPRINT_SCOPE[1])
        self.assertIn(probe.CLAUDE_STATE_DIR, probe.CLAUDE_FINGERPRINT_SCOPE[2])
        self.assertEqual(probe.CLAUDE_FINGERPRINT_SCOPE[3], [probe.CLAUDE_CREDENTIALS_FILE], "凭据文件只记元数据")

    def test_malformed_relay_step_is_rejected(self) -> None:
        step = {"argv": ["docker", "exec", "capture-cli", "python3", f"{TOOL_ROOT}/claude_fw_e_relay.py", "capture"], "environment": {}}
        job = types.SimpleNamespace(job_id="j", phase="official", steps=[step])
        with self.assertRaisesRegex(probe.ProbeConfigError, "FW-F v5"):
            probe.claude_tui_combos([job])


FAKE_DOCKER = r'''#!{python}
import hashlib, json, os, sys
args = sys.argv[1:]
log = os.environ["FAKE_DOCKER_LOG"]
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if "unshare" in args:
    sys.stdin.buffer.read()
    config = json.loads(args[-1])
    failing = os.environ.get("FAKE_FAIL_PROBE", "")
    fail = bool(failing) and config["probe_id"] == failing
    result = {{"schema_version": "official-client-launch-probe-run/v1", "combo_id": config["combo_id"],
              "network_interfaces": ["lo"], "diagnostic_window": config["diagnostic_window"],
              "drive_sha256": json.loads(os.environ["FAKE_DRIVE_SHA"]), "test_mutations": config["test_mutations"],
              "private_home": {{"credentials": "synthetic"}}}}
    screens = ["trust_directory"] if any(item.startswith("untrust:") for item in config["test_mutations"]) else []
    if fail:
        result.update(status="failed", reason="token_request_missing",
                      detected_screens=["model_migration"] if config["diagnostic_window"] else [])
    else:
        result.update(status="passed", reason="token_request_observed", detected_screens=screens,
                      token_request={{"host": "api.anthropic.com", "method": "POST", "path": "/v1/messages", "token_found": True}})
    print("CLIENT_LAUNCH_PROBE_RESULT " + json.dumps(result))
elif "ps" in args:
    print("python3 -m something")
else:
    sys.stdin.buffer.read()
    counter = log + ".fingerprints"
    count = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(count + 1))
    entries = {{"sha256:/root/.claude.json": "1" * 64, "/root/.claude/.credentials.json": [33152, 10, 1]}}
    if os.environ.get("FAKE_FINGERPRINT_CHANGE") and count:
        entries["sha256:/root/.claude.json"] = "2" * 64
    print(json.dumps({{"entries": entries}}))
'''


class HostRunTests(unittest.TestCase):
    """以 docker 替身端到端跑 ``run --client claude`` 与 ``verify``。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        docker.chmod(0o700)
        self.data = self.root / "data"
        tools = self.data / "tools" / "official_client_capture"
        tools.mkdir(parents=True)
        source = REPO_ROOT / "tools" / "official_client_capture"
        self.drive = {}
        for name in probe.CLAUDE_DRIVE_FILES:
            (tools / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, tools / name)
            self.drive[name] = hashlib.sha256((source / name).read_bytes()).hexdigest()
        self.campaign = self.root / "campaign"
        self.campaign.mkdir()
        self.output = self.root / "probe-out"
        self.environment = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_DOCKER_LOG": str(self.root / "docker.log"),
            "FAKE_DRIVE_SHA": json.dumps(self.drive),
        }
        self.manifest = {"campaign_id": "claude-code-2_1_285-lp1", "baseline_version": "2.1.226", "target_version": TARGET}

    def main(self, argv: list[str], **env: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with mock.patch.object(probe, "load_campaign_jobs", return_value=(self.manifest, _jobs())) as loader, \
                mock.patch.dict(os.environ, {**self.environment, **env}), \
                mock.patch.object(probe, "QUIESCENCE_POLL_SECONDS", 0.01), contextlib.redirect_stdout(stdout):
            code = probe.main(argv)
        if argv[0] == "run" and code in {0, 4}:
            self.assertEqual(loader.call_args.args[1], "claude", "作业展开按 --client 取适配器")
        return code, stdout.getvalue()

    def run_probe(self, *extra: str, **env: str) -> tuple[int, dict]:
        code, _ = self.main(["run", "--client", "claude", "--campaign-dir", str(self.campaign), "--output-dir", str(self.output),
                             "--data-root", str(self.data), *extra], **env)
        path = self.output / "report.json"
        return code, (json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})

    def verify(self, *extra: str) -> int:
        return self.main(["verify", "--output-dir", str(self.output), *extra])[0]

    def docker_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in (self.root / "docker.log").read_text(encoding="utf-8").splitlines()]

    def test_all_combos_pass_and_verify_needs_the_client(self) -> None:
        code, report = self.run_probe()
        self.assertEqual(code, 0, report.get("problems"))
        self.assertEqual((report["schema_version"], report["client"], report["status"]), (probe.CLAUDE_REPORT_SCHEMA, "claude", "passed"))
        self.assertEqual(len(report["combos"]), 5)
        self.assertEqual(report["tui_job_count"], 6)
        self.assertEqual(report["host_drive_sha256"], self.drive)
        self.assertFalse(report["drive_mismatch"])
        self.assertTrue(all(combo["diagnostic"] is None for combo in report["combos"]))
        self.assertEqual(report["runner_sha256"], hashlib.sha256((SCRIPTS / "claude_launch_probe_runner.py").read_bytes()).hexdigest())
        runner_calls = [call for call in self.docker_calls() if "unshare" in call]
        self.assertEqual(len(runner_calls), 5)
        for call in runner_calls:
            self.assertEqual(call[:4], ["exec", "-i", "capture-cli", "unshare"])
            config = json.loads(call[-1])
            self.assertEqual(runner.validate_config(config), config)
            self.assertEqual((config["overlay_targets"], config["stub_hosts"]), (probe.CLAUDE_OVERLAY_TARGETS, probe.CLAUDE_STUB_HOSTS))
        fingerprints = [call for call in self.docker_calls() if "unshare" not in call and "ps" not in call]
        self.assertEqual([json.loads(call[-1]) for call in fingerprints], [probe.CLAUDE_FINGERPRINT_SCOPE] * 2)
        self.assertEqual(self.verify("--client", "claude"), 0)
        self.assertEqual(self.verify(), 1, "Claude 报告不能当 Codex 报告复核")

    def test_failed_combo_gets_a_diagnostic_rerun_and_blocks(self) -> None:
        code, report = self.run_probe(FAKE_FAIL_PROBE="v4-tui-usage")
        self.assertEqual(code, 4)
        failed = next(combo for combo in report["combos"] if combo["probe_ids"] == ["v4-tui-usage"])
        self.assertEqual(failed["run"]["status"], "failed")
        self.assertEqual(failed["diagnostic"]["detected_screens"], ["model_migration"])
        self.assertEqual(failed["diagnostic"]["diagnostic_window"], probe.DIAGNOSTIC_WINDOW)
        configs = [json.loads(call[-1]) for call in self.docker_calls() if "unshare" in call]
        self.assertEqual(len(configs), 6, "5 个组合各跑一次，失败的那个再以诊断窗口复跑一次")
        self.assertEqual(len({config["token"] for config in configs}), 6, "每次运行都用新口令")
        self.assertEqual(self.verify("--client", "claude"), 1)

    def test_injected_untrusted_directory_is_identified_and_never_releases(self) -> None:
        code, report = self.run_probe("--test-mutation", "untrust:/work")
        self.assertEqual(code, 4, "带验收变更的运行不能作为放行依据")
        self.assertEqual({tuple(combo["run"]["detected_screens"]) for combo in report["combos"]}, {("trust_directory",)})
        self.assertEqual(report["test_mutations"], ["untrust:/work"])
        self.assertIn("带验收变更的运行不能作为放行依据", report["problems"])
        self.assertEqual(self.verify("--client", "claude"), 1)
        path = self.output / "report.json"
        resealed = probe.seal_report({**{key: value for key, value in report.items() if key not in {"report_sha256", "test_mutations", "problems", "status"}},
                                      "test_mutations": [], "only_scenarios": ["v4-tui-usage"], "problems": [], "status": "passed"})
        path.write_text(json.dumps(resealed), encoding="utf-8")
        self.assertEqual(self.verify("--client", "claude"), 1, "只探测部分场景的报告同样拒绝")

    def test_state_drift_and_drive_mismatch_fail(self) -> None:
        code, report = self.run_probe(FAKE_FINGERPRINT_CHANGE="1")
        self.assertEqual(code, 4)
        self.assertEqual(report["fingerprint"]["changed"], ["sha256:/root/.claude.json"])
        code, report = self.run_probe(FAKE_DRIVE_SHA=json.dumps({name: "0" * 64 for name in probe.CLAUDE_DRIVE_FILES}))
        self.assertEqual(code, 4)
        self.assertTrue(report["drive_mismatch"])

    def test_rejects_codex_acceptance_arguments(self) -> None:
        for extra in (["--test-mutation", "unack_migration:x"], ["--only-scenario", "compact-tui"], ["--test-mutation", "untrust:"]):
            with self.subTest(extra=extra):
                code, _ = self.main(["run", "--client", "claude", "--campaign-dir", str(self.campaign), "--output-dir", str(self.output), *extra])
                self.assertEqual(code, 2)

    def test_only_scenario_selects_by_probe(self) -> None:
        code, report = self.run_probe("--only-scenario", "v4-tui-count-tokens")
        self.assertEqual(code, 4)
        self.assertEqual(sorted(combo["probe_ids"] for combo in report["combos"]),
                         [["v4-tui-compact", "v4-tui-count-tokens"], ["v4-tui-count-tokens"], ["v4-tui-count-tokens"]],
                         "按 probe 选中主模型组合与两个非主模型组合")

    def test_verify_binds_campaign_through_the_adapter(self) -> None:
        self.assertEqual(self.run_probe()[0], 0)
        for manifest, expected in (
            ({"campaign_id": "claude-code-2_1_285-lp1", "target_version": TARGET}, 0),
            ({"campaign_id": "other", "target_version": TARGET}, 1),
        ):
            with self.subTest(manifest=manifest), mock.patch.object(claude_upgrade, "load_campaign_manifest", return_value=manifest):
                self.assertEqual(self.verify("--client", "claude", "--campaign-dir", str(self.campaign)), expected)


class DriveCliTests(unittest.TestCase):
    """受管驱动入口 ``claude_drive_tui_cli.py``：参数闭集、调用驱动函数的形状、会话文本落盘与退出码。"""

    def test_parse_rejects_bad_arguments(self) -> None:
        base = ["--claude-bin", "/opt/claude/claude", "--model", "m"]
        good = drive_cli.parse_arguments([*base, "--prompt", "hi", "--", "--no-chrome", ""])
        self.assertEqual(good.extra_args, ["--no-chrome", ""], "-- 之后原样交给驱动（含空串）")
        for label, argv in (
            ("相对客户端路径", ["--claude-bin", "claude", "--model", "m", "--prompt", "x"]),
            ("两种提交都给", [*base, "--prompt", "x", "--input", "y"]),
            ("两种都不给", base),
            ("空文本", [*base, "--input", ""]),
            ("非正超时", [*base, "--prompt", "x", "--total-timeout", "0"]),
            ("相对工作目录", [*base, "--prompt", "x", "--cwd", "work"]),
            ("TERM 含空白", [*base, "--prompt", "x", "--term", "xterm 256"]),
        ):
            with self.subTest(label=label), self.assertRaises(drive_cli.DriveCliError):
                drive_cli.parse_arguments(argv)

    def test_run_calls_the_drive_functions_and_writes_the_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "t.txt"
            calls: list = []

            def fake_drive(claude, model, prompt, env, **options):
                calls.append(("drive", claude, model, prompt, os.getcwd(), env.get("TERM"), options))
                return {"steps": [(1.0, "enter-menu"), (4.0, "sent-prompt")], "sent_prompt": True, "auth_gate_detected": False, "transcript": "screen"}

            argv = ["--claude-bin", "/opt/claude/claude", "--model", "m", "--cwd", str(root), "--prompt", TOKEN, "--total-timeout", "70",
                    "--quiet-after", "5", "--transcript", str(transcript), "--", "--no-chrome"]
            stdout = io.StringIO()
            with mock.patch.object(drive_cli, "drive", fake_drive), mock.patch.dict(os.environ, {}, clear=False), \
                    contextlib.redirect_stdout(stdout):
                os.environ.pop("TERM", None)
                code = drive_cli.main(argv)
            self.assertEqual(code, drive_cli.EXIT_COMPLETE)
            [(kind, claude, model, prompt, cwd, term, options)] = calls
            self.assertEqual((kind, claude, model, prompt, Path(cwd).resolve(), term), ("drive", "/opt/claude/claude", "m", TOKEN, root.resolve(), None))
            self.assertEqual(options, {"extra_args": ("--no-chrome",), "total_timeout": 70, "quiet_after_prompt": 5})
            self.assertEqual(transcript.read_text(encoding="utf-8"), "screen")
            self.assertEqual(oct(transcript.stat().st_mode & 0o777), "0o600")
            summary = json.loads(stdout.getvalue())
            self.assertEqual((summary["schema_version"], summary["input_complete"], summary["inputs_sent"]), (drive_cli.RESULT_SCHEMA, True, 1))
            self.assertNotIn("screen", stdout.getvalue(), "会话文本只进文件，不进标准输出")

            def gate(claude, model, inputs, env, **options):
                calls.append(("sequence", inputs, options))
                return {"steps": [], "sent_inputs": [], "auth_gate_detected": True, "transcript": "Select login method"}

            with mock.patch.object(drive_cli, "drive_sequence", gate), contextlib.redirect_stdout(io.StringIO()):
                code = drive_cli.main(["--claude-bin", "/opt/claude/claude", "--model", "m", "--input", "a", "--input", "b"])
            self.assertEqual(code, drive_cli.EXIT_AUTH_GATE)
            self.assertEqual(calls[-1], ("sequence", ("a", "b"), {"extra_args": ()}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(drive_cli.main(["--claude-bin", "/opt/claude/claude", "--model", "m", "--prompt", "x", "--transcript", str(transcript)]),
                                 drive_cli.EXIT_USAGE, "会话文本文件已存在：拒绝覆盖")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class DriveCliTrustDialogTests(unittest.TestCase):
    """Claude 侧驱动（v3，驱动入口与中继共用）的信任框处理：按选项文字定位确认项；焦点核对不了就绝不回车。"""

    YES = "Yes, I trust this folder"

    def test_trust_selection_by_option_text(self) -> None:
        plan = v3.trust_selection(TRUST_SCREEN_285)
        self.assertEqual((plan["action"], plan["target"], plan["focused"], plan["keys"]), ("move", self.YES, "No, exit", v3.KEY_END),
                         "2.1.285：取消项在前且默认聚焦，用 End 把焦点移到末位的确认项")
        self.assertEqual(plan["options"], ["No, exit", self.YES])
        plan = v3.trust_selection(TRUST_SCREEN_226)
        self.assertEqual((plan["action"], plan["focused"], plan["keys"]), ("confirm", self.YES, b""), "2.1.226：焦点已在确认项上，直接回车")
        cancel_variant = TRUST_SCREEN_285.replace("❯No,exit", "❯No,continuewithouttheseperm\nissions")
        self.assertEqual(v3.trust_selection(cancel_variant)["keys"], v3.KEY_END)
        middle = TRUST_SCREEN_285.replace("Yes,Itrustthisfolder", "Yes,Itrustthisfolder\n\nNo,continuewithoutthesepermissions")
        self.assertEqual(v3.trust_selection(middle)["keys"], v3.KEY_HOME + v3.KEY_DOWN, "确认项在中间：Home 再 Down")
        legacy = "╭──╮│ Do you trust the files in this folder? │ ❯ 1. Yes, proceed │ 2. No, exit"
        self.assertEqual(v3.trust_selection(legacy)["action"], "confirm")
        partial = TRUST_SCREEN_226.split("2.No,exit")[0]
        self.assertEqual(v3.trust_selection(partial), {"action": "wait"}, "取消项还没画出来：渲染不完整，不发键")
        self.assertIsNone(v3.trust_selection("Choose the text style\n❯ 1. Dark mode"), "不是信任框")
        self.assertIsNone(v3.trust_selection(TRUST_SCREEN_285.split("❯No,exit")[0]), "确认项还没画出来：不当信任框处理")
        stale_then_redrawn = TRUST_SCREEN_285 + TRUST_SCREEN_226
        self.assertEqual(v3.trust_selection(stale_then_redrawn)["action"], "confirm", "只解析最后一次完整渲染")

    def _handler(self) -> tuple[v3.TrustDialogHandler, _Clock, list[bytes], list[str]]:
        clock, writes, steps = _Clock(), [], []
        handler = v3.TrustDialogHandler(write=lambda fd, data: writes.append(data), clock=clock)
        return handler, clock, writes, steps

    def test_handler_moves_focus_verifies_the_redraw_then_confirms(self) -> None:
        for label, redraw in (("只重绘指针所在格子（无颜色时的格子差分）", "\x1b[13;1H \x1b[15;1H❯"),
                              ("整行重绘", "\x1b[13;1H  No, exit\x1b[15;1H❯ Yes, I trust this folder")):
            with self.subTest(label=label):
                handler, clock, writes, steps = self._handler()
                transcript = bytearray(TRUST_SCREEN_285.encode("utf-8"))
                self.assertEqual(handler.step(7, TRUST_SCREEN_285, transcript, steps.append), "pending")
                self.assertEqual(writes, [], "首次看到信任框先等它画完、按键处理器就绪")
                clock.now += v3.TRUST_SETTLE_SECONDS
                self.assertEqual(handler.step(7, TRUST_SCREEN_285, transcript, steps.append), "pending")
                self.assertEqual(writes, [v3.KEY_END])
                transcript += redraw.encode("utf-8")
                self.assertEqual(handler.step(7, TRUST_SCREEN_285, transcript, steps.append), "pending", "核对前等重绘")
                clock.now += v3.TRUST_VERIFY_SECONDS
                self.assertEqual(handler.step(7, TRUST_SCREEN_285, transcript, steps.append), "confirmed")
                self.assertEqual(writes, [v3.KEY_END, v3.KEY_ENTER])
                self.assertEqual(steps, ["trust-focus-confirm-option", "enter-menu"])

    def test_handler_confirms_2_1_226_with_a_single_enter(self) -> None:
        handler, clock, writes, steps = self._handler()
        transcript = bytearray(TRUST_SCREEN_226.encode("utf-8"))
        handler.step(7, TRUST_SCREEN_226, transcript, steps.append)
        clock.now += v3.TRUST_SETTLE_SECONDS
        self.assertEqual(handler.step(7, TRUST_SCREEN_226, transcript, steps.append), "confirmed")
        self.assertEqual(writes, [v3.KEY_ENTER], "与共用状态机发出的按键相同")

    def test_handler_never_presses_enter_when_the_focus_cannot_be_verified(self) -> None:
        for label, redraw in (("重绘显示焦点仍在取消项", "\x1b[13;1H❯ No, exit"), ("按键后客户端没有任何重绘", "")):
            with self.subTest(label=label):
                handler, clock, writes, steps = self._handler()
                transcript = bytearray(TRUST_SCREEN_285.encode("utf-8"))
                handler.step(7, TRUST_SCREEN_285, transcript, steps.append)
                outcome = None
                for _ in range(v3.TRUST_MAX_MOVES + 2):
                    clock.now += max(v3.TRUST_SETTLE_SECONDS, v3.TRUST_VERIFY_SECONDS)
                    outcome = handler.step(7, TRUST_SCREEN_285, transcript, steps.append)
                    transcript += redraw.encode("utf-8")
                self.assertEqual(outcome, "unresolved")
                self.assertNotIn(v3.KEY_ENTER, writes, "信任框上拿不准就不回车（回车会按默认项选到退出）")
                self.assertEqual(writes, [v3.KEY_END] * v3.TRUST_MAX_MOVES)
                self.assertEqual(steps[-1], "trust-dialog-unresolved")
                self.assertEqual(handler.step(7, TRUST_SCREEN_285, transcript, steps.append), "unresolved", "放弃后不再发任何键")
                self.assertEqual(len(writes), v3.TRUST_MAX_MOVES)

    def test_handler_waits_for_a_complete_render_and_ignores_other_menus(self) -> None:
        handler, clock, writes, steps = self._handler()
        partial = TRUST_SCREEN_226.split("2.No,exit")[0]
        for _ in range(3):
            clock.now += 5
            self.assertEqual(handler.step(7, partial, bytearray(partial.encode()), steps.append), "pending")
        self.assertEqual(writes, [])
        self.assertIsNone(handler.step(7, "Choose the text style\n❯ 1. Dark mode", bytearray(), steps.append))


# 伪终端里的替身客户端（FAKE_CLAUDE_MODE）：
# * ``285``／``226``：先画信任框（2.1.285 取消项在前且默认聚焦、不带编号；2.1.226 确认项在前、带编号），焦点变化时只重画
#   指针所在的格子（与客户端无颜色时按格子差分重绘同形），回车按焦点确认或取消（取消后保持存活，避免平台差异）；
# * ``285-trusted``：工作目录已预置信任，直接进主界面。
# 主界面：2.1.285 的页脚是「⏵⏵ auto mode on (shift+tab to cycle) · ← for agents」（不显示「? for shortcuts」），2.1.226 是
# 「⏸ manual mode on · ? for shortcuts · ← for agents」。每次提交：输入框随消息增加下移重画（❯ 重新输出），页脚只重画提示
# 部分为「esc to interrupt · ← for agents」，忙碌 FAKE_CLAUDE_BUSY_SECONDS 秒（其间收到的输入记成 bytes_while_busy），再画
# 回答、下移输入框并把提示部分改回空闲。按键、选择与提交都记进 FAKE_CLAUDE_LOG。
FAKE_CLAUDE_BODY = r"""
import json, os, select, sys, time, tty

MODE = os.environ["FAKE_CLAUDE_MODE"]
LOG = os.environ["FAKE_CLAUDE_LOG"]
BUSY_SECONDS = float(os.environ.get("FAKE_CLAUDE_BUSY_SECONDS", "0.3"))
tty.setraw(0)
AUTO = MODE.startswith("285")
OPTIONS = ["No, exit", "Yes, I trust this folder"] if AUTO else ["Yes, I trust this folder", "No, exit"]
NUMBERED = not AUTO
FIRST_ROW = 13
FOOTER_ROW = 40
MODE_LABEL = "⏵⏵ auto mode on (shift+tab to cycle)" if AUTO else "⏸ manual mode on"
IDLE_HINTS = "← for agents" if AUTO else "? for shortcuts · ← for agents"
BUSY_HINTS = "esc to interrupt · ← for agents"
HINT_COLUMN = len(MODE_LABEL) + 4
state = {"focus": 0, "screen": "prompt" if MODE == "285-trusted" else "trust", "line": b"", "turns": 0, "box": 8}
buffer = bytearray()


def out(text):
    os.write(1, text.encode("utf-8"))


def record(kind, value):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "value": value}) + "\n")


def row(index):
    pointer = "❯ " if index == state["focus"] else "  "
    number = f"{index + 1}. " if NUMBERED else ""
    return f"\x1b[{FIRST_ROW + 2 * index};1H{pointer}{number}{OPTIONS[index]}"


def move(target):
    old = state["focus"]
    state["focus"] = target % len(OPTIONS)
    if state["focus"] != old:
        out(f"\x1b[{FIRST_ROW + 2 * old};1H \x1b[{FIRST_ROW + 2 * state['focus']};1H❯")


def input_box():
    out(f"\x1b[{state['box']};1H" + "─" * 40 + f"\x1b[{state['box'] + 1};1H❯  \x1b[{state['box'] + 2};1H" + "─" * 40)


def hints(text, previous):
    out(f"\x1b[{FOOTER_ROW};{HINT_COLUMN}H{text}" + " " * max(0, len(previous) - len(text)))


def main_screen():
    notice = " ▎ Auto mode is now Claude Code's default permission mode.\r\n 1 more notice hidden\r\n" if AUTO else ""
    out("\x1b[2J\x1b[H ▐▛███▜▌ Claude Code\r\n" + notice + " ● high · /effort\r\n")
    input_box()
    out(f"\x1b[{FOOTER_ROW};1H{MODE_LABEL} · {IDLE_HINTS}")


def read_input(timeout):
    readable, _, _ = select.select([0], [], [], timeout)
    if not readable:
        return b""
    data = os.read(0, 1024)
    if not data:
        raise SystemExit(0)
    return data


def submit():
    state["turns"] += 1
    record("prompt", state["line"].decode("utf-8"))
    out(f"\x1b[{state['box']};1H❯ {state['line'].decode('utf-8')}")
    state["line"] = b""
    state["box"] += 3
    input_box()
    hints(BUSY_HINTS, IDLE_HINTS)
    deadline = time.time() + BUSY_SECONDS
    while time.time() < deadline:
        data = read_input(max(0.0, deadline - time.time()))
        if data:
            record("bytes_while_busy", data.decode("latin-1"))
            buffer.extend(data)
    out(f"\x1b[{state['box']};1H RESPONSE {state['turns']}")
    state["box"] += 2
    input_box()
    hints(IDLE_HINTS, BUSY_HINTS)


if state["screen"] == "trust":
    out("\x1b[2J\x1b[H" + "─" * 40 + "\r\n Accessing workspace:\r\n\r\n /work/fake\r\n\r\n"
        " Quick safety check: Is this a project you created or one you trust?\r\n\r\n Security guide\r\n"
        + "".join(row(index) for index in range(len(OPTIONS)))
        + f"\x1b[{FIRST_ROW + 2 * len(OPTIONS) + 1};1H Enter to confirm · Esc to cancel\r\n")
else:
    main_screen()

while True:
    try:
        data = read_input(None)
    except OSError:
        break
    record("bytes", data.decode("latin-1"))
    buffer.extend(data)
    while buffer:
        if state["screen"] == "trust":
            for sequence, action in ((b"\x1b[F", "end"), (b"\x1b[H", "home"), (b"\x1b[B", "down"), (b"\x1b[A", "up"), (b"\r", "enter")):
                if buffer.startswith(sequence):
                    del buffer[:len(sequence)]
                    break
            else:
                if buffer.startswith(b"\x1b") and len(buffer) < 3:
                    break
                del buffer[:1]
                action = None
            if action == "end":
                move(len(OPTIONS) - 1)
            elif action == "home":
                move(0)
            elif action == "down":
                move(state["focus"] + 1)
            elif action == "up":
                move(state["focus"] - 1)
            elif action == "enter":
                choice = OPTIONS[state["focus"]]
                record("selected", choice)
                if choice.startswith("Yes"):
                    state["screen"] = "prompt"
                    main_screen()
                else:
                    state["screen"] = "exited"
                    out("\x1b[2J\x1b[H exiting\r\n")
        elif state["screen"] == "prompt":
            if buffer.startswith(b"\r"):
                del buffer[:1]
                submit()
            else:
                state["line"] += bytes(buffer[:1])
                del buffer[:1]
        else:
            buffer.clear()
"""


class DriveCliTrustPtyTests(unittest.TestCase):
    """在伪终端里端到端驱动替身客户端：Claude 侧驱动（驱动入口与中继共用）在 2.1.285 上确认信任框、认出 auto 模式页脚下的
    主输入框并提交；共用状态机作反向对照（选到「No, exit」；已信任时也认不出 2.1.285 的主输入框）。"""

    YES = "Yes, I trust this folder"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fake = self.root / "claude"
        self.fake.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE_BODY, encoding="utf-8")
        self.fake.chmod(0o700)
        self.log = self.root / "fake-claude.jsonl"

    def environment(self, mode: str, busy_seconds: float = 0.3) -> dict[str, str]:
        return {"FAKE_CLAUDE_MODE": mode, "FAKE_CLAUDE_LOG": str(self.log), "FAKE_CLAUDE_BUSY_SECONDS": str(busy_seconds),
                "PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get("PATH", "")}

    def events(self, kind: str) -> list[str]:
        if not self.log.exists():
            return []
        return [item["value"] for item in map(json.loads, self.log.read_text(encoding="utf-8").splitlines()) if item["kind"] == kind]

    def keys_before_prompt(self) -> list[str]:
        chunks = self.events("bytes")
        return chunks[:chunks.index(TOKEN)] if TOKEN in chunks else chunks

    def input_bytes(self, kind: str = "bytes") -> list[str]:
        """收到的输入，去掉驱动收尾时发的 Ctrl-C（``_stop`` 的 ``\x03``，不是输入）。"""

        return [chunk for chunk in self.events(kind) if chunk != "\x03"]

    def test_cli_entry_confirms_trust_and_submits_on_2_1_285(self) -> None:
        """经运行器实际调用的入口 ``main``（与启动探测同样的参数形状）：信任框上先移焦点再确认，auto 模式页脚下的主输入框
        认作可以输入，口令进了输入框。"""

        transcript = self.root / "transcript.txt"
        argv = ["--claude-bin", str(self.fake), "--model", "m", "--cwd", str(self.root), "--prompt", TOKEN,
                "--total-timeout", "25", "--quiet-after", "1", "--transcript", str(transcript), "--", "--no-chrome"]
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, self.environment("285")), contextlib.redirect_stdout(stdout):
            code = drive_cli.main(argv)
        summary = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, drive_cli.EXIT_COMPLETE, summary)
        self.assertEqual(self.events("selected"), [self.YES])
        self.assertEqual(self.events("prompt"), [TOKEN])
        self.assertEqual(self.keys_before_prompt(), [v3.KEY_END.decode(), "\r"], "先用 End 把焦点移到确认项，核对后再回车")
        self.assertEqual([tag for _, tag in summary["steps"]][:3], ["trust-focus-confirm-option", "enter-menu", "sent-prompt"])
        self.assertEqual(sum(1 for _, tag in summary["steps"] if tag == "enter-menu"), 1, "运行器的 menus_accepted 口径不变")
        flat = "".join(transcript.read_text(encoding="utf-8").split())
        self.assertIn("Yes,Itrustthisfolder", flat)
        self.assertIn("automodeon(shift+tabtocycle)", flat)
        self.assertNotIn("?forshortcuts", flat, "2.1.285 的 auto 模式页脚没有「? for shortcuts」")

    def test_cli_entry_submits_when_the_workspace_is_already_trusted(self) -> None:
        """正常路径（私有 HOME 与中继都预置了信任）：直接进 auto 模式主界面，不经信任框就提交。"""

        argv = ["--claude-bin", str(self.fake), "--model", "m", "--prompt", TOKEN, "--total-timeout", "20", "--quiet-after", "1"]
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, self.environment("285-trusted")), contextlib.redirect_stdout(stdout):
            code = drive_cli.main(argv)
        summary = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, drive_cli.EXIT_COMPLETE, summary)
        self.assertEqual(self.events("prompt"), [TOKEN])
        self.assertEqual([tag for _, tag in summary["steps"]][:1], ["sent-prompt"])

    def test_shared_state_machine_cannot_finish_on_2_1_285(self) -> None:
        """反向对照（ARM64 实测的两处现象）：共用状态机在 2.1.285 的信任框上回车选到「No, exit」；已信任时认不出 auto 模式
        页脚下的主输入框，一条输入都不发。"""

        result = drive_claude_tui.drive(str(self.fake), "m", TOKEN, self.environment("285"), total_timeout=4, quiet_after_prompt=1)
        self.assertFalse(result["sent_prompt"])
        self.assertEqual(self.events("selected"), ["No, exit"])
        self.assertEqual(self.keys_before_prompt()[:1], ["\r"])
        self.log.unlink()
        result = drive_claude_tui.drive(str(self.fake), "m", TOKEN, self.environment("285-trusted"), total_timeout=6, quiet_after_prompt=1)
        self.assertFalse(result["sent_prompt"])
        self.assertEqual((result["steps"], self.input_bytes()), ([], []), "主输入框已就绪，共用状态机却一个键都没发")
        self.assertEqual(drive_claude_tui.classify_screen(result["transcript"]), "waiting")
        self.assertEqual(v3.classify_screen(result["transcript"]), "idle_prompt")

    def test_cli_driver_sends_the_same_single_enter_on_2_1_226(self) -> None:
        result = drive_cli.drive(str(self.fake), "m", TOKEN, self.environment("226"), total_timeout=25, quiet_after_prompt=1)
        self.assertTrue(result["sent_prompt"], result["steps"])
        self.assertEqual(self.events("selected"), [self.YES])
        self.assertEqual(self.keys_before_prompt(), ["\r"], "焦点已在确认项上：只发一个回车，与共用状态机相同")

    def test_cli_sequence_driver_waits_for_the_auto_mode_footer_to_go_idle(self) -> None:
        """compact 一类多轮场景：第一轮回答期间页脚是「… · esc to interrupt · ← for agents」，忙碌页脚里的「← for agents」
        不算空闲，第二轮等页脚改回空闲再发（忙碌 2 秒，长于驱动每轮提交后等的 1 秒）。"""

        result = drive_cli.drive_sequence(str(self.fake), "m", ("ALPHAINPUT", "BRAVOINPUT"), self.environment("285", busy_seconds=2.0),
                                          total_timeout=40, quiet_after_last_input=1)
        self.assertTrue(result["all_inputs_sent"], result["steps"])
        self.assertEqual(self.events("selected"), [self.YES])
        self.assertEqual(self.events("prompt"), ["ALPHAINPUT", "BRAVOINPUT"])
        self.assertEqual(self.input_bytes("bytes_while_busy"), [], "忙碌期间一个键都不发")


class ClaudeTuiScreenStateTests(unittest.TestCase):
    """Claude 侧主输入框判定（v3.classify_screen）：2.1.285 的 auto 模式页脚与 2.1.226 的页脚。"""

    # 2026-10-07 m4 ARM64 实测（claude-code-2_1_285-b210-20261007t114446z-client-launch-probe，v4-tui-usage 组合的
    # tui_visible_tail，空格被光标定位吃掉；❯ 后是不换行空格）。
    IDLE_285 = (
        "\n▐▛███▛█ClaudeCodev2.1.285\n▝▜██████▀Sonnet5·ClaudeAPI\n▝▝▝▝/work/probe-run/results/workspace\n"
        "▎AutomodeisnowClaudeCode'sdefaultpermissionmode.\n▎AutomodeletsClaudehandlepermissionpromptsautomatically.\n"
        "▎https://code.claude.com/docs/en/permission-modes\n1morenoticehidden\n●high·/effort\n" + "─" * 80 + "\n❯\xa0 \n"
        + "─" * 80 + "\n⏵⏵automodeon(shift+tabtocycle)·←foragents\n▟▟\n█▟█▟\n"
    )
    # 2.1.226 真实采集（complete-v16 v4-tui-compact 的 tui-transcript.log）里的空闲与忙碌页脚。
    IDLE_226 = "─" * 40 + "❯" + "─" * 40 + "⏸manualmodeon·?forshortcuts·←foragents●high·/effort"
    BUSY_226 = "❯只回复FW_F_V4_COMPACT_SEED_OK，不得调用任何工具。✻Orchestrating…" + "─" * 40 + "❯" + "─" * 40 + "⏸manualmodeon·esctointerrupt·←foragents●high·/effort✽✻"

    def test_2_1_285_auto_mode_footer_is_ready_for_input(self) -> None:
        self.assertEqual(drive_claude_tui.classify_screen(self.IDLE_285), "waiting", "共用状态机只认「? for shortcuts」：卡住")
        self.assertEqual(v3.classify_screen(self.IDLE_285), "idle_prompt")

    def test_busy_footer_tail_is_not_idle(self) -> None:
        busy = self.IDLE_285 + "\n❯ " + TOKEN + "\n⏵⏵automodeon(shift+tabtocycle)·esctointerrupt·←foragents\n✻Thinking…"
        self.assertEqual(v3.classify_screen(busy), "busy", "忙碌页脚自带的「← for agents」不算空闲")
        diff_busy = self.IDLE_285 + "\n❯ " + TOKEN + "esctointerrupt·←foragents"
        self.assertEqual(v3.classify_screen(diff_busy), "busy", "按格子差分只重画了提示部分")
        tasks = self.IDLE_285 + "esctointerrupt·ctrl+ttoshowtasks·←foragents"
        self.assertEqual(v3.classify_screen(tasks), "busy")
        idle_again = diff_busy + "RESPONSE❯\xa0 ←foragents"
        self.assertEqual(v3.classify_screen(idle_again), "idle_prompt", "提示部分改回空闲")
        self.assertEqual(v3.classify_screen(idle_again + "esctointerrupt·←foragents"), "busy")

    def test_2_1_226_footers_keep_the_shared_verdict(self) -> None:
        for label, screen in (("空闲", self.IDLE_226), ("忙碌", self.BUSY_226), ("忙碌后回到空闲", self.BUSY_226 + "?forshortcuts·←foragents"),
                              ("只有指针", "❯"), ("忙碌提示", "Working…\nesc to interrupt")):
            with self.subTest(label=label):
                self.assertEqual(v3.classify_screen(screen), drive_claude_tui.classify_screen(screen))

    def test_menus_and_login_keep_precedence_and_pointer_is_required(self) -> None:
        self.assertEqual(v3.classify_screen("Select login method\n❯ Claude account\n⏵⏵ auto mode on (shift+tab to cycle)"), "auth_gate")
        self.assertEqual(v3.classify_screen(TRUST_SCREEN_285), "menu")
        self.assertEqual(v3.classify_screen("⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"), "waiting", "没有主输入指针")


class RelayTuiDriverTests(unittest.TestCase):
    """中继的 TUI 场景（v3／v4／v5）与驱动入口用同一份 Claude 侧驱动，并端到端走一遍 v4 的 TUI 场景执行函数。"""

    def test_relay_paths_bind_the_claude_side_driver(self) -> None:
        self.assertIs(v4.drive_tui, v3.claude_tui_drive)
        self.assertIs(v4.drive_tui_sequence, v3.claude_tui_drive_sequence)
        self.assertIs(drive_cli.drive, v3.claude_tui_drive)
        self.assertIs(drive_cli.drive_sequence, v3.claude_tui_drive_sequence)
        tui_probe = next(probe_id for probe_id in v3.PROBE_IDS if v3.get_probe(probe_id).driver == "tui")
        item = v3.get_probe(tui_probe)
        calls: list = []

        def fake(claude, model, prompt, environment, **options):
            calls.append((claude, model, prompt))
            return {"sent_prompt": True, "transcript": "", "steps": []}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(v3, "claude_tui_drive", fake):
            summary = v3.run_claude_fw_f_probe(claude_bin="/opt/claude/claude", model="m", probe_id=tui_probe,
                                               environment=dict(item.env_dict()), output_dir=Path(directory) / "out",
                                               timeout=10, known_secrets={})
        self.assertEqual(calls, [("/opt/claude/claude", "m", item.prompt)], "v3 的 TUI 场景（v5 清单里的 v4-replay-tui）也走它")
        self.assertTrue(summary["valid"])

    def test_v4_tui_probe_submits_on_2_1_285_end_to_end(self) -> None:
        item = dataclasses.replace(v5.get_probe("v4-tui-attachment"), wait_seconds=1)
        self.assertFalse(item.require_debug_log)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "claude"
            fake.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE_BODY, encoding="utf-8")
            fake.chmod(0o700)
            log = root / "fake-claude.jsonl"
            output = root / "results"
            output.mkdir()
            environment = {**item.env_dict(), "FAKE_CLAUDE_MODE": "285-trusted", "FAKE_CLAUDE_LOG": str(log),
                           "PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get("PATH", "")}
            result = v4._run_tui_probe(claude_bin=str(fake), model="m", probe=item, environment=environment,
                                       output_dir=output, timeout=20, known_secrets={})
            prompts = [entry["value"] for entry in map(json.loads, log.read_text(encoding="utf-8").splitlines()) if entry["kind"] == "prompt"]
        self.assertTrue(result["input_complete"], result["steps"])
        self.assertEqual(prompts, [item.prompt], "中继的 TUI 场景把固定提示词提交进 2.1.285 的主输入框")


class EntryWiringTests(unittest.TestCase):
    def test_launch_probe_step_registers_the_client_runner(self) -> None:
        for client, runner_name in (("codex", "client_launch_probe_runner.py"), ("claude", "claude_launch_probe_runner.py")):
            with self.subTest(client=client):
                names = {item.name for item in es.STEP_BY_ID_BY_CLIENT[client]["client-launch-probe"].inputs}
                self.assertIn(f"driver:{runner_name}", names)
                self.assertTrue((SCRIPTS / runner_name).is_file())
                self.assertEqual(CT.spec(client)["launch_probe_runner"], runner_name)
        self.assertIsNone(CT.pending("claude", "step:job-rehearsal"))
        self.assertIsNone(CT.pending("claude", "step:client-launch-probe"))

    def test_claude_job_rehearsal_and_launch_probe_commands(self) -> None:
        from tools.official_client_capture.tests.test_ci_entry_steps import Fixture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fx = Fixture(root)
            fx.params.update({"RUNROOT": str(root / "run"), "CLIENT": "claude"})
            calls: list[list[str]] = []

            def run(argv, cwd, env, log):
                calls.append(list(argv))
                return 1

            orchestrator = eo.Orchestrator(fx.params, driver_dir=fx.driver, run_dir=root / "runs", steps_dir=fx.steps_dir,
                                           runner=run, es_runner=fx.runner)
            campaign = root / "campaign" / "campaign.json"
            with mock.patch.object(orchestrator, "record_product", return_value=campaign):
                orchestrator.step_job_rehearsal()
                orchestrator.step_client_launch_probe()
        collect, launch = calls
        self.assertEqual(collect[collect.index("-m") + 1], CT.spec("claude")["job_rehearsal_module"])
        self.assertEqual(collect[-2:], ["--client", "claude"])
        self.assertEqual(launch[-2:], ["--client", "claude"])
        self.assertTrue(launch[2].endswith("client_launch_probe.py"))


if __name__ == "__main__":
    unittest.main()
