"""codex_daemon_lifecycle：采集容器内 Codex app-server daemon 的生命周期（0.157.0 起 TUI 默认经 daemon）。

进程表用临时目录伪造的 /proc 表示；发信号换成改写伪进程表的替身，不会触碰真实进程。客户端命令用
假 codex 脚本模拟 ``app-server daemon version／stop`` 的 JSON 输出。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import stat
import tempfile
import textwrap
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_daemon_lifecycle as lifecycle

FAKE_CODEX = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, pathlib, sys
    home = os.environ["CODEX_HOME"]
    proc = pathlib.Path(os.environ["FAKE_PROC_ROOT"])
    log = pathlib.Path(os.environ["FAKE_CODEX_LOG"])
    log.open("a").write(" ".join(sys.argv[1:]) + " CODEX_HOME=" + home + "\\n")
    command = sys.argv[-1]
    daemon = proc / os.environ.get("FAKE_DAEMON_PID", "4242")
    if command == "version":
        if os.environ.get("FAKE_VERSION_FAIL") == "1":
            print("Error: app server is not running", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"status": "running", "backend": "pid", "cliVersion": "0.157.0",
                          "appServerVersion": os.environ.get("FAKE_APP_VERSION", "0.157.0"),
                          "managedCodexVersion": "0.157.0", "socketPath": home + "/sock"}))
    elif command == "stop":
        if os.environ.get("FAKE_STOP_KEEP") != "1" and daemon.exists():
            for child in daemon.iterdir():
                child.unlink()
            daemon.rmdir()
        print(json.dumps({"status": "stopped", "backend": "pid"}))
    """
)


class FakeProc:
    """伪造的 /proc：每个进程一个目录，含 stat、cmdline、environ 与 exe 符号链接。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self.immune: dict[int, set[int]] = {}
        self.signals: list[tuple[str, int, int]] = []

    def add(
        self,
        pid: int,
        argv: list[str],
        *,
        codex_home: str | None = None,
        exe: str = "/usr/bin/python3",
        pgid: int | None = None,
        state: str = "S",
        ignore: tuple[int, ...] = (),
    ) -> None:
        base = self.root / str(pid)
        base.mkdir()
        (base / "stat").write_text(f"{pid} (proc) {state} 1 {pgid or pid} {pgid or pid} 0 -1 0\n")
        (base / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
        environ = [b"PATH=/usr/bin", b"HOME=/root"]
        if codex_home is not None:
            environ.append(b"CODEX_HOME=" + codex_home.encode())
        (base / "environ").write_bytes(b"\0".join(environ) + b"\0")
        (base / "exe").symlink_to(exe)
        self.immune[pid] = set(ignore)

    def alive(self) -> set[int]:
        return {int(path.name) for path in self.root.iterdir()}

    def _deliver(self, pid: int, sig: int) -> None:
        base = self.root / str(pid)
        if not base.exists():
            raise ProcessLookupError(pid)
        if sig in self.immune.get(pid, set()):
            return
        shutil.rmtree(base)

    def signal_process(self, pid: int, sig: int) -> None:
        self.signals.append(("pid", pid, int(sig)))
        self._deliver(pid, sig)

    def signal_group(self, pgid: int, sig: int) -> None:
        self.signals.append(("group", pgid, int(sig)))
        members = [
            int(path.name)
            for path in self.root.iterdir()
            if (path / "stat").read_text().split(") ", 1)[1].split()[2] == str(pgid)
        ]
        if not members:
            raise ProcessLookupError(pgid)
        for pid in members:
            self._deliver(pid, sig)


class LifecycleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.proc = FakeProc(self.tmp / "proc")
        self.parent = self.tmp / "root"
        self.parent.mkdir()
        self.home = self.parent / ".codex-daemon-unit-run"
        self.source = self.tmp / "source-home"
        self.source.mkdir()
        (self.source / "auth.json").write_text('{"tokens":{"access_token":"secret-value"}}')
        (self.source / "config.toml").write_text(
            'model = "gpt-5.6-sol"\n[projects."/tmp/tui-probe"]\ntrust_level = "trusted"\n'
        )
        (self.source / "installation_id").write_text("11111111-2222-3333-4444-555555555555")
        (self.source / "version.json").write_text('{"latest_version":"0.157.0"}')
        (self.source / ".sandbox_migration").write_text("v1")
        (self.source / "models_cache.json").write_text("{}")
        self.codex = self.tmp / "codex"
        self.codex.write_text(FAKE_CODEX)
        self.codex.chmod(0o755)
        self.codex_log = self.tmp / "codex.log"
        patcher = mock.patch.dict(
            os.environ,
            {"FAKE_PROC_ROOT": str(self.proc.root), "FAKE_CODEX_LOG": str(self.codex_log)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, target in (("signal_process", self.proc.signal_process), ("signal_group", self.proc.signal_group)):
            signal_patch = mock.patch.object(lifecycle, name, side_effect=target)
            signal_patch.start()
            self.addCleanup(signal_patch.stop)

    def run_main(self, *arguments: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = lifecycle.main(
                [
                    "--proc-root", str(self.proc.root),
                    "--homes-parent", str(self.parent),
                    "--grace", "0.3",
                    "--kill-grace", "0.3",
                    *arguments,
                ]
            )
        lines = buffer.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, buffer.getvalue())
        return code, json.loads(lines[0])

    def add_daemon(self, pid: int = 4242, home: Path | None = None, **extra) -> None:
        home = home or self.home
        self.proc.add(
            pid,
            [f"{home}/packages/app-server-daemon/current/bin/codex", "app-server", "--listen", "unix://", "--managed-daemon"],
            codex_home=str(home),
            exe=f"{home}/packages/app-server-daemon/releases/0.157.0-aarch64-unknown-linux-musl/bin/codex",
            **extra,
        )


class PrepareTest(LifecycleTestBase):
    def test_冷启动_home_只带白名单文件与_features_表(self) -> None:
        code, result = self.run_main(
            "prepare", "--home", str(self.home), "--source-home", str(self.source),
            "--disable-features", "plugins apps",
        )
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["copied_files"], ["auth.json", "installation_id", "version.json", ".sandbox_migration"]
        )
        self.assertEqual(result["features"], {"plugins": False, "apps": False})
        # 模型缓存不复制：daemon 启动期的 models 请求正是要取的样本。
        self.assertFalse((self.home / "models_cache.json").exists())
        config = tomllib.loads((self.home / "config.toml").read_text())
        self.assertEqual(config["features"], {"plugins": False, "apps": False})
        self.assertEqual(config["projects"]["/tmp/tui-probe"]["trust_level"], "trusted")
        settings = json.loads((self.home / "app-server-daemon" / "settings.json").read_text())
        self.assertEqual(settings, {"shutdownGraceSeconds": 10, "updater": {"autoUpdateEnabled": False}})
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        for name in ("auth.json", "config.toml", "app-server-daemon/settings.json"):
            self.assertEqual(stat.S_IMODE((self.home / name).stat().st_mode), 0o600, name)
        # 输出不含凭据原文。
        self.assertNotIn("secret-value", json.dumps(result))

    def test_home_已存在时拒绝复用(self) -> None:
        self.home.mkdir()
        code, result = self.run_main("prepare", "--home", str(self.home), "--source-home", str(self.source))
        self.assertEqual(code, 3)
        self.assertIn("已存在", result["error"])

    def test_源配置已含_features_时失败关闭(self) -> None:
        (self.source / "config.toml").write_text('model = "x"\n[features]\nfoo = true\n')
        code, result = self.run_main(
            "prepare", "--home", str(self.home), "--source-home", str(self.source), "--disable-features", "plugins"
        )
        self.assertEqual(code, 3)
        self.assertIn("features", result["error"])
        self.assertFalse(self.home.exists())

    def test_点号键形式的_features_同样拒绝(self) -> None:
        (self.source / "config.toml").write_text('model = "x"\nfeatures.foo = true\n')
        code, _ = self.run_main("prepare", "--home", str(self.home), "--source-home", str(self.source))
        self.assertEqual(code, 3)

    def test_缺少_auth_json_时失败(self) -> None:
        (self.source / "auth.json").unlink()
        code, result = self.run_main("prepare", "--home", str(self.home), "--source-home", str(self.source))
        self.assertEqual(code, 3)
        self.assertIn("auth.json", result["error"])
        self.assertFalse(self.home.exists())

    def test_源配置损坏属于环境条件而非参数错误(self) -> None:
        (self.source / "config.toml").write_text("model = [\n")
        code, result = self.run_main("prepare", "--home", str(self.home), "--source-home", str(self.source))
        self.assertEqual(code, 3)
        self.assertIn("解析失败", result["error"])

    def test_非法_home_与功能名以_2_退出(self) -> None:
        for home in (self.tmp / "elsewhere", self.parent / ".codex", self.parent / ".codex-daemon-a/b"):
            with self.subTest(home=str(home)):
                code, _ = self.run_main("prepare", "--home", str(home), "--source-home", str(self.source))
                self.assertEqual(code, 2)
        code, _ = self.run_main(
            "prepare", "--home", str(self.home), "--source-home", str(self.source), "--disable-features", "Plugins"
        )
        self.assertEqual(code, 2)
        self.assertFalse(self.home.exists())


class StatusTest(LifecycleTestBase):
    def test_daemon_运行且版本相符(self) -> None:
        self.add_daemon()
        code, result = self.run_main(
            "status", "--home", str(self.home), "--codex-bin", str(self.codex),
            "--require-mode", "daemon", "--expect-version", "0.157.0",
        )
        self.assertEqual(code, 0, result)
        self.assertEqual(result["mode"], "daemon")
        self.assertEqual(result["daemon"]["appServerVersion"], "0.157.0")
        self.assertEqual(result["processes"], [{"pid": 4242, "role": "daemon", "codex_home": str(self.home)}])
        self.assertIn(f"daemon version CODEX_HOME={self.home}", self.codex_log.read_text())

    def test_未拉起_daemon_判为内嵌并按要求失败(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_VERSION_FAIL": "1"}):
            code, result = self.run_main(
                "status", "--home", str(self.home), "--codex-bin", str(self.codex), "--require-mode", "daemon"
            )
        self.assertEqual(code, 3)
        self.assertEqual(result["mode"], "embedded")
        self.assertIn("not running", result["stderr_tail"])

    def test_版本不符失败(self) -> None:
        self.add_daemon()
        with mock.patch.dict(os.environ, {"FAKE_APP_VERSION": "0.158.0"}):
            code, result = self.run_main(
                "status", "--home", str(self.home), "--codex-bin", str(self.codex), "--expect-version", "0.157.0"
            )
        self.assertEqual(code, 3)
        self.assertIn("0.158.0", result["error"])

    def test_version_报运行但进程表没有_daemon_不算_daemon_模式(self) -> None:
        code, result = self.run_main("status", "--home", str(self.home), "--codex-bin", str(self.codex))
        self.assertEqual(code, 0)
        self.assertEqual(result["mode"], "embedded")


class StopTest(LifecycleTestBase):
    def test_优雅停止后无残留(self) -> None:
        self.add_daemon()
        code, result = self.run_main("stop", "--home", str(self.home), "--codex-bin", str(self.codex))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["cli_stop"]["status"], "stopped")
        self.assertEqual(result["terminated"], [])
        self.assertEqual(result["remaining"], [])
        self.assertEqual(self.proc.signals, [])

    def test_updater_与安装子进程按进程组终止(self) -> None:
        self.add_daemon()
        updater = f"{self.home}/packages/app-server-daemon/current/bin/codex"
        self.proc.add(5000, [updater, "app-server", "daemon", "pid-update-loop"], codex_home=str(self.home), exe=updater)
        self.proc.add(5001, ["/bin/sh", "-c", "curl install.sh"], codex_home=str(self.home), exe="/bin/dash", pgid=5000)
        self.proc.add(6000, ["/usr/bin/sleep", "100"], exe="/usr/bin/sleep")
        code, result = self.run_main("stop", "--home", str(self.home), "--codex-bin", str(self.codex))
        self.assertEqual(code, 0, result)
        self.assertEqual({item["pid"] for item in result["terminated"]}, {5000, 5001})
        self.assertIn(("group", 5000, int(signal.SIGTERM)), self.proc.signals)
        self.assertEqual(self.proc.alive(), {6000})

    def test_不响应_SIGTERM_的进程兜底_SIGKILL(self) -> None:
        self.add_daemon(ignore=(signal.SIGTERM,))
        with mock.patch.dict(os.environ, {"FAKE_STOP_KEEP": "1"}):
            code, result = self.run_main("stop", "--home", str(self.home), "--codex-bin", str(self.codex))
        self.assertEqual(code, 0, result)
        self.assertEqual([item["pid"] for item in result["killed"]], [4242])
        self.assertIn(("group", 4242, int(signal.SIGKILL)), self.proc.signals)

    def test_终止不了即失败(self) -> None:
        self.add_daemon(ignore=(signal.SIGTERM, signal.SIGKILL))
        with mock.patch.dict(os.environ, {"FAKE_STOP_KEEP": "1"}):
            code, result = self.run_main("stop", "--home", str(self.home), "--codex-bin", str(self.codex))
        self.assertEqual(code, 3)
        self.assertEqual([item["pid"] for item in result["remaining"]], [4242])

    def test_同组的无关进程不受组信号波及(self) -> None:
        # 组长不属于该 home 时只逐个发信号。
        self.proc.add(7000, ["/usr/bin/bash"], exe="/usr/bin/bash")
        self.proc.add(7001, ["/usr/bin/python3", "drive_codex_tui.py"], codex_home=str(self.home), pgid=7000)
        code, result = self.run_main("stop", "--home", str(self.home), "--codex-bin", str(self.codex))
        self.assertEqual(code, 0, result)
        self.assertNotIn("group", {kind for kind, _, _ in self.proc.signals})
        self.assertEqual(self.proc.alive(), {7000})


class SweepAndRemoveTest(LifecycleTestBase):
    def test_外来_daemon_失败关闭且不发任何信号(self) -> None:
        self.add_daemon(pid=4100, home=Path("/root/.codex"))
        self.add_daemon(pid=4200)
        code, result = self.run_main("sweep")
        self.assertEqual(code, 3)
        self.assertIn("/root/.codex", result["error"])
        self.assertEqual(self.proc.signals, [])
        self.assertEqual(self.proc.alive(), {4100, 4200})

    def test_只凭可执行文件位置也能识别外来_daemon(self) -> None:
        # 默认 home 启动时环境里没有 CODEX_HOME，只能从 exe 反推。
        self.proc.add(
            4300,
            ["/root/.codex/packages/app-server-daemon/current/bin/codex", "app-server", "--listen", "unix://", "--managed-daemon"],
            exe="/root/.codex/packages/app-server-daemon/releases/0.157.0/bin/codex (deleted)",
        )
        code, result = self.run_main("sweep")
        self.assertEqual(code, 3)
        self.assertIn('"codex_home": "/root/.codex"', result["error"])

    def test_本作业命名空间的残留被终止并删除_home(self) -> None:
        stale_running = self.parent / ".codex-daemon-old-run"
        stale_idle = self.parent / ".codex-daemon-crashed-run"
        for home in (stale_running, stale_idle):
            home.mkdir()
            (home / "auth.json").write_text("{}")
        self.add_daemon(pid=4400, home=stale_running)
        self.proc.add(4500, ["/usr/bin/sleep", "100"], exe="/usr/bin/sleep")
        self.proc.add(4600, [str(self.codex), "app-server"], codex_home="/tmp/codex-memgen-x", exe=str(self.codex))
        code, result = self.run_main("sweep")
        self.assertEqual(code, 0, result)
        self.assertEqual([item["pid"] for item in result["terminated"]], [4400])
        self.assertEqual(sorted(result["removed_homes"]), [".codex-daemon-crashed-run", ".codex-daemon-old-run"])
        self.assertFalse(stale_running.exists() or stale_idle.exists())
        # 与 daemon 无关的进程（含其它作业的 stdio app-server）不受影响。
        self.assertEqual(self.proc.alive(), {4500, 4600})

    def test_僵尸进程不计入(self) -> None:
        self.add_daemon(pid=4700, home=Path("/root/.codex"), state="Z")
        code, result = self.run_main("sweep")
        self.assertEqual(code, 0, result)

    def test_remove_要求先无残留(self) -> None:
        self.home.mkdir()
        self.add_daemon()
        code, result = self.run_main("remove", "--home", str(self.home))
        self.assertEqual(code, 3)
        self.assertTrue(self.home.exists())
        shutil.rmtree(self.proc.root / "4242")
        code, result = self.run_main("remove", "--home", str(self.home))
        self.assertEqual(code, 0, result)
        self.assertTrue(result["removed"])
        self.assertFalse(self.home.exists())
        code, result = self.run_main("remove", "--home", str(self.home))
        self.assertEqual((code, result["removed"]), (0, False))


if __name__ == "__main__":
    unittest.main()
