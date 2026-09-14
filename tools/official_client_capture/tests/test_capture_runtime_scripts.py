"""ARM64 受管抓包运行时的静态安全与装配契约。"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.capturelib.identity import (
    CAPTURE_SOURCE_RELATIVE_PATHS,
)


TOOL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = TOOL_ROOT / "runtime_scripts"
RUNTIME_NAMES = (
    "start_direct.sh",
    "stop_direct.sh",
    "start_mitm.sh",
    "stop_mitm.sh",
)


class CaptureRuntimeScriptsTest(unittest.TestCase):
    @staticmethod
    def _write_executable(path: Path, source: str) -> None:
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)

    def _wrapper_fixture(
        self,
        root: Path,
        script_name: str,
        *,
        host_identity: str,
        container_identity: str,
    ) -> tuple[subprocess.CompletedProcess[str], Path, str]:
        """用纯本地假命令验证 wrapper 的宿主／容器路径分界。"""

        host_root = root / "host-data"
        runs_root = host_root / "runs"
        runs_root.mkdir(parents=True, mode=0o700)
        fake_bin = root / "bin"
        fake_bin.mkdir(mode=0o700)
        command_log = root / "commands.log"
        self._write_executable(
            fake_bin / "stat",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$FAKE_HOST_IDENTITY\"\n",
        )
        self._write_executable(
            fake_bin / "docker",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_COMMAND_LOG\"\n"
            "if [[ $1 == exec && $3 == stat && $4 == -Lc ]]; then\n"
            "  printf '%s\\n' \"$FAKE_CONTAINER_IDENTITY\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 97\n",
        )
        self._write_executable(
            fake_bin / "openssl",
            "#!/usr/bin/env bash\n"
            "printf 'openssl %s\\n' \"$*\" >> \"$FAKE_COMMAND_LOG\"\n"
            "exit 97\n",
        )
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "CAPTURE_CONTAINER": "fixture-container",
            "CAPTURE_ROOT": "/container-capture",
            "CAPTURE_HOST_DATA_ROOT": str(host_root),
            "RUN_ID": "storage-fixture",
            "CODEX_VERSION": "0.154.0",
            "SCENARIO": "http-response",
            "FAKE_COMMAND_LOG": str(command_log),
            "FAKE_HOST_IDENTITY": host_identity,
            "FAKE_CONTAINER_IDENTITY": container_identity,
        }
        result = subprocess.run(
            ["bash", str(TOOL_ROOT / script_name)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        log = command_log.read_text(encoding="utf-8") if command_log.exists() else ""
        return result, runs_root / "storage-fixture", log

    def test_四个脚本可执行且通过_bash_语法检查(self) -> None:
        for name in RUNTIME_NAMES:
            with self.subTest(name=name):
                path = RUNTIME_ROOT / name
                self.assertTrue(path.is_file())
                self.assertTrue(path.stat().st_mode & 0o111)
                result = subprocess.run(
                    ["bash", "-n", str(path)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_四个脚本进入抓包执行源身份(self) -> None:
        for name in RUNTIME_NAMES:
            self.assertIn(
                f"runtime_scripts/{name}",
                CAPTURE_SOURCE_RELATIVE_PATHS,
            )

    def test_direct_只回收带身份状态的独占_sidecar(self) -> None:
        start = (RUNTIME_ROOT / "start_direct.sh").read_text(encoding="utf-8")
        stop = (RUNTIME_ROOT / "stop_direct.sh").read_text(encoding="utf-8")
        self.assertIn('--network "container:$source_container"', start)
        self.assertIn("sub2apiplus.capture.role=direct", start)
        self.assertIn("schema=direct-capture-state/v1", start)
        self.assertIn('docker logs "$sidecar_name"', start)
        self.assertIn('== *"listening on "*', start)
        self.assertNotIn("if [[ -e $output_dir/egress.pcap ]]; then", start)
        self.assertIn("actual_id", stop)
        self.assertIn("actual_role", stop)
        self.assertIn("actual_subject", stop)
        self.assertNotIn("pkill", start + stop)

    def test_mitm_校验进程启动时钟后才按进程组停止(self) -> None:
        start = (RUNTIME_ROOT / "start_mitm.sh").read_text(encoding="utf-8")
        stop = (RUNTIME_ROOT / "stop_mitm.sh").read_text(encoding="utf-8")
        self.assertIn("setsid env", start)
        self.assertIn("schema=mitm-capture-state/v1", start)
        self.assertIn("/proc/$pid/stat", stop)
        self.assertIn('actual_start_ticks != "$start_ticks"', stop)
        self.assertIn('kill -TERM -- "-$pgid"', stop)
        self.assertNotIn("pkill", start + stop)

    def test_mitm_后台启动前已创建日志并安装失败清理陷阱(self) -> None:
        """后台重定向不得与 chmod 竞态，也不能留下没有 state 的孤儿进程。"""

        start = (RUNTIME_ROOT / "start_mitm.sh").read_text(encoding="utf-8")
        create_log = 'install -m 0600 /dev/null "$log_path"'
        install_trap = "trap cleanup_failed_start EXIT ERR INT TERM"
        launch = "setsid env"
        self.assertIn(create_log, start)
        self.assertIn(install_trap, start)
        self.assertLess(start.index(create_log), start.index(launch))
        self.assertLess(start.index(install_trap), start.index(launch))
        self.assertNotIn('chmod 0600 "$log_path"', start)

    def test_宿主_wrapper_分离容器逻辑根与宿主可写根(self) -> None:
        """宿主脚本不得把容器只读父挂载误当成宿主输出路径。"""

        for name in (
            "run_official_codex_compact_capture.sh",
            "run_official_http_fallback_baseline.sh",
            "run_official_relay_scenario.sh",
        ):
            with self.subTest(name=name):
                source = (TOOL_ROOT / name).read_text(encoding="utf-8")
                self.assertIn("capture_host_data_root=${CAPTURE_HOST_DATA_ROOT:-", source)
                self.assertIn("host_runs_identity=$(stat -Lc '%d:%i'", source)
                self.assertIn(
                    'container_runs_identity=$(docker exec "$capture_container" stat -Lc',
                    source,
                )
                self.assertIn("宿主与容器 runs 根不同源", source)
                self.assertNotIn('work_dir="$capture_root/runs/$run_id"', source)

        relay = (TOOL_ROOT / "run_official_relay_scenario.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('[[ -e $work_dir || -L $work_dir ]]', relay)
        self.assertIn('--run-root "$container_work_dir"', relay)
        self.assertIn('python3 "$host_tool_root/build_scenario_facts.py"', relay)
        self.assertIn('pcap_dir="$work_dir/direct"', relay)
        self.assertIn(
            'observation_dir="$work_dir/scenario-observations"', relay
        )

    def test_宿主_wrapper_不同源时在任何请求前失败(self) -> None:
        """错误挂载必须在创建 run 根和启动客户端之前失败。"""

        scripts = (
            "run_official_codex_compact_capture.sh",
            "run_official_http_fallback_baseline.sh",
            "run_official_relay_scenario.sh",
        )
        for name in scripts:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                result, run_root, log = self._wrapper_fixture(
                    Path(directory),
                    name,
                    host_identity="host:1",
                    container_identity="container:2",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("宿主与容器 runs 根不同源", result.stderr)
                self.assertFalse(run_root.exists())
                self.assertEqual(log.count(" stat -Lc "), 1)
                self.assertNotIn("codex", log)
                self.assertNotIn("h1_wire_probe", log)
                self.assertNotIn("upstream_byte_relay", log)

    def test_宿主_wrapper_同源时只在宿主数据根创建输出(self) -> None:
        """同源夹具通过边界后，输出只能落到显式宿主数据根。"""

        scripts = (
            "run_official_codex_compact_capture.sh",
            "run_official_http_fallback_baseline.sh",
            "run_official_relay_scenario.sh",
        )
        for name in scripts:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                result, run_root, log = self._wrapper_fixture(
                    Path(directory),
                    name,
                    host_identity="same:7",
                    container_identity="same:7",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(run_root.is_dir())
                self.assertIn(" stat -Lc ", log)
                self.assertFalse(Path("/container-capture/runs/storage-fixture").exists())

    @unittest.skipUnless(
        Path("/proc/self/stat").is_file() and shutil.which("setsid") is not None,
        "MITM 生命周期夹具需要 Linux /proc 与 setsid",
    )
    def test_mitm_离线夹具可重复启动停止且失败不留孤儿(self) -> None:
        """用 localhost 假 MITM 动态验证日志竞态与进程回收闭环。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture_mount = root / "capture"
            (capture_mount / "runs").mkdir(parents=True, mode=0o700)
            state_root = root / "state"
            confdir = root / "confdir"
            confdir.mkdir(mode=0o700)
            addon = root / "addon.py"
            addon.write_text("# 离线占位插件。\n", encoding="utf-8")
            fake_mitm = root / "fake-mitmdump.py"
            self._write_executable(
                fake_mitm,
                "#!/usr/bin/env python3\n"
                "import signal, socket, sys, time\n"
                "port = int(sys.argv[sys.argv.index('--listen-port') + 1])\n"
                "listener = socket.socket()\n"
                "listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                "listener.bind(('127.0.0.1', port))\n"
                "listener.listen()\n"
                "def stop(*_args):\n"
                "    listener.close()\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "while True:\n"
                "    time.sleep(0.05)\n",
            )
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            environment = {
                **os.environ,
                "CAPTURE_MOUNT": str(capture_mount),
                "CAPTURE_STATE_ROOT": str(state_root),
                "CAPTURE_MITMDUMP_BIN": str(fake_mitm),
                "CAPTURE_MITM_ADDON": str(addon),
                "CAPTURE_MITM_CONFDIR": str(confdir),
                "CAPTURE_MITM_PORT": str(port),
                "CAPTURE_TASK": "oauth",
                "CAPTURE_BOUNDARY": "offline_fixture",
                "CAPTURE_SCENARIO": "lifecycle",
                "CAPTURE_TARGET_HOSTS": "localhost",
            }
            start = RUNTIME_ROOT / "start_mitm.sh"
            stop = RUNTIME_ROOT / "stop_mitm.sh"
            for index in range(1, 4):
                run_id = f"offline-mitm-{index}"
                started = subprocess.run(
                    ["bash", str(start), run_id, "fake-mitm"],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(started.returncode, 0, started.stderr)
                state_path = state_root / "mitm.state"
                self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
                log_path = (
                    capture_mount / "runs" / run_id / "mitm/fake-mitm/mitmdump.log"
                )
                self.assertTrue(log_path.is_file())
                self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
                stopped = subprocess.run(
                    ["bash", str(stop)],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertFalse(state_path.exists())
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

            failing_mitm = root / "failing-mitmdump.sh"
            self._write_executable(
                failing_mitm,
                "#!/usr/bin/env bash\nexit 42\n",
            )
            failed_environment = {
                **environment,
                "CAPTURE_MITMDUMP_BIN": str(failing_mitm),
            }
            failed = subprocess.run(
                ["bash", str(start), "offline-mitm-failure", "fake-mitm"],
                env=failed_environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse((state_root / "mitm.state").exists())

    def test_运行镜像包含抓包与_zstd_依赖(self) -> None:
        dockerfile = (TOOL_ROOT / "runtime_image" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        for dependency in (
            "bubblewrap",
            "mitmproxy",
            "python3-zstandard",
            "tcpdump",
            "tshark",
            "tini",
        ):
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, dockerfile)
        self.assertIn("import zstandard", dockerfile)

    def test_调用方不再依赖镜像外的旧脚本目录(self) -> None:
        for name in (
            "run_official_codex_compact_capture.sh",
            "run_sub2api_direct_matrix.sh",
            "run_sub2api_openai_mitm_matrix.sh",
        ):
            with self.subTest(name=name):
                source = (TOOL_ROOT / name).read_text(encoding="utf-8")
                self.assertIn("capture_runtime_root=", source)
                self.assertNotIn("/opt/oauth-capture/scripts/", source)

    def test_模型条件收据在冻结抓包运行时内生成(self) -> None:
        """zstd 解析依赖必须来自 Campaign 绑定镜像，不能依赖 ARM64 宿主 Python。"""

        source = (TOOL_ROOT / "run_official_relay_scenario.sh").read_text(
            encoding="utf-8"
        )
        invocation = (
            'docker exec "$capture_container" \\\n'
            '    python3 "$capture_tool_root/model_condition_receipts.py"'
        )
        self.assertIn(invocation, source)
        self.assertNotIn(
            '\n  python3 "$capture_tool_root/model_condition_receipts.py"',
            source,
        )

    def test_官方驱动全部使用_campaign_冻结二进制(self) -> None:
        """不得落回镜像内不存在或未进入 Campaign 身份的默认 Codex 路径。"""

        compact = (TOOL_ROOT / "run_official_codex_compact_capture.sh").read_text(
            encoding="utf-8"
        )
        relay = (TOOL_ROOT / "run_official_relay_scenario.sh").read_text(
            encoding="utf-8"
        )
        self.assertEqual(compact.count('--codex-bin "$codex_bin"'), 2)
        # realtime、三类普通 TUI、guardian、memgen、review，以及两个已受管的
        # compaction/auth 驱动，共九处显式绑定；直接 codex exec 也使用 $codex_bin。
        self.assertGreaterEqual(relay.count('--codex-bin "$codex_bin"'), 9)
        self.assertIn('timeout 240 "$codex_bin" exec', relay)


if __name__ == "__main__":
    unittest.main()
