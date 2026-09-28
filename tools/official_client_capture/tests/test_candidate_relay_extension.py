"""候选专用 relay 扩展与 Cookie 预热（修好接着跑第 55 项）。

官方 0.157 的 WS 握手与 alpha-search／图像请求都带 Cloudflare Cookie（来自先前响应的 Set-Cookie），候选场景必须
先建立同样的账号 Cookie jar 前提，SPEC-WS-002／EP-015／EP-022 的头序才可比。upstream_byte_relay.py 被已封存的
官方作业声明依赖，不能改；两份候选采集脚本用逐字相同的启动器加载同一份 relay 模块，只包装候选合成分派。

锁定：
1. 启动器在两份脚本里逐字相同，且 relay 文件本身不被改写；
2. 扩展只在 core A05（目标 ≥0.157.0）与 aux（目标 ≥0.156.1）生效，每个 Campaign 只放行第一次预热，
   其余请求原样交回原分派（失败关闭不变）；
3. A05 采集段在 WS 轮次之前经官方入口、独立会话发预热，收尾校验逐字节核对前提；
4. 0.157.0 证据标签的 A05 逐连接规则与新顺序一致，旧版本不变。
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

TOOL_ROOT = Path(__file__).resolve().parents[1]
RELAY = TOOL_ROOT / "upstream_byte_relay.py"
CORE = TOOL_ROOT / "run_candidate_core_capture.sh"
AUX = TOOL_ROOT / "run_candidate_aux_capture.sh"
BEGIN = "# >>> candidate-relay-extension"
END = "# <<< candidate-relay-extension"
PRIME = "POST /backend-api/codex/responses HTTP/1.1"
HEAD = b"POST /backend-api/codex/responses HTTP/1.1\r\nhost: chatgpt.com\r\n\r\n"


def _block(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    start = source.index(BEGIN)
    return source[start:source.index(END, start) + len(END)]


def _launcher_code(path: Path) -> str:
    block = _block(path)
    opener = "IFS= read -r -d '' candidate_relay_launcher <<'PY' || true\n"
    start = block.index(opener) + len(opener)
    return block[start:block.index("\nPY\n", start) + 1]


def _patched_relay(profile: str, version: str, scenario: str | None = None) -> Any:
    """按候选脚本的实参执行启动器（不进入 main），返回被包装后的 relay 模块。"""

    argv = ["-c", str(RELAY), "--cert", "c", "--key", "k", "--codex-version", version,
            "--synthetic-profile", profile, "--allow-synthetic-responses"]
    if scenario is not None:
        argv += ["--candidate-core-scenario", scenario, "--candidate-core-ws-failures", "0"]
    namespace: dict[str, Any] = {"__name__": "candidate_relay_launcher"}
    with mock.patch.dict(os.environ, {"CANDIDATE_RELAY_LAUNCHER_NO_MAIN": "1"}), \
            mock.patch.object(sys, "argv", argv), mock.patch.dict(sys.modules):
        exec(compile(_launcher_code(CORE), "candidate_relay_launcher", "exec"), namespace)  # noqa: S102
    return namespace["relay"]


def _fields(response: Any) -> Any:
    """两份模块对象里的数据类类型不同，按字段比较合成响应。"""

    return None if response is None else (type(response).__name__, dataclasses.asdict(response))


def _pristine_relay() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("upstream_byte_relay_pristine", RELAY)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {spec.name: module}):
        spec.loader.exec_module(module)
    return module


class LauncherIdentityTest(unittest.TestCase):
    def test_launcher_block_is_identical_in_both_candidate_scripts(self) -> None:
        self.assertEqual(_block(CORE), _block(AUX))

    def test_both_scripts_start_relay_through_launcher_with_unchanged_arguments(self) -> None:
        for path in (CORE, AUX):
            source = path.read_text(encoding="utf-8")
            with self.subTest(script=path.name):
                self.assertIn('    launcher=$1\n    shift\n    python3 -c "$launcher" "$1" --cert "$2"', source)
                self.assertIn('\' sh "$candidate_relay_launcher" "$relay_tool" ', source)
                self.assertNotIn('    python3 "$1" --cert "$2"', source)

    def test_launcher_runs_relay_main_without_touching_relay_file(self) -> None:
        before = hashlib.sha256(RELAY.read_bytes()).hexdigest()
        result = subprocess.run(
            [sys.executable, "-c", _launcher_code(CORE), str(RELAY), "--help"],
            capture_output=True, text=True, timeout=60, check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        self.assertIn("--synthetic-profile", result.stdout)
        self.assertIn('{"candidate_relay_extensions": []}', result.stderr)
        self.assertEqual(hashlib.sha256(RELAY.read_bytes()).hexdigest(), before)

    def test_launcher_never_writes_bytecode_next_to_relay(self) -> None:
        """按模块加载会写 __pycache__（直接当脚本运行不会）；即使环境没设禁写，受管工具目录也不得出现缓存。"""

        import shutil
        import tempfile

        with tempfile.TemporaryDirectory(prefix="relay-launcher-") as tmp:
            copy = Path(tmp) / "upstream_byte_relay.py"
            shutil.copyfile(RELAY, copy)
            env = {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}
            result = subprocess.run(
                [sys.executable, "-c", _launcher_code(CORE), str(copy), "--help"],
                capture_output=True, text=True, timeout=60, check=False, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-1000:])
            self.assertFalse((Path(tmp) / "__pycache__").exists())


class CoreCookiePrimeExtensionTest(unittest.TestCase):
    def test_a05_first_http_post_gets_cookie_prime_on_0157(self) -> None:
        relay = _patched_relay("candidate-core-v1", "0.157.0", "A05")
        prime = relay._synthetic_core_response("A05", "chatgpt.com", PRIME, HEAD, b"", 1, "0.157.0")
        self.assertIsNotNone(prime)
        self.assertEqual(prime.action, "responses_http_success")
        self.assertEqual(prime.set_cookie_names, ("_cfuvid",))
        self.assertIn(b"set-cookie: " + relay._SYNTHETIC_CORE_CFUV_COOKIE.encode("ascii"), prime.wire)
        self.assertIn(b"content-type: text/event-stream", prime.wire)
        self.assertIn(b"resp_candidate_core_a05_cookie_prime", prime.wire)
        # 第二次 HTTP POST、非 chatgpt.com 主机一律拒绝（失败关闭）。
        self.assertIsNone(relay._synthetic_core_response("A05", "chatgpt.com", PRIME, HEAD, b"", 2, "0.157.0"))
        self.assertIsNone(relay._synthetic_core_response("A05", "evil.example", PRIME, HEAD, b"", 1, "0.157.0"))

    def test_other_requests_and_scenarios_keep_original_dispatch(self) -> None:
        patched = _patched_relay("candidate-core-v1", "0.157.0", "A05")
        pristine = _pristine_relay()
        cases = [
            ("A03", PRIME, 1),
            ("A03", PRIME, 3),
            ("A06", PRIME, 1),
            ("A05", "GET /backend-api/codex/models?client_version=0.157.0 HTTP/1.1", 0),
        ]
        for scenario, line, ordinal in cases:
            with self.subTest(scenario=scenario, line=line, ordinal=ordinal):
                ours = patched._synthetic_core_response(scenario, "chatgpt.com", line, HEAD, b"", ordinal, "0.157.0")
                theirs = pristine._synthetic_core_response(scenario, "chatgpt.com", line, HEAD, b"", ordinal, "0.157.0")
                self.assertEqual(_fields(ours), _fields(theirs))

    def test_extension_is_version_and_scenario_gated(self) -> None:
        for version, scenario in (("0.154.0", "A05"), ("0.156.1", "A05"), ("0.157.0", "A06"), ("0.157.0", "A03")):
            with self.subTest(version=version, scenario=scenario):
                relay = _patched_relay("candidate-core-v1", version, scenario)
                self.assertIsNone(relay._synthetic_core_response("A05", "chatgpt.com", PRIME, HEAD, b"", 1, version))


class AuxCookiePrimeExtensionTest(unittest.TestCase):
    def test_first_responses_post_gets_cookie_prime_from_0156_1(self) -> None:
        for version in ("0.156.1", "0.157.0"):
            with self.subTest(version=version):
                relay = _patched_relay("candidate-aux-v1", version)
                prime = relay._synthetic_aux_response("chatgpt.com", PRIME, HEAD, b"", version)
                self.assertIsNotNone(prime)
                self.assertEqual(prime.action, "responses_cookie_prime")
                self.assertIn(b"set-cookie: " + relay._SYNTHETIC_AUX_CFUV_COOKIE.encode("ascii"), prime.wire)
                self.assertIn(b"resp_candidate_aux_cookie_prime", prime.wire)
                self.assertIsNone(relay._synthetic_aux_response("chatgpt.com", PRIME, HEAD, b"", version))

    def test_aux_extension_is_version_gated_and_keeps_other_endpoints(self) -> None:
        old = _patched_relay("candidate-aux-v1", "0.154.0")
        self.assertIsNone(old._synthetic_aux_response("chatgpt.com", PRIME, HEAD, b"", "0.154.0"))
        patched = _patched_relay("candidate-aux-v1", "0.157.0")
        pristine = _pristine_relay()
        lines = (
            "GET /backend-api/codex/models?client_version=0.157.0 HTTP/1.1",
            "POST /backend-api/codex/alpha/search HTTP/1.1",
            "POST /backend-api/codex/images/generations HTTP/1.1",
            "POST /backend-api/codex/images/edits HTTP/1.1",
            "POST /backend-api/codex/responses/compact HTTP/1.1",
        )
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(
                    _fields(patched._synthetic_aux_response("chatgpt.com", line, HEAD, b"", "0.157.0", 1)),
                    _fields(pristine._synthetic_aux_response("chatgpt.com", line, HEAD, b"", "0.157.0", 1)),
                )

    def test_core_profile_does_not_enable_aux_prime(self) -> None:
        relay = _patched_relay("candidate-core-v1", "0.157.0", "A05")
        self.assertIsNone(relay._synthetic_aux_response("chatgpt.com", PRIME, HEAD, b"", "0.157.0"))


class CoreA05CookiePrimeScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CORE.read_text(encoding="utf-8")

    def test_gate_is_0157_and_later(self) -> None:
        line = next(item for item in self.source.splitlines() if item.startswith("target_ws_cookie_prime=$("))
        code = line[line.index("'") + 1:line.rindex("'")]
        for version, expected in (("0.154.0", "0"), ("0.156.1", "0"), ("0.157.0", "1"), ("0.158.0", "1")):
            with self.subTest(version=version):
                result = subprocess.run([sys.executable, "-c", code, version], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), expected)

    def _run_a05_a06(self, version: str) -> list[list[str]]:
        """桩环境真实执行 A05～A06 段（含版本开关行），返回按序记录的调用。"""

        import tempfile

        gate = next(line for line in self.source.splitlines() if line.startswith("target_ws_cookie_prime=$("))
        start = self.source.index("start_capture A05\n")
        end = self.source.index("stop_capture\n", self.source.index("start_capture A06\n")) + len("stop_capture\n")
        harness = r"""
set -e -o pipefail
codex_version=$1
LOG=$2
work_dir=$3
main_model=main-model
lite_model=lite-model
exec_ua=codex-exec-ua
gateway_driver_ua=driver-ua
gateway_driver_originator=driver-originator
session_id=11111111-1111-4111-8111-111111111111
cookie_prime_session_id=44444444-4444-4444-8444-444444444444
record() { local IFS=$'\t'; printf '%s\n' "$*" >>"$LOG"; }
start_capture() { record start_capture "$1"; }
stop_capture() { record stop_capture; }
wait_action() { record wait_action "$@"; }
restart_service() { record restart_service; }
set_account_features() { record set_account_features "$@"; }
write_request_body() { record write_request_body "$(basename "$1")" "$2" "$3" "$4" "${CANDIDATE_BODY_SESSION_ID:-default}"; }
run_response_request() { record run_response_request "$1" "$2" "$4" "$5" "session=$session_id"; }
prepare_a06_bodies() { record prepare_a06_bodies; }
run_response_ws_session() { record run_response_ws_session "session=$session_id"; }
extract_response_id() {
  case "$1" in
    *first.sse) printf resp_candidate_core_a06_0002 ;;
    *) printf resp_candidate_core_a06_0003 ;;
  esac
}
"""
        with tempfile.TemporaryDirectory(prefix="a05-harness-") as tmp:
            log = Path(tmp) / "calls.tsv"
            log.touch()
            result = subprocess.run(
                ["bash", "-c", harness + gate + "\n" + self.source[start:end], "a05-harness", version, str(log), tmp],
                capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            return [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]

    def test_a05_primes_through_official_entry_before_websocket_turns(self) -> None:
        for version in ("0.157.0", "0.158.0"):
            with self.subTest(version=version):
                calls = self._run_a05_a06(version)
                a05 = calls[:calls.index(["stop_capture"])]
                self.assertEqual(a05[0], ["start_capture", "A05"])
                self.assertEqual(
                    a05[1:4],
                    [
                        ["write_request_body", "cookie-prime.json", "main-model", "non_lite", "a05-cookie-prime",
                         "44444444-4444-4444-8444-444444444444"],
                        ["run_response_request", "A05", "cookie-prime", "codex-exec-ua", "codex_exec",
                         "session=44444444-4444-4444-8444-444444444444"],
                        ["wait_action", "A05", "responses_http_success"],
                    ],
                )
                turns = [call for call in a05 if call[:3] in (["run_response_request", "A05", "turn-1"], ["run_response_request", "A05", "turn-2"])]
                self.assertEqual(len(turns), 2)
                self.assertTrue(all(call[3:] == ["driver-ua", "driver-originator", "session=11111111-1111-4111-8111-111111111111"] for call in turns))
                self.assertEqual(sum(1 for call in calls if call[0] == "run_response_request" and call[2] == "cookie-prime"), 1)
                # A05 与 A06 之间不得重启网关或改账号特性，否则 A06 丢失预热建立的 jar。
                self.assertFalse([call for call in calls if call[0] in {"restart_service", "set_account_features"}])
                self.assertIn(["run_response_ws_session", "session=11111111-1111-4111-8111-111111111111"], calls)

    def test_older_targets_do_not_prime(self) -> None:
        for version in ("0.154.0", "0.156.1"):
            with self.subTest(version=version):
                calls = self._run_a05_a06(version)
                self.assertFalse([call for call in calls if "cookie-prime" in call or "cookie-prime.json" in call])
                self.assertEqual(calls[1][:3], ["write_request_body", "lite.json", "lite-model"])

    def test_body_session_override_is_explicit_only(self) -> None:
        self.assertIn(
            'session_id = os.environ.get("CANDIDATE_BODY_SESSION_ID") or "11111111-1111-4111-8111-111111111111"',
            self.source,
        )
        self.assertEqual(self.source.count("CANDIDATE_BODY_SESSION_ID"), 3)

    def test_closeout_verifies_cookie_prerequisite(self) -> None:
        for needle in (
            'minimums["A05"] = {**minimums["A05"], "responses_http_success": 1}',
            "A05 Cookie 预热请求的冷 jar 意外非空",
            "A05 Cookie 预热响应未保留已脱敏 Set-Cookie 证据",
            "A05／A06 WS 握手未回放 Cookie 预热建立的 jar",
            "公开 relay 产物泄漏 _cfuvid Cookie 名或值",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.source)


class A05LabelSequenceTest(unittest.TestCase):
    """A05 由网关驱动：启动期 models 由网关自行发起，预热与两轮 WS 由脚本驱动。"""

    SEQUENCES = {
        "old": [("models", None), ("websocket", "default"), ("websocket", "default")],
        "new": [("models", None), ("prime", None), ("websocket", "default"), ("websocket", "default")],
    }

    def test_every_declaration_labels_a05_in_capture_order(self) -> None:
        for path in sorted(TOOL_ROOT.glob("codex_upgrade_evidence_labels_*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            version = tuple(map(int, document["codex_version"].split(".")))
            entry = next(item for item in document["entries"] if item["job_id"] == "candidate-frozen-core")
            rules = [
                rule for rule in entry["rules"]
                if rule["glob"].startswith("scenarios/A05/relay/") and rule["glob"].endswith(".client_to_upstream.bin")
            ]
            sequence = self.SEQUENCES["new" if version >= (0, 157, 0) else "old"]
            with self.subTest(declaration=path.name):
                for index, (kind, variant) in enumerate(sequence, start=1):
                    name = f"scenarios/A05/relay/conn{index:03d}.client_to_upstream.bin"
                    hits = [rule for rule in rules if fnmatch.fnmatch(name, rule["glob"])]
                    self.assertEqual(len(hits), 1, f"{name} 命中 {[rule['glob'] for rule in hits]}")
                    labels = hits[0]["labels"]
                    if kind == "websocket":
                        self.assertEqual((labels.get("transport"), labels.get("variant")), ("websocket", variant))
                    elif kind == "prime":
                        self.assertEqual(labels.get("transport"), "http")
                        self.assertEqual(labels.get("cookie_state"), "absent")
                        self.assertNotIn("variant", labels)
                    else:
                        self.assertEqual(labels.get("transport"), "http")
                        self.assertNotIn("variant", labels)
                beyond = [f"scenarios/A05/relay/conn{index:03d}.client_to_upstream.bin" for index in range(len(sequence) + 1, 100)]
                for rule in rules:
                    self.assertFalse([name for name in beyond if fnmatch.fnmatch(name, rule["glob"])], rule["glob"])


if __name__ == "__main__":
    unittest.main()
