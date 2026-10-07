"""Claude 客户端启动探测（Claude 升级控制面设计 B2-10）：``client_launch_probe.py --client claude`` 与
``claude_launch_probe_runner.py``、受管驱动入口 ``claude_drive_tui_cli.py`` 的离线测试。

覆盖：
* 运行器纯函数：私有 HOME（合成凭据、隐私设置、全局状态规范化副本；只读全局状态文件，不碰凭据文件；文件不存在时也能
  建）、信任撤销验收变更、客户端参数与中继 ``_run_tui_probe`` 逐项相同、驱动入口命令行、替身应答与请求记录（只记主机、
  方法、路径与头部名称）、交互屏识别、参数闭集；
* 宿主侧：B2-05 真实 2.1.285 清单的 TUI 组合展开与去重、运行器参数与运行器校验互认；以 docker 替身端到端跑 run／verify：
  全部通过、失败组合以 48×160 窗口复跑诊断、注入未确认信任目录被识别为交互屏且报告不能放行、``/root/.claude.json``
  指纹漂移判失败、受管驱动与数据根不一致判失败、未知验收参数拒绝；verify 认客户端、拒绝验收参数报告并按适配器绑定
  预检 Campaign；
* 入口步骤与编排：Claude 的启动探测登记 Claude 运行器，Job 演练与启动探测的命令行追加 ``--client claude``。
"""

from __future__ import annotations

import contextlib
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


class RunnerPrivateHomeTests(unittest.TestCase):
    """私有 HOME：中继 TUI 临时 HOME 的同一布局，凭据合成、全局状态只读一份并规范化。"""

    def test_global_state_normalization_matches_the_relay(self) -> None:
        state = {"oauthAccount": {"accountUuid": "u-1", "emailAddress": "a@b.example"}, "theme": "light", "projects": {"/x": {}}}
        raw = json.dumps(state).encode("utf-8")
        payload, summary = runner.normalized_global_state(raw, TARGET)
        relay_payload, relay_fields = capture_environment._normalized_tui_global_state(raw, TARGET)
        self.assertEqual(payload, relay_payload, "有账号且不带验收变更时与中继的规范化逐字相同")
        self.assertEqual(summary, {"source": "present", "normalized_fields": relay_fields, "oauth_account": "global_state",
                                   "untrusted_projects": []})
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
        self.assertEqual(sorted(summary["untrusted_projects"]), ["/", "/work"])
        self.assertEqual({key: value["hasTrustDialogAccepted"] for key, value in projects.items()}, {"/work": False, "/": False, "/srv": True})
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
        groups = sorted(sorted(combo["probe_ids"]) for combo in combos)
        self.assertEqual(groups, [["v4-tui-attachment"], ["v4-tui-compact", "v4-tui-count-tokens"], ["v4-tui-usage"]],
                         "输入序列与等待时间不进启动身份：compact 与 count-tokens 同一组合")
        self.assertEqual(sum(len(combo["job_ids"]) for combo in combos), 4)
        for combo in combos:
            with self.subTest(combo=combo["combo_id"]):
                self.assertRegex(combo["combo_id"], r"^tui-[0-9a-f]{12}$")
                self.assertEqual((combo["container"], combo["tool_root"], combo["claude_bin"]), ("capture-cli", TOOL_ROOT, f"/opt/claude-{TARGET}/claude"))
                self.assertEqual((combo["model"], combo["target_version"], combo["global_state_file"]), ("claude-sonnet-5", TARGET, "/root/.claude.json"))
                self.assertEqual(set(combo["job_ids"]), {"official-tui"})
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
        self.assertEqual(len(report["combos"]), 3)
        self.assertEqual(report["tui_job_count"], 4)
        self.assertEqual(report["host_drive_sha256"], self.drive)
        self.assertFalse(report["drive_mismatch"])
        self.assertTrue(all(combo["diagnostic"] is None for combo in report["combos"]))
        self.assertEqual(report["runner_sha256"], hashlib.sha256((SCRIPTS / "claude_launch_probe_runner.py").read_bytes()).hexdigest())
        runner_calls = [call for call in self.docker_calls() if "unshare" in call]
        self.assertEqual(len(runner_calls), 3)
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
        self.assertEqual(len(configs), 4)
        self.assertEqual(len({config["token"] for config in configs}), 4, "每次运行都用新口令")
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
        self.assertEqual([combo["probe_ids"] for combo in report["combos"]], [["v4-tui-compact", "v4-tui-count-tokens"]])

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
            with mock.patch.object(drive_cli.drive_claude_tui, "drive", fake_drive), mock.patch.dict(os.environ, {}, clear=False), \
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

            with mock.patch.object(drive_cli.drive_claude_tui, "drive_sequence", gate), contextlib.redirect_stdout(io.StringIO()):
                code = drive_cli.main(["--claude-bin", "/opt/claude/claude", "--model", "m", "--input", "a", "--input", "b"])
            self.assertEqual(code, drive_cli.EXIT_AUTH_GATE)
            self.assertEqual(calls[-1], ("sequence", ("a", "b"), {"extra_args": ()}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(drive_cli.main(["--claude-bin", "/opt/claude/claude", "--model", "m", "--prompt", "x", "--transcript", str(transcript)]),
                                 drive_cli.EXIT_USAGE, "会话文本文件已存在：拒绝覆盖")


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
