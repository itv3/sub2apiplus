"""R19 客户端启动探测（tools/arm64_capture_driver/driver/client_launch_probe*.py、stage1-finish.sh）的离线测试。

覆盖：
* 探测表覆盖：解析 run_official_relay_scenario.sh，调用受管 drive_codex_tui.py 的场景集合必须与探测表一致；
  用 bash 真实执行各调用点的参数展开（把 docker exec 换成回显函数），与探测算出的驱动参数逐项相等；
  驱动 argparse 选项集合钉住（新增选项必须先评估是否影响客户端命令行）；脚本默认值逐项对照；
  受管代码里只有 relay 场景脚本调用 TUI 驱动。
* 运行器纯函数：接口解析、私有 hosts、验收变更、替身应答（accounts/check、/models、426、400、404）、
  请求解析（Content-Length／chunked／压缩）、口令判定、头部取值不入记录、脱敏、参数闭集。
* 宿主工具：作业组合展开与去重、默认值、非法取值拒绝；以替身 docker 端到端跑 run（通过、失败并诊断复跑、
  指纹漂移），verify 拒绝未通过／篡改／验收参数报告。
* stage1-finish.sh 续跑：探测失败不写 stage1.env、不跑 atomic；修复后重跑沿用已有 Job 演练收据、重跑探测、
  补 atomic 并写出含 PROBE 的 stage1.env；半途中断的目录换新后缀；旧 stage1.env 改名留档。
"""

from __future__ import annotations

import contextlib
import gzip
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = REPO_ROOT / "tools" / "official_client_capture"
RELAY_SCRIPT = TOOL_ROOT / "run_official_relay_scenario.sh"
DRIVE_SCRIPT = TOOL_ROOT / "drive_codex_tui.py"
SCRIPTS = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"


def _load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(f"client_launch_probe_test_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


probe = _load("client_launch_probe")
runner = _load("client_launch_probe_runner")

# 只影响交互流程、不进入客户端命令行的驱动选项（各带一个取值）。
NON_ARGV_OPTIONS = {"--warmup", "--slash", "--warmup-ready", "--slash-hold", "--prompt", "--prompt-hold", "--log"}
# drive_codex_tui.py 的全部选项：新增任何一项都必须先评估它是否影响客户端命令行、探测是否需要复刻。
DRIVE_OPTIONS = NON_ARGV_OPTIONS | {
    "--codex-bin", "--model", "--cwd", "--context-window", "--disable", "--enable", "--config",
    "--no-bypass", "--approval-policy", "--sandbox-mode",
}


def _relay_text() -> str:
    return RELAY_SCRIPT.read_text(encoding="utf-8")


def _scenario_markers(text: str) -> dict[str, str]:
    """``case "$scenario" in`` 里场景名 → 提示标记。"""

    lines = text.splitlines()
    start = lines.index('case "$scenario" in')
    end = next(index for index in range(start + 1, len(lines)) if lines[index] == "esac")
    markers: dict[str, str] = {}
    current = None
    for line in lines[start + 1:end]:
        label = re.fullmatch(r"  ([a-z0-9-]+)\)\s*", line)
        if label:
            current = label.group(1)
            continue
        found = re.search(r"prompt='(__[A-Z_]+__)'", line)
        if found and current and current not in markers:
            markers[current] = found.group(1)
    return markers


def _marker_blocks(text: str) -> dict[str, list[str]]:
    """``if/elif [[ $prompt == "__X__" ]]`` 分支 → 分支体各行。"""

    lines = text.splitlines()
    heads = [
        (index, match.group(1)) for index, line in enumerate(lines)
        if (match := re.fullmatch(r'(?:el)?if \[\[ \$prompt == "(__[A-Z_]+__)" \]\]; then', line))
    ]
    blocks = {}
    for position, (index, marker) in enumerate(heads):
        end = heads[position + 1][0] if position + 1 < len(heads) else len(lines)
        body = []
        for line in lines[index + 1:end]:
            if line in {"else", "fi"}:
                break
            body.append(line)
        blocks[marker] = body
    return blocks


def _drive_call(body: list[str]) -> tuple[list[str], list[str]]:
    """分支里调用 drive_codex_tui.py 的完整命令行（含续行）与其前面的简单赋值行。"""

    drive_index = next(index for index, line in enumerate(body) if "drive_codex_tui.py" in line)
    start = max(index for index in range(drive_index + 1) if "docker exec" in body[index])
    end = next(index for index in range(drive_index, len(body)) if "--log " in body[index])
    # 只取字面量赋值：命令替换（如 daemon 分支建 home 的 daemon_tool 调用）不影响驱动参数，也不能在夹具里执行。
    prelude = [
        line for line in body[:start]
        if (re.match(r"^\s*[a-z_]+=", line) or re.match(r"^\s*\[\[ .* \]\] && [a-z_]+=", line)) and "$(" not in line
    ]
    command = body[start:end + 1]
    command[-1] = re.sub(r"\s*2>&1 \| tail -[0-9]+ \|\| true\s*$", "", command[-1])
    return prelude, command


def _expand_call_site(marker: str, environment: dict[str, str]) -> list[str]:
    """用 bash 真实执行调用点的参数展开，返回驱动实际收到的参数（去掉脚本路径）。"""

    text = _relay_text()
    prelude, command = _drive_call(_marker_blocks(text)[marker])
    default_line = next(line for line in text.splitlines() if line.startswith("DISABLE_FEATURES="))
    # daemon 分支以 -e CODEX_HOME=... 指定独立 home（探测用运行器自己的 daemon home 复刻，另有用例核对）。
    command[0] = re.sub(r'docker exec (?:-e \S+ )*"\$capture_container" python3', "__probe_capture", command[0], count=1)
    self_check = "__probe_capture" in command[0]
    assert self_check, command[0]
    script = "\n".join([
        "set -u",
        "__probe_capture() { printf '%s\\0' \"$@\"; }",
        "capture_container=probe-container",
        "capture_tool_root=/probe/tool-root",
        "codex_bin=/probe/codex",
        "model=probe-model",
        "run_id=probe-run",
        "guardian_probe_path=/var/tmp/probe.txt",
        default_line,
        *prelude,
        *command,
    ])
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environment}
    completed = subprocess.run(["bash", "-c", script], capture_output=True, env=env, check=True)
    arguments = completed.stdout.decode("utf-8").split("\0")[:-1]
    assert arguments[0] == "/probe/tool-root/drive_codex_tui.py", arguments[:2]
    return arguments[1:]


def _argv_affecting(arguments: list[str]) -> list[str]:
    kept, skip = [], False
    for item in arguments:
        if skip:
            skip = False
            continue
        if item in NON_ARGV_OPTIONS:
            skip = True
            continue
        kept.append(item)
    return kept


class CallSiteCoverageTests(unittest.TestCase):
    """探测表必须覆盖 relay 场景脚本里全部 TUI 调用点，且参数展开逐项一致。"""

    def test_tui_scenarios_match_script(self) -> None:
        text = _relay_text()
        markers = _scenario_markers(text)
        drive_markers = {marker for marker, body in _marker_blocks(text).items() if any("drive_codex_tui.py" in line for line in body)}
        tui_scenarios = {scenario for scenario, marker in markers.items() if marker in drive_markers}
        self.assertEqual(tui_scenarios, set(probe.TUI_SCENARIOS), "新增或删除了 TUI 场景，探测表必须同步")
        for scenario, spec in probe.TUI_SCENARIOS.items():
            self.assertEqual(markers[scenario], spec["marker"], scenario)
        self.assertEqual(drive_markers, {spec["marker"] for spec in probe.TUI_SCENARIOS.values()})

    def test_call_site_expansion_equals_probe_options(self) -> None:
        cases = [
            {},
            {"DISABLE_FEATURES": ""},
            {"DISABLE_FEATURES": "plugins apps remote_compaction_v2"},
            {"DISABLE_FEATURES": "plugins"},
            {"TUI_ENABLE": "token_budget runtime_metrics", "TUI_DISABLE": "remote_compaction_v2", "CONTEXT_WINDOW": "120000"},
            {"TUI_DISABLE": "alpha beta", "TUI_HOLD": "9", "TUI_WARMUP": "hello world", "GUARDIAN_PROBE_PATH": "/tmp/x"},
        ]
        for scenario, spec in probe.TUI_SCENARIOS.items():
            for environment in cases:
                with self.subTest(scenario=scenario, environment=environment):
                    actual = _argv_affecting(_expand_call_site(spec["marker"], environment))
                    expected = [
                        "--codex-bin", "/probe/codex", "--model", "probe-model", "--cwd", spec["cwd"],
                        *probe.drive_options(scenario, environment),
                    ]
                    self.assertEqual(actual, expected)

    def test_call_site_variables_are_known(self) -> None:
        """调用点引用的变量只能是探测已处理的集合，新增变量必须先纳入探测。"""

        text = _relay_text()
        allowed_common = {"capture_container", "capture_tool_root", "codex_bin", "model", "run_id", "f"}
        for scenario, spec in probe.TUI_SCENARIOS.items():
            prelude, command = _drive_call(_marker_blocks(text)[spec["marker"]])
            names = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", "\n".join(prelude + command)))
            interactive_only = {"TUI_WARMUP", "TUI_SLASH", "TUI_READY", "TUI_HOLD", "TUI_PROMPT", "guardian_probe_path"}
            derived = {"ctx_opt", "work", "daemon_home"}
            with self.subTest(scenario=scenario):
                self.assertLessEqual(names, allowed_common | interactive_only | derived | set(spec["variables"]))

    def test_daemon_call_site_uses_independent_home_without_overrides(self) -> None:
        """daemon 调用点经 -e CODEX_HOME 指向独立 home、命令行零覆盖；功能开关与探测的 daemon_features 同源。"""

        text = _relay_text()
        prelude, command = _drive_call(_marker_blocks(text)["__DAEMON_TUI__"])
        self.assertIn('daemon_home="/root/.codex-daemon-$run_id"', "\n".join(prelude))
        self.assertTrue(command[0].strip().startswith('docker exec -e CODEX_HOME="$daemon_home" "$capture_container" python3'))
        self.assertIn('daemon_tool prepare --home "$daemon_home" --disable-features "$DISABLE_FEATURES"', text)
        for environment in ({}, {"DISABLE_FEATURES": ""}, {"DISABLE_FEATURES": "plugins"}):
            with self.subTest(environment=environment):
                self.assertEqual(probe.drive_options("daemon-tui", environment), [])
        self.assertEqual(probe.daemon_features("daemon-tui", {}), ["plugins", "apps"])
        self.assertEqual(probe.daemon_features("daemon-tui", {"DISABLE_FEATURES": "plugins"}), ["plugins"])
        self.assertIsNone(probe.daemon_features("compact-tui", {}))
        # 探测的独立 home 同样不在临时目录下（客户端对 temp_dir 下的 CODEX_HOME 拒建 helper 别名）。
        self.assertFalse(str(runner.DAEMON_HOME).startswith(("/tmp/", "/var/tmp/")))
        self.assertTrue(str(runner.DAEMON_HOME).startswith("/work/"))

    def test_drive_option_set_is_pinned(self) -> None:
        options = set(re.findall(r'add_argument\("(--[a-z-]+)"', DRIVE_SCRIPT.read_text(encoding="utf-8")))
        self.assertEqual(options, DRIVE_OPTIONS, "drive_codex_tui.py 选项变化：先评估是否影响客户端命令行，再同步探测")

    def test_relay_defaults_match_script(self) -> None:
        text = _relay_text()
        expected_lines = {
            "CAPTURE_CONTAINER": f"capture_container=${{CAPTURE_CONTAINER:-{probe.RELAY_DEFAULTS['CAPTURE_CONTAINER']}}}",
            "CAPTURE_ROOT": f"capture_root=${{CAPTURE_ROOT:-{probe.RELAY_DEFAULTS['CAPTURE_ROOT']}}}",
            "CODEX_BIN": f"codex_bin=${{CODEX_BIN:-{probe.RELAY_DEFAULTS['CODEX_BIN']}}}",
            "MODEL": f"model=${{MODEL:-{probe.RELAY_DEFAULTS['MODEL']}}}",
            "DISABLE_FEATURES": f'DISABLE_FEATURES=${{DISABLE_FEATURES:-"{probe.RELAY_DEFAULTS["DISABLE_FEATURES"]}"}}',
        }
        lines = set(text.splitlines())
        for key, line in expected_lines.items():
            with self.subTest(key=key):
                self.assertIn(line, lines)
        self.assertIn(f"capture_tool_root=${{CAPTURE_TOOL_ROOT:-$capture_root/{probe.TOOL_ROOT_SUFFIX}}}", lines)

    def test_only_relay_script_invokes_tui_driver(self) -> None:
        callers = sorted(
            path.relative_to(TOOL_ROOT).as_posix()
            for path in TOOL_ROOT.rglob("*")
            if path.is_file() and path.suffix in {".sh", ".py"} and "tests" not in path.relative_to(TOOL_ROOT).parts
            and path != DRIVE_SCRIPT and "drive_codex_tui" in path.read_text(encoding="utf-8", errors="replace")
        )
        self.assertEqual(callers, [RELAY_SCRIPT.name], "新增了 TUI 驱动调用方，启动探测必须覆盖它")

    def test_runner_reuses_drive_visible(self) -> None:
        sys.path.insert(0, str(TOOL_ROOT))
        try:
            spec = importlib.util.spec_from_file_location("drive_codex_tui_for_probe_test", DRIVE_SCRIPT)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(TOOL_ROOT))
        raw = "\x1b[1m│ T │ │ r │\x1b[0m".encode() + "Trust this folder? Codex".encode()
        self.assertIn("trust_directory", runner.detect_screens(module.visible(raw, collapse=True)))


CONTAINER_CODEX_HOME = Path("/root/.codex")


def _runner_config(**overrides):
    config = {
        "combo_id": "guardian-tui-abc", "tool_root": "/root/oauth-capture/tools/official_client_capture",
        "codex_bin": "/opt/codex/bin/codex", "model": "m", "cwd": "/work", "drive_options": ["--disable", "apps"],
        "token": "ZQXPROBEABCDEFGHIJKLMNOPQRST", "prompt_hold_seconds": 20, "deadline_seconds": 90,
        "overlay_targets": list(probe.OVERLAY_TARGETS), "stub_hosts": list(probe.STUB_HOSTS),
        "test_mutations": [], "diagnostic_window": None, "daemon": None,
    }
    config.update(overrides)
    return config


class RunnerFunctionTests(unittest.TestCase):
    SAMPLE_CONFIG = (
        'model = "m"\n[projects."/"]\ntrust_level = "trusted"\n\n[projects."/work"]\ntrust_level = "trusted"\n\n'
        '[notice.model_migrations]\n"gpt-x" = "gpt-y"\nother = "z"\n\n[tui]\nscreen_reader_detection_done = true\n'
    )

    def test_link_names_and_hosts(self) -> None:
        output = "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n2: eth0@if12: <BROADCAST> mtu 1500\n"
        self.assertEqual(runner.parse_link_names(output), ["lo", "eth0"])
        hosts = runner.build_hosts("127.0.0.1\tlocalhost\n1.2.3.4 chatgpt.com\n172.18.0.5\tabc\n", ["chatgpt.com", "auth.openai.com"])
        self.assertEqual(hosts.splitlines(), ["127.0.0.1\tlocalhost", "172.18.0.5\tabc", "127.0.0.1 chatgpt.com", "127.0.0.1 auth.openai.com"])

    def test_config_mutations_only_touch_target(self) -> None:
        untrusted = runner.apply_config_mutation(self.SAMPLE_CONFIG, "untrust:/work")
        self.assertNotIn('[projects."/work"]', untrusted)
        self.assertIn('[projects."/"]', untrusted)
        self.assertIn('"gpt-x" = "gpt-y"', untrusted)
        unacked = runner.apply_config_mutation(self.SAMPLE_CONFIG, "unack_migration:gpt-x")
        self.assertNotIn('"gpt-x" = "gpt-y"', unacked)
        self.assertIn('other = "z"', unacked)
        self.assertIn('[projects."/work"]', unacked)
        for bad in ("untrust:/missing", "delete:/work", "untrust:/w ork", "unack_migration:none"):
            with self.subTest(bad=bad), self.assertRaises(runner.ProbeError):
                runner.apply_config_mutation(self.SAMPLE_CONFIG, bad)

    def test_decide_response(self) -> None:
        models = b'{"models":[{"slug":"m"}]}'
        self.assertEqual(runner.decide_response("GET", "/backend-api/codex/models", {}, models, 'W/"e"')[:2], (200, "OK"))
        self.assertEqual(runner.decide_response("GET", "/backend-api/codex/models", {}, models, 'W/"e"')[3], {"ETag": 'W/"e"'})
        self.assertEqual(runner.decide_response("GET", "/backend-api/codex/models", {}, None, None)[0], 404)
        self.assertEqual(runner.decide_response("GET", "/backend-api/codex/responses", {"upgrade": "websocket"}, models, None)[0], 426)
        self.assertEqual(runner.decide_response("POST", "/backend-api/codex/responses", {}, models, None)[0], 400)
        status, _, payload, _ = runner.decide_response("GET", "/backend-api/wham/accounts/check", {"chatgpt-account-id": "acct-1"}, models, None)
        self.assertEqual(status, 200)
        document = json.loads(payload)
        self.assertEqual(document["accounts"], [{"id": "acct-1", "workspace_backend_origin": "NO_CONSTRAINT", "account_routing_override": "NO_CONSTRAINT", "structure": "personal"}])
        self.assertEqual((document["account_ordering"], document["default_account_id"]), (["acct-1"], "acct-1"))
        self.assertEqual(runner.decide_response("GET", "/backend-api/wham/accounts/check", {}, models, None)[0], 404)
        self.assertEqual(runner.decide_response("GET", "/backend-api/wham/usage", {}, models, None)[0], 404)

    def test_models_payload_from_cache(self) -> None:
        payload, etag, summary = runner.models_payload_from_cache({"models": [{"slug": "m"}], "etag": "E", "client_version": "9.1.0", "fetched_at": "t"})
        self.assertEqual(json.loads(payload), {"models": [{"slug": "m"}]})
        self.assertEqual((etag, summary["source"], summary["model_count"], summary["client_version"]), ("E", "codex_home_cache", 1, "9.1.0"))
        self.assertEqual(runner.models_payload_from_cache({"models": []})[2], {"source": "invalid"})
        self.assertEqual(runner.models_payload_from_cache([])[2], {"source": "invalid"})

    def test_serve_request_detects_token_and_never_records_header_values(self) -> None:
        token = b"ZQXPROBEABCDEFGHIJKLMNOPQRST"
        body = gzip.compress(b'{"input":[{"content":"' + token + b'"}]}')
        request = (
            b"POST /backend-api/codex/responses?x=1 HTTP/1.1\r\nHost: chatgpt.com\r\nAuthorization: Bearer secret-value-123\r\n"
            b"chatgpt-account-id: acct-secret\r\nContent-Encoding: gzip\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        sent = []
        record = runner.serve_request(io.BytesIO(request), sent.append, token=token, models_payload=None, models_etag=None)
        self.assertTrue(record["token_found"])
        self.assertEqual((record["method"], record["path"], record["host"], record["response_status"]), ("POST", "/backend-api/codex/responses", "chatgpt.com", 400))
        self.assertIn(b"HTTP/1.1 400 Bad Request", sent[0])
        serialized = json.dumps(record)
        self.assertNotIn("secret-value-123", serialized)
        self.assertNotIn("acct-secret", serialized)
        self.assertIn("authorization", record["header_names"])
        chunked = b"POST /x/responses HTTP/1.1\r\nHost: chatgpt.com\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nZQXPR\r\n17\r\nOBEABCDEFGHIJKLMNOPQRST\r\n0\r\n\r\n"
        self.assertTrue(runner.serve_request(io.BytesIO(chunked), sent.append, token=token, models_payload=None, models_etag=None)["token_found"])
        partial = b"POST /x/responses HTTP/1.1\r\nHost: chatgpt.com\r\nContent-Length: 20\r\n\r\nZQXPROBEABCDEFGHIJKL"
        self.assertFalse(runner.serve_request(io.BytesIO(partial), sent.append, token=token, models_payload=None, models_etag=None)["token_found"])
        upgrade = b"GET /backend-api/codex/responses HTTP/1.1\r\nHost: chatgpt.com\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        record = runner.serve_request(io.BytesIO(upgrade), sent.append, token=token, models_payload=None, models_etag=None)
        self.assertTrue(record["upgrade_websocket"])
        self.assertIn(b"HTTP/1.1 426 Upgrade Required", sent[-1])
        self.assertIsNone(runner.serve_request(io.BytesIO(b""), sent.append, token=token, models_payload=None, models_etag=None))

    def test_decode_body(self) -> None:
        self.assertEqual(runner.decode_body("identity", b"abc"), (b"abc", True))
        self.assertEqual(runner.decode_body("gzip", gzip.compress(b"abc")), (b"abc", True))
        self.assertEqual(runner.decode_body("br", b"abc"), (b"", False))
        self.assertEqual(runner.decode_body("gzip", b"not-gzip"), (b"", False))
        compressor = None
        with contextlib.suppress(ImportError):
            from compression import zstd as compressor  # type: ignore[no-redef]
        if compressor is None:
            with contextlib.suppress(ImportError):
                import zstandard

                compressor = types.SimpleNamespace(compress=zstandard.ZstdCompressor().compress)
        if compressor is not None:
            self.assertEqual(runner.decode_body("zstd", compressor.compress(b"abc")), (b"abc", True))

    def test_screens_and_redaction(self) -> None:
        self.assertEqual(runner.detect_screens("│T││r│u│s│t│t│h│i│s│f│o│l│d│e│r│?"), ["trust_directory"])
        self.assertEqual(runner.detect_screens("Codexjustgotanupgrade•Modelchangedtogpt"), ["model_migration", "model_changed"])
        self.assertEqual(runner.detect_screens("›Askcodex"), [])
        text = runner.redact("user a.b@example.com --ask-for-approval on-request sk-abcdefghijklmnopqrstu Bearer xyz eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig")
        self.assertNotIn("example.com", text)
        self.assertIn("--ask-for-approval", text)
        self.assertNotIn("sk-abcdefghijklmnop", text)
        self.assertNotIn("xyz", text)
        self.assertNotIn("eyJhbGci", text)

    def test_validate_config_is_closed(self) -> None:
        self.assertEqual(runner.validate_config(_runner_config(), codex_home=CONTAINER_CODEX_HOME)["cwd"], "/work")
        with self.assertRaises(runner.ProbeError):
            runner.validate_config(_runner_config(), codex_home=Path("/home/other/.codex"))
        bad = {
            "口令含数字": _runner_config(token="ZQXPROBE1234567890ABCD"),
            "口令过短": _runner_config(token="ZQX"),
            "工作目录不在覆盖层": _runner_config(cwd="/srv/app"),
            "相对路径": _runner_config(codex_bin="codex"),
            "多余键": {**_runner_config(), "extra": 1},
            "诊断窗口非法": _runner_config(diagnostic_window=[1, 2]),
            "保持时间非整数": _runner_config(prompt_hold_seconds=True),
            "缺 daemon 键": {key: value for key, value in _runner_config().items() if key != "daemon"},
            "daemon 多余键": _runner_config(daemon={"features": ["plugins"], "home": "/tmp/x"}),
            "daemon 功能名非法": _runner_config(daemon={"features": ["Plugins"]}),
            "daemon 功能名重复": _runner_config(daemon={"features": ["apps", "apps"]}),
            "daemon home 不在覆盖层": _runner_config(daemon={"features": ["apps"]}, overlay_targets=["/root/.codex", "/tmp"]),
        }
        for label, config in bad.items():
            with self.subTest(label=label), self.assertRaises(runner.ProbeError):
                runner.validate_config(config, codex_home=CONTAINER_CODEX_HOME)

    def test_daemon_config_and_tool_invocation(self) -> None:
        daemon = _runner_config(daemon={"features": ["plugins", "apps"]})
        self.assertEqual(runner.validate_config(daemon, codex_home=CONTAINER_CODEX_HOME)["daemon"], {"features": ["plugins", "apps"]})
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 3, stdout='noise\n{"status": "failed", "mode": "embedded"}\n', stderr="")

        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            payload = runner._daemon_tool("/tool", "status", "--home", str(runner.DAEMON_HOME))
        self.assertEqual(payload, {"status": "failed", "mode": "embedded", "exit_code": 3})
        self.assertEqual(
            calls[0][:6],
            ["python3", "-B", "/tool/drive_codex_daemon.py", "--homes-parent", "/work", "status"],
        )

    def test_drive_argv_replaces_prompt_only(self) -> None:
        config = _runner_config(drive_options=["--no-bypass", "--config", 'approvals_reviewer="auto_review"', "--disable", "apps"])
        argv = runner.build_drive_argv(config, "/mnt/shim", "/mnt/tui.log")
        self.assertEqual(argv[:2], ["python3", "/root/oauth-capture/tools/official_client_capture/drive_codex_tui.py"])
        self.assertEqual(argv[2:8], ["--codex-bin", "/mnt/shim", "--model", "m", "--cwd", "/work"])
        self.assertEqual(argv[8:13], ["--no-bypass", "--config", 'approvals_reviewer="auto_review"', "--disable", "apps"])
        self.assertEqual(argv[13:], ["--prompt", config["token"], "--prompt-hold", "20", "--log", "/mnt/tui.log"])


def _job(job_id: str, environment: dict[str, str], *, argv: list[str] | None = None, phase: str = "official"):
    step = {"argv": argv or ["bash", "/repo/tools/official_client_capture/run_official_relay_scenario.sh"], "environment": environment, "timeout_seconds": 60}
    return types.SimpleNamespace(job_id=job_id, phase=phase, steps=[step])


BASE_ENV = {"CAPTURE_CONTAINER": "capture-cli", "CAPTURE_ROOT": "/root/oauth-capture", "CODEX_BIN": "/opt/codex-9.1.0/bin/codex", "MODEL": "main-model"}


class ComboExtractionTests(unittest.TestCase):
    def test_combos_dedupe_and_defaults(self) -> None:
        jobs = [
            _job("a-compact", {**BASE_ENV, "SCENARIO": "compact-tui", "RUN_ID": "r1"}),
            _job("b-compact-wham", {**BASE_ENV, "SCENARIO": "compact-tui", "RUN_ID": "r2", "REQUIRE_REQUEST_PATH": "/x"}),
            _job("c-compact-v2", {**BASE_ENV, "SCENARIO": "compact-tui", "DISABLE_FEATURES": "plugins apps remote_compaction_v2"}),
            _job("d-guardian", {**BASE_ENV, "SCENARIO": "guardian-tui", "GUARDIAN_PROBE_PATH": "/var/tmp/p"}),
            _job("e-image", {**BASE_ENV, "SCENARIO": "image"}),
            _job("f-core", {**BASE_ENV}, argv=["docker", "exec", "capture-cli", "python3", "/x/capture.py"]),
            _job("g-review-defaults", {"SCENARIO": "review-tui"}),
            _job("h-daemon", {**BASE_ENV, "SCENARIO": "daemon-tui", "RUN_ID": "r3"}),
        ]
        combos = probe.tui_combos(jobs)
        by_jobs = {tuple(combo["job_ids"]): combo for combo in combos}
        self.assertEqual(
            set(by_jobs),
            {("a-compact", "b-compact-wham"), ("c-compact-v2",), ("d-guardian",), ("g-review-defaults",), ("h-daemon",)},
        )
        daemon = by_jobs[("h-daemon",)]
        self.assertEqual((daemon["cwd"], daemon["drive_options"], daemon["daemon_features"]), ("/tmp/tui-probe", [], ["plugins", "apps"]))
        self.assertEqual(probe.runner_config(daemon, token="T", test_mutations=[], diagnostic_window=None)["daemon"], {"features": ["plugins", "apps"]})
        self.assertNotIn("daemon_features", by_jobs[("a-compact", "b-compact-wham")])
        self.assertIsNone(
            probe.runner_config(by_jobs[("a-compact", "b-compact-wham")], token="T", test_mutations=[], diagnostic_window=None)["daemon"]
        )
        self.assertEqual(by_jobs[("a-compact", "b-compact-wham")]["drive_options"], ["--disable", "plugins", "--disable", "apps"])
        self.assertEqual(by_jobs[("c-compact-v2",)]["drive_options"][-2:], ["--disable", "remote_compaction_v2"])
        guardian = by_jobs[("d-guardian",)]
        self.assertEqual((guardian["cwd"], guardian["drive_options"][:2]), ("/work", ["--no-bypass", "--approval-policy"]))
        review = by_jobs[("g-review-defaults",)]
        self.assertEqual(
            (review["container"], review["codex_bin"], review["model"], review["tool_root"], review["cwd"]),
            ("capture-cli", "/root/.local/bin/codex", "gpt-5.4", "/root/oauth-capture/tools/official_client_capture", "/tmp/review-probe"),
        )
        self.assertTrue(all(combo["combo_id"].startswith(combo["scenario"] + "-") for combo in combos))

    def test_rejects_values_bash_would_expand_differently(self) -> None:
        for environment in (
            {"DISABLE_FEATURES": "plu*ins"},
            {"DISABLE_FEATURES": "a;b"},
            {"TUI_ENABLE": "x$(id)"},
            {"CODEX_BIN": "relative/codex"},
            {"MODEL": "m m"},
        ):
            with self.subTest(environment=environment), self.assertRaises(probe.ProbeConfigError):
                probe.tui_combos([_job("x", {**BASE_ENV, "SCENARIO": "compact-tui", **environment})])

    def test_token_has_no_digits(self) -> None:
        for _ in range(20):
            token = probe.new_token()
            self.assertRegex(token, r"^[A-Z]{28}$")
            runner.validate_config(_runner_config(token=token), codex_home=CONTAINER_CODEX_HOME)


FAKE_DOCKER = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
log = os.environ["FAKE_DOCKER_LOG"]
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if "unshare" in args:
    sys.stdin.buffer.read()
    config = json.loads(args[-1])
    failing = os.environ.get("FAKE_FAIL_SCENARIO", "")
    fail = bool(failing) and config["combo_id"].startswith(failing)
    result = {{"schema_version": "arm64-client-launch-probe-run/v1", "combo_id": config["combo_id"],
              "network_interfaces": ["lo"], "drive_codex_tui_sha256": os.environ.get("FAKE_DRIVE_SHA", "d" * 64),
              "diagnostic_window": config["diagnostic_window"]}}
    if fail:
        result.update(status="failed", reason="drive_exited",
                      detected_screens=["trust_directory"] if config["diagnostic_window"] else [])
    else:
        result.update(status="passed", reason="token_request_observed",
                      token_request={{"method": "POST", "path": "/backend-api/codex/responses"}})
    print("noise line")
    print("CLIENT_LAUNCH_PROBE_RESULT " + json.dumps(result))
elif "ps" in args:
    counter = log + ".ps"
    index = (int(open(counter).read()) if os.path.exists(counter) else 0) + 1
    open(counter, "w").write(str(index))
    show_from = int(os.environ.get("FAKE_PS_FROM", "0"))
    show_until = int(os.environ.get("FAKE_PS_UNTIL", "0"))
    present = (show_from and index >= show_from) or (show_until and index <= show_until)
    print(os.environ["FAKE_PS_LINE"] if present else "python3 -m something")
else:
    sys.stdin.buffer.read()
    counter = os.environ["FAKE_DOCKER_LOG"] + ".fingerprints"
    count = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(count + 1))
    entries = {{"/root/.codex/config.toml": [33152, 10, 1]}}
    if os.environ.get("FAKE_FINGERPRINT_CHANGE") and count:
        entries["/root/.codex/config.toml"] = [33152, 11, 2]
    print(json.dumps({{"entries": entries}}))
'''


class HostToolRunTests(unittest.TestCase):
    def _run(self, root: Path, *, env: dict[str, str], extra: list[str] | None = None, quiescence_timeout: float = 5.0) -> tuple[int, dict]:
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        docker.chmod(0o700)
        campaign = root / "campaign"
        campaign.mkdir(exist_ok=True)
        output = root / "probe-out"
        jobs = [
            _job("a-compact", {**BASE_ENV, "SCENARIO": "compact-tui"}),
            _job("b-guardian", {**BASE_ENV, "SCENARIO": "guardian-tui"}),
        ]
        manifest = {"campaign_id": "c-test", "baseline_version": "9.0.0", "target_version": "9.1.0"}
        environment = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_DOCKER_LOG": str(root / "docker.log"),
            "FAKE_PS_LINE": " ".join(probe.UNSHARE) + " python3 - {}", **env,
        }
        stdout = io.StringIO()
        with mock.patch.object(probe, "load_campaign_jobs", return_value=(manifest, jobs)), mock.patch.dict(os.environ, environment), \
                mock.patch.object(probe, "QUIESCENCE_POLL_SECONDS", 0.01), mock.patch.object(probe, "QUIESCENCE_TIMEOUT_SECONDS", quiescence_timeout), \
                contextlib.redirect_stdout(stdout):
            code = probe.main(["run", "--campaign-dir", str(campaign), "--output-dir", str(output), *(extra or [])])
        report_path = output / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        return code, report

    def _docker_calls(self, root: Path) -> list[list[str]]:
        return [json.loads(line) for line in (root / "docker.log").read_text(encoding="utf-8").splitlines()]

    def test_all_combos_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = self._run(root, env={})
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "passed")
            self.assertEqual([combo["scenario"] for combo in report["combos"]], ["compact-tui", "guardian-tui"])
            self.assertTrue(all(combo["diagnostic"] is None for combo in report["combos"]))
            self.assertEqual(report["tui_job_count"], 2)
            calls = self._docker_calls(root)
            runner_calls = [call for call in calls if "unshare" in call]
            self.assertEqual(len(runner_calls), 2)
            for call in runner_calls:
                self.assertEqual(call[:4], ["exec", "-i", "capture-cli", "unshare"])
                self.assertEqual(call[3:11], probe.UNSHARE)
                config = json.loads(call[-1])
                self.assertIsNone(config["diagnostic_window"])
                self.assertEqual(config["overlay_targets"], probe.OVERLAY_TARGETS)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(["verify", "--output-dir", str(root / "probe-out")]), 0)
            self.assertEqual(len(list((root / "probe-out" / "attempts").iterdir())), 1)

    def test_failed_combo_gets_diagnostic_rerun_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = self._run(root, env={"FAKE_FAIL_SCENARIO": "guardian-tui"}, extra=["--test-mutation", "untrust:/work"])
            self.assertEqual(code, 4)
            self.assertEqual(report["status"], "failed")
            guardian = next(combo for combo in report["combos"] if combo["scenario"] == "guardian-tui")
            self.assertEqual(guardian["run"]["status"], "failed")
            self.assertEqual(guardian["diagnostic"]["detected_screens"], ["trust_directory"])
            self.assertEqual(guardian["diagnostic"]["diagnostic_window"], probe.DIAGNOSTIC_WINDOW)
            configs = [json.loads(call[-1]) for call in self._docker_calls(root) if "unshare" in call]
            self.assertEqual([config["test_mutations"] for config in configs], [["untrust:/work"]] * 3)
            self.assertEqual(len({config["token"] for config in configs}), 3, "每次运行都用新口令")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(["verify", "--output-dir", str(root / "probe-out")]), 1)

    def test_fingerprint_drift_and_leftovers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, report = self._run(Path(directory), env={"FAKE_FINGERPRINT_CHANGE": "1"})
            self.assertEqual(code, 4)
            self.assertEqual(report["fingerprint"]["changed"], ["/root/.codex/config.toml"])
        with tempfile.TemporaryDirectory() as directory:
            # 开跑前没有、结束时出现探测运行器：残留进程判失败。
            code, report = self._run(Path(directory), env={"FAKE_PS_FROM": "2"})
            self.assertEqual(code, 4)
            self.assertEqual(len(report["leftover_processes"]), 1)

    def test_waits_for_interrupted_runner_before_starting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # 上一次被中断的运行器还在（前两次查询可见）：等它退出后再开跑，本次正常通过。
            code, report = self._run(Path(directory), env={"FAKE_PS_UNTIL": "2"})
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "passed")
            self.assertGreaterEqual(report["quiescence_wait_seconds"], 0.0)
        with tempfile.TemporaryDirectory() as directory:
            # 一直不退出：超时失败关闭，不派发运行器、不写报告。
            root = Path(directory)
            code, report = self._run(root, env={"FAKE_PS_FROM": "1"}, quiescence_timeout=0.05)
            self.assertEqual(code, 2)
            self.assertEqual(report, {})
            self.assertFalse(any("unshare" in call for call in self._docker_calls(root)))

    def test_verify_rejects_tampering_and_acceptance_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = self._run(root, env={})
            self.assertEqual(code, 0)
            path = root / "probe-out" / "report.json"
            for label, change in (
                ("篡改组合结果", lambda r: r["combos"][0]["run"].update(status="failed")),
                ("篡改状态不改摘要", lambda r: r.update(status="passed", problems=["x"])),
            ):
                with self.subTest(label=label):
                    tampered = json.loads(json.dumps(report))
                    change(tampered)
                    path.write_text(json.dumps(tampered), encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(probe.main(["verify", "--output-dir", str(root / "probe-out")]), 1)
            for label, change in (("验收变更", {"test_mutations": ["untrust:/work"]}), ("部分场景", {"only_scenarios": ["guardian-tui"]})):
                with self.subTest(label=label):
                    resealed = probe.seal_report({**{k: v for k, v in report.items() if k != "report_sha256"}, **change})
                    path.write_text(json.dumps(resealed), encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(probe.main(["verify", "--output-dir", str(root / "probe-out")]), 1)

    def test_verify_binds_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self._run(root, env={})[0], 0)
            from tools.official_client_capture import codex_upgrade

            for manifest, expected in (
                ({"campaign_id": "c-test", "target_version": "9.1.0"}, 0),
                ({"campaign_id": "c-other", "target_version": "9.1.0"}, 1),
                ({"campaign_id": "c-test", "target_version": "9.2.0"}, 1),
            ):
                with self.subTest(manifest=manifest), mock.patch.object(codex_upgrade, "load_campaign_manifest", return_value=manifest), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(probe.main(["verify", "--output-dir", str(root / "probe-out"), "--campaign-dir", str(root / "campaign")]), expected)

    def test_rejects_unknown_acceptance_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "campaign").mkdir()
            for extra in (["--test-mutation", "chmod:/work"], ["--only-scenario", "image"]):
                with self.subTest(extra=extra), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(probe.main(["run", "--campaign-dir", str(root / "campaign"), "--output-dir", str(root / "o"), *extra]), 2)


STUB_PROBE = r'''#!{python}
import json, os, sys
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as handle:
    handle.write("probe " + " ".join(sys.argv[1:]) + "\n")
if sys.argv[1] == "run":
    sys.exit(int(os.environ.get("STUB_PROBE_RC", "0")))
sys.exit(int(os.environ.get("STUB_VERIFY_RC", "0")))
'''
STUB_JOB_REHEARSAL = r'''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as handle:
    handle.write("job-rehearsal " + args[0] + "\n")
root = Path(args[args.index("--evidence-root") + 1])
output = args[args.index("--output") + 1]
if args[0] == "finalize" and os.environ.get("STUB_JR_FAIL"):
    sys.exit(1)
(root / output).write_text("{}", encoding="utf-8")
'''
STUB_DOCKER = r'''#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
for ((i=1; i<=$#; i++)); do
  if [ "${!i}" = "--evidence-root" ]; then j=$((i+1)); root="${!j}"; fi
done
host_root="$STUB_DATA_ROOT${root#/capture}"
echo '{}' > "$host_root/receipt.json"
'''


class _RoundFixture:
    """一轮参数文件（由 env.example.sh 模板替换占位符生成）与最小数据根布局。"""

    def __init__(self, root: Path) -> None:
        self.data_root = root / "data"
        self.runroot = root / "runroot"
        for sub in ("control", "evidence/campaigns", "staging", "environment", "tools/official_client_capture"):
            (self.data_root / sub).mkdir(parents=True, exist_ok=True)
        self.runroot.mkdir(mode=0o700)
        text = (SCRIPTS / "env.example.sh").read_text(encoding="utf-8")
        for key in ("REPLACE_APPROVED_PROFILE_SHA256", "REPLACE_OFFICIAL_CODEX_SHA256", "REPLACE_OFFICIAL_PACKAGE_SHA256", "REPLACE_KILO_SHA256"):
            text = text.replace(key, "a" * 64)
        text = text.replace("REPLACE_CODEX_ACCOUNT_ID", "91").replace("REPLACE_API_KEY_ID", "92")
        text = re.sub(r"^D=.*$", f"D={self.data_root}", text, flags=re.M)
        text = re.sub(r"^RUNROOT=.*$", f"RUNROOT={self.runroot}", text, flags=re.M)
        self.round = re.search(r"^ROUND=(.*)$", text, flags=re.M).group(1)
        self.stamp = re.search(r"^STAMP=(.*)$", text, flags=re.M).group(1)
        self.env_file = self.runroot / "env.sh"
        self.env_file.write_text(text, encoding="utf-8")
        self.env_file.chmod(0o600)
        self.env = {"ARM64_VC_ENV": str(self.env_file)}


class Stage1FinishResumeTests(unittest.TestCase):
    """stage1-finish.sh 的续跑语义：探测失败阻断收口，修复后单独重跑即可补齐。"""

    def _fixture(self, root: Path):
        fixture = _RoundFixture(root)
        drv = root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "wait_state.py", "stage1-finish.sh"):
            (drv / name).write_bytes((SCRIPTS / name).read_bytes())
        (drv / "client_launch_probe.py").write_text(STUB_PROBE.format(python=sys.executable), encoding="utf-8")
        tools = fixture.data_root / "tools" / "official_client_capture"
        (tools / "codex_upgrade_job_rehearsal_receipt.py").write_text(STUB_JOB_REHEARSAL, encoding="utf-8")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(STUB_DOCKER, encoding="utf-8")
        (bin_dir / "docker").chmod(0o700)
        pre = fixture.data_root / "evidence" / "campaigns" / "preflight"
        pre.mkdir(parents=True)
        (fixture.runroot / "stage1.partial.env").write_text(
            f"DEPLOY={fixture.data_root}/control/deploy.json\nENV={fixture.data_root}/environment/p0\nPRECID=preflight\nPRE={pre}\n",
            encoding="utf-8",
        )
        log = root / "stub.log"
        log.touch()
        env = {
            **fixture.env, "STUB_LOG": str(log), "STUB_DATA_ROOT": str(fixture.data_root),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        return fixture, drv, env, log

    def _finish(self, drv: Path, env: dict[str, str], cwd: Path, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(drv / "stage1-finish.sh")], capture_output=True, text=True, env={**os.environ, **env, **extra}, cwd=str(cwd))

    @staticmethod
    def _env_file(path: Path) -> dict[str, str]:
        return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())

    def test_probe_failure_blocks_then_rerun_completes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, drv, env, log = self._fixture(root)
            (fixture.runroot / "stage1.env").write_text("DEPLOY=old\n", encoding="utf-8")

            failed = self._finish(drv, env, root, STUB_PROBE_RC="4")
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertFalse((fixture.runroot / "stage1.env").exists(), "探测失败不得留下 stage1.env")
            self.assertEqual(len(list(fixture.runroot.glob("stage1.env.superseded-*"))), 1, "旧 stage1.env 改名留档")
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual([line.split()[0] + " " + line.split()[1] for line in calls], ["job-rehearsal collect", "job-rehearsal finalize", "probe run"])
            self.assertNotIn("docker", " ".join(calls), "探测失败不得进入 atomic-double")

            log.write_text("", encoding="utf-8")
            done = self._finish(drv, env, root)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("STAGE1_DONE", done.stdout)
            self.assertIn("Job 演练收据已存在，沿用", done.stdout)
            calls = [line.split()[:2] for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([call[0] for call in calls], ["probe", "probe", "docker"])
            self.assertEqual([call[1] for call in calls[:2]], ["run", "verify"])
            values = self._env_file(fixture.runroot / "stage1.env")
            self.assertEqual(set(values), {"DEPLOY", "ENV", "PRECID", "PRE", "JR", "AT", "PROBE"})
            self.assertTrue(values["PROBE"].endswith(f"-client-launch-probe-{fixture.round}-{fixture.stamp}"))
            self.assertTrue(values["JR"].endswith(f"-job-rehearsal-vc5-{fixture.round}-{fixture.stamp}"))
            self.assertEqual(values["AT"], f"codex-atomic-vc0-vc1-{fixture.round}-{fixture.stamp}")

            log.write_text("", encoding="utf-8")
            again = self._finish(drv, env, root)
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertEqual([line.split()[0] for line in log.read_text(encoding="utf-8").splitlines()], ["probe", "probe"], "第三次只重跑探测")
            self.assertIn("atomic-double 收据已存在，沿用", again.stdout)

    def test_interrupted_rehearsal_directory_is_kept_and_redone_under_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, drv, env, log = self._fixture(root)
            interrupted = self._finish(drv, env, root, STUB_JR_FAIL="1")
            self.assertNotEqual(interrupted.returncode, 0)
            base = next(fixture.data_root.glob("control/*-job-rehearsal-vc5-*"))
            self.assertTrue((base / "facts.json").exists())
            self.assertFalse((base / "receipt.json").exists())
            done = self._finish(drv, env, root)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            values = self._env_file(fixture.runroot / "stage1.env")
            self.assertRegex(values["JR"], re.escape(str(base)) + r"-r[0-9]{6}$")
            self.assertTrue((base / "facts.json").exists(), "半途中断的目录原样保留")

    def test_verify_failure_also_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, drv, env, _log = self._fixture(root)
            result = self._finish(drv, env, root, STUB_VERIFY_RC="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((fixture.runroot / "stage1.env").exists())


class DriverWiringTests(unittest.TestCase):
    def test_stage1_hands_over_to_finish_and_stage2_verifies(self) -> None:
        stage1 = (SCRIPTS / "stage1.sh").read_text(encoding="utf-8")
        self.assertIn('bash "$DRV/stage1-finish.sh"', stage1)
        self.assertNotIn("codex_upgrade_job_rehearsal_receipt", stage1, "Job 演练已移入可续跑的收尾段")
        self.assertNotIn("atomic-double-collect", stage1)
        self.assertLess(stage1.index("stage1.partial.env"), stage1.index('bash "$DRV/stage1-finish.sh"'))
        finish = (SCRIPTS / "stage1-finish.sh").read_text(encoding="utf-8")
        self.assertLess(finish.index("codex_upgrade_job_rehearsal_receipt"), finish.index("client_launch_probe.py\" run"))
        self.assertLess(finish.index("client_launch_probe.py\" verify"), finish.index("atomic-double-collect"))
        stage2 = (SCRIPTS / "stage2.sh").read_text(encoding="utf-8")
        self.assertLess(stage2.index("client_launch_probe.py\" verify"), stage2.index("codex_upgrade_policy_certification"))


if __name__ == "__main__":
    unittest.main()
