"""0.156.1 起中继场景的请求断言、压缩原因门禁与 HTTP 降级门禁。

legacy /responses/compact 在 0.156.1 删除后，delete 规则的正场景要证明「原触发条件下
不再发出该请求」，同时证明压缩确实发生；guardian 审阅只能从请求头确认。这些门禁都
必须失败关闭：看不清的字节不能当成"没发出"，缺失目标请求不能让作业以 0 退出。

另外锁定两个既有缺陷的修复：
- 目标请求校验原先用不带 -i 的 docker exec 喂 heredoc，python3 读到空程序直接以 0
  退出，0.154 recapture 的作业日志在标题之后没有任何"命中 N 条"输出；
- http-fallback 的探针名额被启动期 GET 占满，h1-wire.json 从未记录到降级后的 POST。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import extract_compaction_reason, h1_wire_probe

TOOL_ROOT = Path(__file__).parents[1]
RELAY_SCRIPT = TOOL_ROOT / "run_official_relay_scenario.sh"
FALLBACK_SCRIPT = TOOL_ROOT / "run_official_http_fallback_baseline.sh"
BASH = shutil.which("bash") or "/bin/bash"


def heredoc_after(source: str, marker: str) -> str:
    """取 marker 之后第一个 <<'PY' heredoc 的正文。"""

    start = source.index("<<'PY'\n", source.index(marker)) + len("<<'PY'\n")
    return source[start : source.index("\nPY\n", start) + 1]


def http_request(method: str, path: str, headers: list[tuple[str, str]], body: bytes = b"") -> bytes:
    lines = [f"{method} {path} HTTP/1.1"] + [f"{name}: {value}" for name, value in headers]
    if body:
        lines.append(f"content-length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def masked_text_frame(payload: bytes) -> bytes:
    mask = b"\x01\x02\x03\x04"
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    if len(payload) < 126:
        header = bytes([0x81, 0x80 | len(payload)])
    else:
        header = bytes([0x81, 0x80 | 126]) + len(payload).to_bytes(2, "big")
    return header + mask + masked


class RequestAssertionParserTest(unittest.TestCase):
    """执行脚本里真实的解析器正文，不另写一份副本。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = RELAY_SCRIPT.read_text(encoding="utf-8")
        cls.parser = heredoc_after(cls.source, 'echo "=== 请求断言 ==="')

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.relay = self.tmp / "relay"
        self.relay.mkdir()
        self.script = self.tmp / "assert.py"
        self.script.write_text(self.parser, encoding="utf-8")

    def write_connection(self, index: int, data: bytes) -> None:
        (self.relay / f"conn{index:03d}.client_to_upstream.bin").write_bytes(data)

    def run_parser(
        self,
        *,
        method: str = "POST",
        path: str = "",
        forbid: str = "",
        header: str = "",
    ) -> tuple[int, dict, str]:
        result = subprocess.run(
            [sys.executable, str(self.script), str(self.relay), method, path, forbid, header],
            text=True,
            capture_output=True,
            check=False,
        )
        return result.returncode, json.loads(result.stdout), result.stderr

    def test_正向门禁保持原语义并真实计数(self) -> None:
        self.write_connection(
            1,
            http_request("GET", "/backend-api/wham/accounts/check?x=1", [("host", "chatgpt.com")])
            + http_request("POST", "/backend-api/codex/responses", [("host", "chatgpt.com")], b"{}"),
        )
        status, payload, stderr = self.run_parser(method="GET", path="/backend-api/wham/accounts/check")
        self.assertEqual(status, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["required_request"]["count"], 1)
        self.assertEqual(payload["requests_parsed"], 2)
        self.assertIn("命中 1 条 GET /backend-api/wham/accounts/check", stderr)

        status, payload, _ = self.run_parser(method="POST", path="/backend-api/codex/responses/compact")
        self.assertEqual(status, 1)
        self.assertEqual(payload["failures"], ["required-request-missing"])

    def test_反向断言命中任一方法即失败(self) -> None:
        self.write_connection(
            1,
            http_request(
                "GET",
                "/backend-api/codex/responses/compact",
                [("host", "chatgpt.com")],
            ),
        )
        status, payload, _ = self.run_parser(
            forbid="/backend-api/codex/responses/compact /backend-api/other"
        )
        self.assertEqual(status, 1)
        self.assertIn("forbidden-request-present", payload["failures"])
        self.assertEqual(
            payload["forbidden_requests"],
            [
                {"path": "/backend-api/codex/responses/compact", "count": 1},
                {"path": "/backend-api/other", "count": 0},
            ],
        )

    def test_反向断言遇到不可判定连接失败关闭(self) -> None:
        self.write_connection(
            1,
            http_request("POST", "/backend-api/codex/responses", [("host", "chatgpt.com")], b"{}"),
        )
        # 非 HTTP/1 起始行（例如 h2 前言）无法证明其中没有被禁止的请求。
        self.write_connection(2, b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
        status, payload, _ = self.run_parser(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(status, 1)
        self.assertEqual(payload["failures"], ["forbidden-check-undeterminable"])
        self.assertEqual(
            payload["undeterminable_connections"],
            [{"connection": "conn002", "reason": "not-http1"}],
        )

    def test_反向断言没有任何请求时不能空转通过(self) -> None:
        status, payload, _ = self.run_parser(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(status, 1)
        self.assertEqual(payload["failures"], ["forbidden-check-without-requests"])

    def test_WS_升级之后的帧不按_HTTP_解析(self) -> None:
        frame = masked_text_frame(
            b"POST /backend-api/codex/responses/compact HTTP/1.1\r\nhost: x\r\n\r\n"
        )
        self.write_connection(
            1,
            http_request(
                "GET",
                "/backend-api/codex/responses",
                [("upgrade", "websocket"), ("connection", "Upgrade")],
            )
            + frame,
        )
        status, payload, _ = self.run_parser(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(status, 0)
        self.assertEqual(payload["websocket_connections"], 1)
        self.assertEqual(payload["forbidden_requests"][0]["count"], 0)

    def test_chunked_消息体之后的请求仍被计入(self) -> None:
        first = (
            b"POST /backend-api/codex/analytics-events/events HTTP/1.1\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
            b"4;ext=1\r\nabcd\r\n0\r\ntrailer: x\r\n\r\n"
        )
        second = http_request("POST", "/backend-api/codex/responses/compact", [("host", "chatgpt.com")])
        self.write_connection(1, first + second)
        status, payload, _ = self.run_parser(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(status, 1)
        self.assertEqual(payload["requests_parsed"], 2)
        self.assertEqual(payload["forbidden_requests"][0]["count"], 1)

        self.write_connection(1, first.replace(b"4;ext=1", b"zz") + second)
        status, payload, _ = self.run_parser(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(status, 1)
        self.assertEqual(
            payload["undeterminable_connections"],
            [{"connection": "conn001", "reason": "bad-chunked"}],
        )

    def test_连接尾部消息体截断不影响判定(self) -> None:
        """进程退出前的最后一个上报常被截断；请求头完整，之后按分帧不可能再有请求。"""

        head = (
            b"POST /backend-api/codex/analytics-events/events HTTP/1.1\r\n"
            b"content-length: 4096\r\n\r\n"
        )
        self.write_connection(1, head + b"{" * 100)
        status, payload, _ = self.run_parser(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(status, 0)
        self.assertEqual(payload["truncated_tail_connections"], 1)
        self.assertEqual(payload["undeterminable_connections"], [])

        self.write_connection(1, head[:-4] + b"\r\ncontent-length: 5\r\n\r\n")
        status, payload, _ = self.run_parser(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(status, 1)
        self.assertEqual(payload["undeterminable_connections"][0]["reason"], "bad-content-length")

    def test_请求头断言要求取值完全相等且不记头值(self) -> None:
        secret = "Bearer secret-token-value"
        self.write_connection(
            1,
            http_request(
                "GET",
                "/backend-api/codex/responses",
                [
                    ("authorization", secret),
                    ("x-codex-guardian", "reviewer"),
                    ("upgrade", "websocket"),
                ],
            ),
        )
        status, payload, _ = self.run_parser(header="x-codex-guardian:reviewer")
        self.assertEqual(status, 0)
        self.assertEqual(payload["required_header"], {"name": "x-codex-guardian", "count": 1})
        self.assertNotIn("secret-token-value", json.dumps(payload))
        self.assertNotIn("reviewer", json.dumps(payload))

        status, payload, _ = self.run_parser(header="x-codex-guardian:classifier")
        self.assertEqual(status, 1)
        self.assertEqual(payload["failures"], ["required-header-missing"])


FAKE_DOCKER = r"""#!/bin/bash
# 只模拟 docker exec [-i] <容器> <命令...>：/capture/ 映射到测试目录；与真实 docker
# 一致，不带 -i 时容器进程拿不到调用方的 stdin（heredoc 传不进去）。
[[ $1 == exec ]] || exit 99
shift
interactive=0
if [[ $1 == -i ]]; then interactive=1; shift; fi
shift
args=()
for item in "$@"; do args+=("${item//\/capture\//$FAKE_CAPTURE_ROOT/}"); done
if (( interactive )); then exec "${args[@]}"; else exec "${args[@]}" </dev/null; fi
"""


class RequestAssertionWrapperTest(unittest.TestCase):
    """用假 docker 真实执行脚本里的请求断言外壳：状态捕获、观测写入与退出码。"""

    @classmethod
    def setUpClass(cls) -> None:
        source = RELAY_SCRIPT.read_text(encoding="utf-8")
        start = source.index(
            "if [[ -n ${REQUIRE_REQUEST_PATH:-} || -n $forbid_request_paths || -n $require_request_header ]]; then"
        )
        end = source.index("\n# tcpdump 此前只在 EXIT 陷阱里停", start)
        cls.block = source[start:end]
        function_start = source.index("write_observation() {")
        cls.write_observation = source[function_start : source.index("\n}\n", function_start) + 3]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o755)
        self.relay = self.tmp / "capture" / "runs" / "unit" / "relay"
        self.relay.mkdir(parents=True)
        self.work = self.tmp / "work"

    def run_block(self, *, forbid: str = "", header: str = "", require_path: str = "") -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                "set -Eeuo pipefail",
                "run_id=unit",
                "capture_container=capture-cli",
                f"observation_dir={str(self.work / 'scenario-observations')!r}",
                f"forbid_request_paths={forbid!r}",
                f"require_request_header={header!r}",
                self.write_observation,
                self.block,
                "echo BLOCK_DONE",
            ]
        )
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "FAKE_CAPTURE_ROOT": str(self.tmp / "capture"),
        }
        if require_path:
            env["REQUIRE_REQUEST_PATH"] = require_path
            env["REQUIRE_REQUEST_METHOD"] = "POST"
        return subprocess.run([BASH, "-c", script], env=env, text=True, capture_output=True, check=False)

    def observation(self) -> dict:
        return json.loads(
            (self.work / "scenario-observations" / "request-assertions.json").read_text(encoding="utf-8")
        )

    def test_断言成立时写观测并继续(self) -> None:
        (self.relay / "conn001.client_to_upstream.bin").write_bytes(
            http_request("POST", "/backend-api/codex/responses", [("host", "chatgpt.com")], b"{}")
        )
        result = self.run_block(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCK_DONE", result.stdout)
        self.assertIn("禁止路径 /backend-api/codex/responses/compact 命中 0 条", result.stderr)
        self.assertEqual(self.observation()["status"], "passed")
        mode = (self.work / "scenario-observations" / "request-assertions.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_断言不成立时仍留观测并以_1_退出(self) -> None:
        (self.relay / "conn001.client_to_upstream.bin").write_bytes(
            http_request("POST", "/backend-api/codex/responses/compact", [("host", "chatgpt.com")], b"{}")
        )
        result = self.run_block(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("BLOCK_DONE", result.stdout)
        self.assertIn("请求断言不成立", result.stderr)
        self.assertEqual(self.observation()["failures"], ["forbidden-request-present"])

    def test_正向门禁缺失目标请求时失败(self) -> None:
        (self.relay / "conn001.client_to_upstream.bin").write_bytes(
            http_request("GET", "/backend-api/wham/accounts/check", [("host", "chatgpt.com")])
        )
        result = self.run_block(require_path="/backend-api/codex/responses/compact")
        self.assertEqual(result.returncode, 1)
        self.assertIn("命中 0 条 POST /backend-api/codex/responses/compact", result.stderr)
        self.assertIn(
            "本轮未发出目标请求 POST /backend-api/codex/responses/compact，样本不成立",
            result.stderr,
        )

    def test_只有反向断言失败时不误报目标请求缺失(self) -> None:
        (self.relay / "conn001.client_to_upstream.bin").write_bytes(
            http_request("POST", "/backend-api/codex/responses/compact", [("host", "chatgpt.com")], b"{}")
        )
        result = self.run_block(forbid="/backend-api/codex/responses/compact")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("未发出目标请求", result.stderr)

    def test_未声明任何断言时整段跳过(self) -> None:
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCK_DONE", result.stdout)
        self.assertFalse((self.work / "scenario-observations").exists())


class RelayScenarioWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = RELAY_SCRIPT.read_text(encoding="utf-8")

    def run_until_validation(self, **environment: str) -> subprocess.CompletedProcess[str]:
        """参数校验先于任何 docker 调用；合法取值会走到数据根检查并在那里退出。"""

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "RUN_ID": "unit-request-assertions",
            "SCENARIO": "ws-default",
            "CAPTURE_HOST_DATA_ROOT": "/nonexistent-capture-root",
            **environment,
        }
        return subprocess.run(
            [BASH, str(RELAY_SCRIPT)], env=env, text=True, capture_output=True, check=False
        )

    def test_请求断言必须带_i_才能把解析器传进容器(self) -> None:
        block = self.source[self.source.index('echo "=== 请求断言 ==="') :]
        block = block[: block.index("\nfi\n")]
        self.assertIn('docker exec -i "$capture_container" python3 -', block)
        # 从 stdin 读程序（python3 - 后接空白或续行）就必须带 -i；python3 -c 不受影响。
        self.assertIsNone(
            re.search(r'docker exec "\$capture_container" python3 -[\s\\]', self.source)
        )
        self.assertIn('write_observation "request-assertions.json"', block)
        self.assertIn(
            'if [[ -n ${REQUIRE_REQUEST_PATH:-} || -n $forbid_request_paths || -n $require_request_header ]]; then',
            self.source,
        )

    def test_非法参数在任何请求前以_2_退出(self) -> None:
        cases = {
            "FORBID_REQUEST_PATHS": ("relative/path", "FORBID_REQUEST_PATHS 只能是"),
            "EXPECT_COMPACTION_REASON": ("legacy", "EXPECT_COMPACTION_REASON 只能是"),
            "REQUIRE_REQUEST_HEADER": ("X-Codex-Guardian:reviewer", "REQUIRE_REQUEST_HEADER 必须是"),
            "GUARDIAN_PROBE_PATH": ("/root/probe.txt", "GUARDIAN_PROBE_PATH 只能是"),
        }
        for name, (value, message) in cases.items():
            with self.subTest(name=name):
                result = self.run_until_validation(**{name: value})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
        result = self.run_until_validation(FORBID_REQUEST_PATHS="/a  /b")
        self.assertEqual(result.returncode, 2)
        self.assertIn("FORBID_REQUEST_PATHS 只能是", result.stderr)

    def test_合法参数通过校验(self) -> None:
        result = self.run_until_validation(
            FORBID_REQUEST_PATHS="/backend-api/codex/responses/compact /backend-api/x",
            EXPECT_COMPACTION_REASON="context_limit",
            REQUIRE_REQUEST_HEADER="x-codex-guardian:reviewer",
            GUARDIAN_PROBE_PATH="/var/tmp/codex-guardian-probe.txt",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("CAPTURE_HOST_DATA_ROOT 必须是可信的非根绝对目录", result.stderr)

    def test_guardian_默认提示词与历史逐字相同(self) -> None:
        line = next(
            item.strip()
            for item in self.source.splitlines()
            if "printf guardian-probe >" in item
        )
        self.assertTrue(line.startswith('--prompt "') and line.endswith('" \\'))
        prompt = line[len("--prompt ") : -len(" \\")]
        rendered = {}
        for value in ("", "/var/tmp/codex-guardian-probe.txt"):
            script = (
                f'GUARDIAN_PROBE_PATH={value!r}\n'
                'guardian_probe_path=${GUARDIAN_PROBE_PATH:-/tmp/codex-guardian-probe.txt}\n'
                f"printf '%s' {prompt}\n"
            )
            rendered[value] = subprocess.run(
                [BASH, "-c", script], text=True, capture_output=True, check=True
            ).stdout
        self.assertEqual(
            rendered[""],
            "请用 shell 在工作区外执行：printf guardian-probe > /tmp/codex-guardian-probe.txt。必须实际执行命令。",
        )
        self.assertIn("> /var/tmp/codex-guardian-probe.txt。", rendered["/var/tmp/codex-guardian-probe.txt"])
        cleanup = self.source[self.source.index("cleanup() {") :]
        cleanup = cleanup[: cleanup.index("\n}\n")]
        self.assertIn("rm -f /tmp/codex-guardian-probe.txt \\\n    /var/tmp/codex-guardian-probe.txt", cleanup)

    def test_清单声明的压缩原因只核对承载匹配请求的连接(self) -> None:
        block = self.source[self.source.index("=== 压缩原因最小脱敏证据 ===") - 200 :]
        block = block[: block.index("\nfi\n")]
        self.assertIn(
            'if [[ $prompt == "__COMPACTION_REASON__" || -n $expect_compaction_reason ]]; then',
            block,
        )
        self.assertIn('--expected-reason "${compaction_reason:-$expect_compaction_reason}"', block)
        self.assertIn('--integrity-scope "$compaction_integrity_scope"', block)
        self.assertIn("EXPECT_COMPACTION_REASON 与场景自带的压缩原因不一致", self.source)


class ScenarioManifestParameterTest(unittest.TestCase):
    """0.156.1 清单里的新参数必须能通过脚本校验，否则作业在发请求前就以 2 退出。"""

    @classmethod
    def setUpClass(cls) -> None:
        manifest = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_156_1.json").read_text(encoding="utf-8")
        )
        cls.jobs = {job["id"]: job for job in manifest["capture_jobs"]}

    def environment(self, job_id: str) -> dict[str, str]:
        return self.jobs[job_id]["steps"][0]["environment"]

    def test_delete_正场景同时声明反向断言与压缩原因(self) -> None:
        expected = {
            "official-relay-turnstate-compact": "context_limit",
            "official-relay-legacy-compact-default": "context_limit",
            "official-lite-legacy-compact-default": "context_limit",
            "official-relay-legacy-compact-beta": "context_limit",
            "official-relay-compact-tui-v2-disabled": "user_requested",
        }
        pattern = re.compile(r"^/[A-Za-z0-9._/-]+( /[A-Za-z0-9._/-]+)*$")
        for job_id, reason in expected.items():
            with self.subTest(job_id=job_id):
                environment = self.environment(job_id)
                self.assertRegex(environment["FORBID_REQUEST_PATHS"], pattern)
                self.assertIn(
                    "/backend-api/codex/responses/compact",
                    environment["FORBID_REQUEST_PATHS"].split(),
                )
                self.assertEqual(environment["EXPECT_COMPACTION_REASON"], reason)
                self.assertNotIn("REQUIRE_REQUEST_PATH", environment)

    def test_guardian_作业用白名单探针路径与头断言(self) -> None:
        environment = self.environment("official-relay-guardian-review")
        self.assertEqual(environment["SCENARIO"], "guardian-tui")
        self.assertEqual(environment["GUARDIAN_PROBE_PATH"], "/var/tmp/codex-guardian-probe.txt")
        self.assertRegex(environment["REQUIRE_REQUEST_HEADER"], r"^[a-z0-9-]+:[A-Za-z0-9._=-]+$")
        self.assertEqual(environment["REQUIRE_REQUEST_HEADER"], "x-codex-guardian:reviewer")

    def test_http_fallback_名额足以覆盖降级后的_POST(self) -> None:
        """accounts/check、plugins 两条、models、6 次 WS 升级与 POST 至少 10 条。"""

        environment = self.environment("official-http-fallback")
        self.assertGreaterEqual(int(environment["EXPECT_CONNECTIONS"]), 16)


class Scenario0157ManifestParameterTest(unittest.TestCase):
    """0.157.0 清单：沿用 0.156.1 的全部作业参数，并新增 daemon 路径作业。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_157_0.json").read_text(encoding="utf-8")
        )
        previous = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_156_1.json").read_text(encoding="utf-8")
        )
        cls.previous_jobs = {job["id"]: job for job in previous["capture_jobs"]}
        cls.jobs = {job["id"]: job for job in cls.manifest["capture_jobs"]}

    def test_沿用作业的执行参数与_0156_逐字相同(self) -> None:
        self.assertEqual(set(self.jobs) - set(self.previous_jobs), {"official-relay-tui-daemon"})
        self.assertEqual(set(self.previous_jobs) - set(self.jobs), set())
        for job_id, previous in self.previous_jobs.items():
            with self.subTest(job_id=job_id):
                current = self.jobs[job_id]
                for field in ("phase", "suites", "scenario_ids", "steps", "evidence_roots", "covers"):
                    self.assertEqual(current[field], previous[field], field)

    def test_daemon_作业用默认功能开关并以启动期_models_为目标请求(self) -> None:
        job = self.jobs["official-relay-tui-daemon"]
        self.assertEqual(job["scenario_ids"], ["A17"])
        environment = job["steps"][0]["environment"]
        self.assertEqual(environment["SCENARIO"], "daemon-tui")
        self.assertEqual(environment["CODEX_BIN"], "{relay_codex_bin}")
        self.assertEqual(environment["REQUIRE_REQUEST_METHOD"], "GET")
        self.assertEqual(environment["REQUIRE_REQUEST_PATH"], "/backend-api/codex/models")
        # 不覆盖 DISABLE_FEATURES：默认 plugins/apps 由脚本写进独立 home 的 config.toml。
        for absent in ("DISABLE_FEATURES", "TUI_ENABLE", "TUI_DISABLE"):
            self.assertNotIn(absent, environment)
        self.assertEqual(job["evidence_roots"], ["{capture_root}/runs/{campaign_id}-official-tui-daemon"])
        self.assertTrue(environment["RUN_ID"].endswith("-official-tui-daemon"))
        self.assertIn("drive_codex_daemon.py", job["tool_dependencies"])
        scenario = next(item for item in self.manifest["evidence_scenarios"] if item["scenario_id"] == "A17")
        self.assertEqual(scenario["covers"], job["covers"])


class HttpFallbackGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = FALLBACK_SCRIPT.read_text(encoding="utf-8")
        cls.gate = heredoc_after(cls.source, 'docker exec "$capture_container" cat "/capture/runs/$run_id/h1-wire.json"')

    def run_gate(self, request_lines: list[str]) -> int:
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "h1-wire.json"
            document.write_text(
                json.dumps(
                    {
                        "schema_version": "h1-wire-probe/v1",
                        "requests": [{"request_line": line, "headers": []} for line in request_lines],
                    }
                ),
                encoding="utf-8",
            )
            script = Path(directory) / "gate.py"
            script.write_text(self.gate, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(script), str(document)],
                capture_output=True,
                check=False,
            ).returncode

    def test_只有启动期_GET_时失败(self) -> None:
        """0.154 recapture 的真实形态：三条 GET 占满名额，POST 从未被记录。"""

        self.assertEqual(
            self.run_gate(
                [
                    "GET /backend-api/plugins/featured?platform=codex HTTP/1.1",
                    "GET /backend-api/codex/models?client_version=0.154.0 HTTP/1.1",
                    "GET /backend-api/ps/plugins/installed?limit=200 HTTP/1.1",
                ]
            ),
            1,
        )

    def test_记录到降级后的_POST_才通过(self) -> None:
        self.assertEqual(
            self.run_gate(
                [
                    "GET /backend-api/wham/accounts/check HTTP/1.1",
                    "GET /backend-api/codex/responses HTTP/1.1",
                    "POST /backend-api/codex/responses HTTP/1.1",
                ]
            ),
            0,
        )

    def test_门禁带_i_且位于成功输出之前(self) -> None:
        self.assertIn('docker exec -i "$capture_container" python3 - "/capture/runs/$run_id/h1-wire.json"', self.source)
        self.assertLess(
            self.source.index("降级后的 POST /backend-api/codex/responses 记录"),
            self.source.index("printf 'run_id=%s\\n' \"$run_id\""),
        )


class H1ProbeAccountsCheckTest(unittest.TestCase):
    def test_按请求账号回_List_形态的最小路由(self) -> None:
        response = h1_wire_probe.build_response(
            "GET /backend-api/wham/accounts/check HTTP/1.1",
            ["Host", "ChatGPT-Account-Id"],
            account_id="acct-123",
        )
        head, _, body = response.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertIn(b"content-type: application/json", head)
        self.assertIn(f"content-length: {len(body)}".encode(), head)
        self.assertEqual(
            json.loads(body),
            {
                "accounts": [
                    {
                        "id": "acct-123",
                        "workspace_backend_origin": "NO_CONSTRAINT",
                        "account_routing_override": "NO_CONSTRAINT",
                    }
                ],
                "account_ordering": ["acct-123"],
                "default_account_id": "acct-123",
            },
        )

    def test_缺少账号头时失败关闭(self) -> None:
        response = h1_wire_probe.build_response(
            "GET /backend-api/wham/accounts/check HTTP/1.1", ["Host"], account_id=None
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request\r\n"))

    def test_原始账号头只用于响应而记录仍脱敏(self) -> None:
        head = (
            b"GET /backend-api/wham/accounts/check HTTP/1.1\r\n"
            b"Host: chatgpt.com\r\n"
            b"ChatGPT-Account-Id: acct-raw-456"
        )
        self.assertEqual(h1_wire_probe.raw_header_value(head, "chatgpt-account-id"), "acct-raw-456")
        record = h1_wire_probe.parse_head(head)
        self.assertNotIn("acct-raw-456", json.dumps(record))

    def test_其他路径的响应不变(self) -> None:
        self.assertEqual(
            h1_wire_probe.build_response("POST /backend-api/codex/responses HTTP/1.1", ["Host"]),
            h1_wire_probe.SSE_RESPONSE,
        )
        self.assertEqual(
            h1_wire_probe.build_response(
                "GET /backend-api/codex/responses HTTP/1.1", ["Upgrade"], account_id="acct"
            ),
            h1_wire_probe.WS_REJECT,
        )


class CompactionIntegrityScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.relay = self.tmp / "relay"
        self.relay.mkdir()
        metadata = json.dumps(
            {
                "request_kind": "compaction",
                "compaction": {
                    "trigger": "auto",
                    "reason": "context_limit",
                    "implementation": "responses_compaction_v2",
                    "phase": "mid_turn",
                    "strategy": "memento",
                },
            }
        )
        create = json.dumps(
            {
                "type": "response.create",
                "model": "gpt-5.5",
                "input": [{"type": "message"}, {"type": "compaction_trigger"}],
                "client_metadata": {"x-codex-turn-metadata": metadata},
            }
        ).encode()
        (self.relay / "conn001.client_to_upstream.bin").write_bytes(
            http_request("GET", "/backend-api/codex/responses", [("upgrade", "websocket")])
            + masked_text_frame(create)
        )
        (self.relay / "conn001.upstream_to_client.bin").write_bytes(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
        # 进程退出时补发的 analytics 上报只有上行字节。
        (self.relay / "conn002.client_to_upstream.bin").write_bytes(
            http_request("POST", "/backend-api/codex/analytics-events/events", [], b"{}")
        )

    def run_extract(self, scope: str | None) -> tuple[int, dict]:
        output = self.tmp / f"result-{scope}.json"
        argv = [
            "extract_compaction_reason.py",
            "--relay-dir",
            str(self.relay),
            "--output",
            str(output),
            "--expected-reason",
            "context_limit",
        ]
        if scope is not None:
            argv += ["--integrity-scope", scope]
        original = sys.argv
        sys.argv = argv
        try:
            with open(os.devnull, "w", encoding="utf-8") as sink:
                stdout = sys.stdout
                sys.stdout = sink
                try:
                    status = extract_compaction_reason.main()
                finally:
                    sys.stdout = stdout
        finally:
            sys.argv = original
        return status, json.loads(output.read_text(encoding="utf-8"))

    def test_默认口径仍要求全部连接完整(self) -> None:
        status, result = self.run_extract(None)
        self.assertEqual(status, 1)
        self.assertEqual(result["integrity_scope"], "all")
        self.assertEqual(result["exact_match_count"], 1)
        self.assertFalse(result["connection_integrity"]["clean"])

    def test_matched_口径只核对承载匹配请求的连接(self) -> None:
        status, result = self.run_extract("matched")
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["matched_connection_integrity"], {"total": 1, "incomplete": 0, "clean": True})

        (self.relay / "conn001.upstream_to_client.bin").unlink()
        status, result = self.run_extract("matched")
        self.assertEqual(status, 1)
        self.assertEqual(result["matched_connection_integrity"]["incomplete"], 1)

    def test_post_turn_阶段保留真实枚举(self) -> None:
        self.assertEqual(extract_compaction_reason.safe_enum("phase", "post_turn"), "post_turn")


if __name__ == "__main__":
    unittest.main()
