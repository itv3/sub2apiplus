"""中继脚本的 daemon-tui 场景与 daemon 残留清扫（0.157.0 起 TUI 默认经常驻 daemon 发请求）。

锁定四件事：
- daemon-tui 只接受 >=0.157.0，且在任何请求之前拒绝；
- 每个作业在启动中继、劫持 hosts 之前清扫 daemon 残留，清扫失败即退出；
- cleanup 先停 daemon 再还原 hosts／CA（还原后驻留 daemon 的定时刷新会直连真实上游），最后删 home；
- 场景分支不带任何 CLI 覆盖、经独立 CODEX_HOME 启动 TUI，模式判定失败也先停 daemon 再失败退出；
  home 名取 run_id 的 SHA-256 前 16 位，daemon 控制 socket 路径与 run_id 长度无关、不超过 Linux 上限。
分支控制流用假 docker 真实执行脚本原文，不另写副本。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.official_client_capture import drive_codex_daemon as lifecycle

TOOL_ROOT = Path(__file__).parents[1]
RELAY_SCRIPT = TOOL_ROOT / "run_official_relay_scenario.sh"
BASH = shutil.which("bash") or "/bin/bash"
BRANCH_START = 'elif [[ $prompt == "__DAEMON_TUI__" ]]; then'
BRANCH_END = 'elif [[ $prompt == "__MEMGEN__" ]]; then'
HOME_ASSIGNMENT = 'daemon_home="/root/.codex-daemon-$(printf \'%s\' "$run_id" | sha256sum | cut -c1-16)"'
# 2026-09-26 正式 Campaign 的 daemon 作业 run_id：旧命名 /root/.codex-daemon-<run_id> 让控制 socket 路径 120 字节。
FORMAL_RUN_ID = "c01570-formal-vc1-r2-20260926t062738z-official-tui-daemon"

FAKE_DOCKER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    assert args[0] == "exec", args
    args = args[1:]
    env = []
    while args[0] == "-e":
        env.append(args[1])
        args = args[2:]
    container, command = args[0], args[1:]
    with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"env": env, "container": container, "command": command}, ensure_ascii=False) + "\\n")
    tool = command[1].rsplit("/", 1)[-1] if len(command) > 1 else ""
    if tool == "drive_codex_daemon.py":
        sub = command[2]
        code = int(os.environ.get(f"FAKE_{sub.upper()}_CODE", "0"))
        payload = {"schema_version": "codex-daemon-lifecycle/v1", "command": sub,
                   "status": "passed" if code == 0 else "failed"}
        if sub == "status":
            payload["mode"] = "daemon" if code == 0 else "embedded"
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(code)
    if tool == "drive_codex_tui.py":
        print("drive finished")
        sys.exit(0)
    sys.exit(97)
    """
)


def expected_home(run_id: str) -> str:
    return "/root/.codex-daemon-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]


UNIT_HOME = expected_home("unit-run")


def function_source(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    return source[start : source.index("\n}\n", start) + 3]


class DaemonScenarioWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = RELAY_SCRIPT.read_text(encoding="utf-8")

    def run_until_validation(self, **environment: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "RUN_ID": "unit-daemon-tui",
            "SCENARIO": "daemon-tui",
            "CAPTURE_HOST_DATA_ROOT": "/nonexistent-capture-root",
            **environment,
        }
        return subprocess.run([BASH, str(RELAY_SCRIPT)], env=env, text=True, capture_output=True, check=False)

    def test_早于_0157_的版本在任何请求前以_2_退出(self) -> None:
        for version in ("0.156.1", "0.154.0"):
            with self.subTest(version=version):
                result = self.run_until_validation(CODEX_VERSION=version)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("daemon-tui 需要 Codex >=0.157.0", result.stderr)
        result = self.run_until_validation(CODEX_VERSION="0.157.0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("CAPTURE_HOST_DATA_ROOT 必须是可信的非根绝对目录", result.stderr)
        self.assertNotIn("daemon-tui 需要", result.stderr)

    def test_清扫位于启动中继与劫持_hosts_之前且失败即退出(self) -> None:
        sweep = self.source.index("if ! daemon_tool sweep; then")
        self.assertLess(self.source.index("trap cleanup EXIT INT TERM"), sweep)
        # 运行目录先建：失败作业的目录由编排器归档，清扫失败也留得下日志位置。
        self.assertLess(self.source.index('install -d -m 0700 "$work_dir" "$tls_dir"'), sweep)
        self.assertLess(sweep, self.source.index('openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/relay.key"'))
        self.assertLess(sweep, self.source.index('"grep -v \\" $h\\$\\" /etc/hosts > /tmp/.pre'))
        self.assertLess(sweep, self.source.index('"$capture_tool_root/upstream_byte_relay.py"'))
        self.assertLess(sweep, self.source.index("printf '127.0.0.1 $h\\n' >> /etc/hosts"))
        block = self.source[sweep : self.source.index("\nfi\n", sweep)]
        self.assertIn("exit 1", block)

    def test_cleanup_先停_daemon_再还原_hosts_最后删_home(self) -> None:
        cleanup = function_source(self.source, "cleanup")
        stop = cleanup.index('daemon_tool stop --home "$daemon_home" --codex-bin "$codex_bin"')
        self.assertLess(cleanup.index("set +e"), stop)
        self.assertLess(stop, cleanup.index('grep -v \\" $h\\$\\" /etc/hosts'))
        self.assertLess(stop, cleanup.index("update-ca-certificates --fresh"))
        # 注释里也出现 stop_relay 字样，按调用行定位。
        relay_call = cleanup.index("\n  stop_relay\n")
        self.assertLess(stop, relay_call)
        self.assertIn('daemon_tool remove --home "$daemon_home"', cleanup)
        self.assertLess(relay_call, cleanup.index('daemon_tool remove --home "$daemon_home"'))

    def test_daemon_工具在容器内以受管路径执行(self) -> None:
        tool = function_source(self.source, "daemon_tool")
        self.assertIn(
            'docker exec "$capture_container" python3 "$capture_tool_root/drive_codex_daemon.py" "$@"', tool
        )

    def test_场景分支不带任何_CLI_覆盖并经独立_home_启动(self) -> None:
        branch = self.source[self.source.index(BRANCH_START) : self.source.index(BRANCH_END)]
        self.assertIn(HOME_ASSIGNMENT, branch)
        self.assertLess(branch.index(HOME_ASSIGNMENT), branch.index("daemon_tool prepare"))
        self.assertIn('--disable-features "$DISABLE_FEATURES"', branch)
        drive = branch[branch.index('docker exec -e CODEX_HOME="$daemon_home"') : branch.index("--log ")]
        for forbidden in ("--disable", "--enable", "--config", "-c ", "TUI_ENABLE", "TUI_DISABLE"):
            self.assertNotIn(forbidden, drive)
        self.assertIn(
            '--require-mode daemon --expect-version "$codex_version"',
            branch,
        )
        self.assertLess(branch.index("daemon_tool status"), branch.index("daemon_tool stop"))
        self.assertIn("daemon-tui)", self.source)
        self.assertIn("prompt='__DAEMON_TUI__' ;;", self.source)
        # 场景分支位于主流程 stop_relay 之前：daemon 在中继仍在时停止。
        self.assertLess(self.source.index(BRANCH_START), self.source.index("\nstop_relay\n"))


class DaemonScenarioBranchTest(unittest.TestCase):
    """用假 docker 执行脚本里真实的 daemon 分支。"""

    @classmethod
    def setUpClass(cls) -> None:
        source = RELAY_SCRIPT.read_text(encoding="utf-8")
        cls.branch = source[source.index(BRANCH_START) : source.index(BRANCH_END)]
        cls.functions = function_source(source, "write_observation") + function_source(source, "daemon_tool")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o755)
        self.log = self.tmp / "docker.log"
        self.observations = self.tmp / "work" / "scenario-observations"

    def run_branch(self, run_id: str = "unit-run", **codes: str) -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                "set -Eeuo pipefail",
                f"run_id={run_id}",
                "capture_container=capture-cli",
                "capture_tool_root=/root/oauth-capture/tools/official_client_capture",
                "codex_bin=/opt/codex-0.157.0/bin/codex",
                "codex_version=0.157.0",
                "model=gpt-5.5",
                "DISABLE_FEATURES='plugins apps'",
                f"observation_dir={str(self.observations)!r}",
                "prompt=__DAEMON_TUI__",
                self.functions,
                "if false; then :",
                self.branch,
                "fi",
                'echo "BRANCH_DONE daemon_home=$daemon_home"',
            ]
        )
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "FAKE_DOCKER_LOG": str(self.log),
            **codes,
        }
        return subprocess.run([BASH, "-c", script], env=env, text=True, capture_output=True, check=False)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def steps(self) -> list[str]:
        result = []
        for call in self.calls():
            tool = call["command"][1].rsplit("/", 1)[-1]
            result.append(call["command"][2] if tool == "drive_codex_daemon.py" else tool)
        return result

    def test_成功时依次建_home_驱动_判定_停止并留三份观测(self) -> None:
        result = self.run_branch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"BRANCH_DONE daemon_home={UNIT_HOME}", result.stdout)
        self.assertEqual(self.steps(), ["prepare", "drive_codex_tui.py", "status", "stop"])
        calls = self.calls()
        self.assertEqual(
            calls[0]["command"][2:],
            ["prepare", "--home", UNIT_HOME, "--disable-features", "plugins apps"],
        )
        drive = calls[1]
        self.assertEqual(drive["env"], [f"CODEX_HOME={UNIT_HOME}"])
        self.assertFalse({"--disable", "--enable", "--config"} & set(drive["command"]))
        self.assertEqual(
            calls[2]["command"][2:],
            [
                "status", "--home", UNIT_HOME, "--codex-bin", "/opt/codex-0.157.0/bin/codex",
                "--require-mode", "daemon", "--expect-version", "0.157.0",
            ],
        )
        for name in ("daemon-home.json", "daemon-mode.json", "daemon-lifecycle.json"):
            self.assertEqual(json.loads((self.observations / name).read_text())["status"], "passed", name)

    def test_模式不符时仍先停_daemon_再失败(self) -> None:
        result = self.run_branch(FAKE_STATUS_CODE="3")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.steps(), ["prepare", "drive_codex_tui.py", "status", "stop"])
        self.assertIn("本作业要求 TUI 经 daemon 发请求", result.stderr)
        self.assertEqual(json.loads((self.observations / "daemon-mode.json").read_text())["mode"], "embedded")
        self.assertTrue((self.observations / "daemon-lifecycle.json").exists())

    def test_停止失败即作业失败(self) -> None:
        result = self.run_branch(FAKE_STOP_CODE="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("daemon 未能在停中继之前干净停止", result.stderr)

    def test_home_建立失败时不启动_TUI(self) -> None:
        result = self.run_branch(FAKE_PREPARE_CODE="3")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.steps(), ["prepare"])
        self.assertIn("独立 CODEX_HOME 建立失败", result.stderr)

    def test_正式_run_id_的_home_为定长摘要名且控制_socket_不超限(self) -> None:
        socket = "/" + str(lifecycle.CONTROL_SOCKET_RELATIVE)
        legacy = f"/root/.codex-daemon-{FORMAL_RUN_ID}"
        # 反例：旧命名超出 Linux 上限，prepare 的兜底核对拒绝它。
        self.assertEqual(len((legacy + socket).encode("utf-8")), 120)
        with self.assertRaises(ValueError):
            lifecycle.validate_socket_path(Path(legacy))
        result = self.run_branch(run_id=FORMAL_RUN_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        home = expected_home(FORMAL_RUN_ID)
        self.assertIn(f"BRANCH_DONE daemon_home={home}", result.stdout)
        self.assertEqual(self.calls()[0]["command"][2:4], ["prepare", "--home"])
        self.assertEqual(self.calls()[0]["command"][4], home)
        self.assertEqual(len((home + socket).encode("utf-8")), 79)
        self.assertEqual(lifecycle.validate_socket_path(Path(home)), Path(home + socket))
        self.assertNotEqual(home, UNIT_HOME)

    def test_摘要命令输出异常时不建_home_即失败(self) -> None:
        fake = self.bin / "sha256sum"
        fake.write_text("#!/bin/sh\ncat >/dev/null\necho 'not-a-digest  -'\n", encoding="utf-8")
        fake.chmod(0o755)
        result = self.run_branch()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.steps(), [])
        self.assertIn("daemon 作业的 home 名计算失败", result.stderr)


if __name__ == "__main__":
    unittest.main()
