"""修好接着跑第 61 项：Sub2API ingress 启停在两个候选矩阵脚本里失败即清理、残留先清、竞态按真实状态判定。

2026-09-28 194249z VC-5 批次 24：抓包镜像内置的 start_ingress.sh 在后台拉起 mitmdump 后立即 chmod 日志，日志由子进程
重定向时才创建，竞态下报 No such file 非零退出，进程却已在运行；矩阵脚本只在启动成功后置 ingress_started，清理区没
停它，孤儿 ingress 占住 18081 与 PID 文件，后续 compact-direct、compact-mitm 以“已有 Sub2API ingress 进程运行”连带失败。

这里用假的 docker 驱动脚本里的真实函数（从两个脚本按标记原样截取），并在本机用假的 ps／ss 真实执行容器内就绪核对脚本。
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("run_sub2api_openai_mitm_matrix.sh", "run_sub2api_direct_matrix.sh")
BLOCK_RE = re.compile(r"^# >>> sub2api-ingress-lifecycle\n(.*?)^# <<< sub2api-ingress-lifecycle\n", re.S | re.M)
STOP_PAIR_RE = re.compile(r"^stop_pair\(\) \{\n.*?^\}\n", re.S | re.M)
START_PATH = "/capture/scripts/start_ingress.sh"
STOP_PATH = "/capture/scripts/stop_ingress.sh"
INGRESS_COMMAND = (
    "/usr/bin/python3 /usr/bin/mitmdump --mode reverse:http://sub2apiplus:8080 --listen-host 0.0.0.0 "
    "--listen-port 18081 --set confdir=/opt/mitm -s /capture/addons/dump_ingress.py"
)

# 假 docker：只模拟 ingress 相关的三种 exec，状态由 FAKE_STATE 目录下的标记文件控制，每次调用记一行到 FAKE_LOG。
FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
state=$FAKE_STATE
if [[ $1 == exec ]]; then
  shift 2
fi
case "${1:-}" in
  /capture/scripts/stop_ingress.sh)
    echo stop >>"$FAKE_LOG"
    if [[ -e $state/stop_fail ]]; then
      echo "PID 对应的不是本工具启动的 Sub2API ingress，拒绝结束：python3 other" >&2
      exit 1
    fi
    if [[ -e $state/running ]]; then
      rm -f "$state/running"
      echo "Sub2API ingress 已停止。"
    else
      echo "Sub2API ingress 当前未运行。"
    fi
    ;;
  /capture/scripts/start_ingress.sh)
    echo "start $2 $3" >>"$FAKE_LOG"
    if [[ -e $state/running ]]; then
      echo "已有 Sub2API ingress 进程运行，PID=222705。" >&2
      exit 1
    fi
    if [[ -e $state/start_launches ]]; then
      : >"$state/running"
    fi
    exit "$(cat "$state/start_rc" 2>/dev/null || echo 0)"
    ;;
  bash)
    # 参数形如：bash -c <核对脚本> _ <run_id> <subject> <PID 文件> <runs 根>
    echo "ready $5 $6 $7 $8" >>"$FAKE_LOG"
    exit "$(cat "$state/ready_rc" 2>/dev/null || echo 1)"
    ;;
  *)
    echo "other $*" >>"$FAKE_LOG"
    ;;
esac
"""


def _source(script: str) -> str:
    return (ROOT / script).read_text(encoding="utf-8")


def _block(script: str) -> str:
    match = BLOCK_RE.search(_source(script))
    if match is None:
        raise AssertionError(f"{script} 缺少 sub2api-ingress-lifecycle 标记块")
    return match.group(0)


def _stop_pair(script: str) -> str:
    match = STOP_PAIR_RE.search(_source(script))
    if match is None:
        raise AssertionError(f"{script} 缺少 stop_pair 函数")
    return match.group(0)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class IngressLifecycleStructureTests(unittest.TestCase):
    def test_both_matrix_scripts_share_identical_block(self) -> None:
        blocks = {script: _block(script) for script in SCRIPTS}
        self.assertEqual(blocks[SCRIPTS[0]], blocks[SCRIPTS[1]])

    def test_start_only_through_checked_helper(self) -> None:
        for script in SCRIPTS:
            with self.subTest(script=script):
                source = _source(script)
                block = _block(script)
                outside = source.replace(block, "")
                block_code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
                # 启动脚本只在标记块里调用（注释除外恰好一处）；块外不得再有“启动成功后才置位”的旧写法。
                self.assertNotIn(START_PATH, outside)
                self.assertEqual(block_code.count(START_PATH), 1)
                self.assertNotRegex(outside, r"ingress_started=1")
                self.assertEqual(len(re.findall(r"^\s+start_ingress_checked \"\$run_id\" ", outside, re.M)), 1)
                # 清理区仍按 ingress_started 调 stop 脚本（先置位的前提）。
                self.assertIn(STOP_PATH, _stop_pair(script))
                self.assertIn("ingress_started == 1", _stop_pair(script))


class IngressLifecycleBehaviorTests(unittest.TestCase):
    """用脚本里的真实函数与真实 stop_pair 组成最小程序：set -Eeuo pipefail、EXIT 时跑 stop_pair，与矩阵脚本一致。"""

    def _run(self, script: str, *, state: set[str], start_rc: int = 0, ready_rc: int = 1):
        temp = Path(tempfile.mkdtemp(prefix="ingress-lifecycle-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(temp)], check=False)
        bin_dir = temp / "bin"
        bin_dir.mkdir()
        state_dir = temp / "state"
        state_dir.mkdir()
        for name in state:
            (state_dir / name).write_text("", encoding="utf-8")
        (state_dir / "start_rc").write_text(str(start_rc), encoding="utf-8")
        (state_dir / "ready_rc").write_text(str(ready_rc), encoding="utf-8")
        _write_executable(bin_dir / "docker", FAKE_DOCKER)
        log = temp / "docker.log"
        program = "\n".join(
            [
                "set -Eeuo pipefail",
                "capture_container=capture-cli",
                "capture_runtime_root=/runtime",
                "ingress_started=0",
                "mitm_started=0",
                "direct_started=0",
                "active_subject=",
                _block(script),
                _stop_pair(script),
                "trap stop_pair EXIT",
                'start_ingress_checked run-1 codex-ws',
                'echo "RESULT ingress_started=$ingress_started"',
            ]
        )
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_STATE": str(state_dir), "FAKE_LOG": str(log)}
        result = subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True, timeout=60)
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result, calls, state_dir

    def test_normal_start_stops_nothing_extra(self) -> None:
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls, _state = self._run(script, state={"start_launches"}, start_rc=0)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("RESULT ingress_started=1", result.stdout)
                # 预清理（空操作）→ 启动 → 退出时清理区停掉本次 ingress；启动成功不做就绪核对。
                self.assertEqual(calls, ["stop", "start run-1 codex-ws", "stop"])
                self.assertNotIn("已清理上一作业残留", result.stderr)

    def test_start_race_with_ready_ingress_continues(self) -> None:
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls, _state = self._run(script, state={"start_launches"}, start_rc=1, ready_rc=0)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("按已启动继续：run_id=run-1 subject=codex-ws", result.stderr)
                self.assertEqual(
                    calls,
                    [
                        "stop",
                        "start run-1 codex-ws",
                        "ready run-1 codex-ws /run/oauth-capture/sub2api-ingress.pid /capture/runs",
                        "stop",
                    ],
                )

    def test_failed_start_that_launched_process_is_stopped_by_cleanup(self) -> None:
        # 复现批次 24：启动脚本拉起进程后非零退出且未就绪。修复前清理区不调 stop，进程残留（running 标记留存）。
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls, state = self._run(script, state={"start_launches"}, start_rc=1, ready_rc=1)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Sub2API ingress 启动失败：run_id=run-1 subject=codex-ws", result.stderr)
                self.assertNotIn("RESULT", result.stdout)
                self.assertEqual(calls[-1], "stop")
                self.assertFalse((state / "running").exists(), "失败路径的清理区必须停掉已拉起的 ingress")

    def test_leftover_ingress_is_cleared_before_start(self) -> None:
        # 上一作业留下的孤儿 ingress：修复前启动脚本报“已有 Sub2API ingress 进程运行”，后续作业连带失败。
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls, _state = self._run(script, state={"running", "start_launches"}, start_rc=0)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("已清理上一作业残留的 Sub2API ingress。", result.stderr)
                self.assertEqual(calls[:2], ["stop", "start run-1 codex-ws"])
                self.assertNotIn("已有 Sub2API ingress 进程运行", result.stderr)

    def test_foreign_process_refuses_start(self) -> None:
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls, _state = self._run(script, state={"stop_fail"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("清理残留 Sub2API ingress 失败，拒绝启动新的 ingress。", result.stderr)
                self.assertEqual(calls, ["stop"], "核对不过时不得启动，也不再重复停")


class IngressReadyScriptTests(unittest.TestCase):
    """在本机真实执行容器内就绪核对脚本：真 PID（sleep 子进程）、假 ps／ss、临时 PID 文件与元数据。"""

    def setUp(self) -> None:
        match = re.search(r"^ingress_ready_script='\n(.*?)^'\n", _block(SCRIPTS[0]), re.S | re.M)
        self.assertIsNotNone(match, "标记块里缺少 ingress_ready_script")
        self.script = match.group(1)
        self.temp = Path(tempfile.mkdtemp(prefix="ingress-ready-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.temp)], check=False)
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        _write_executable(self.bin / "ps", '#!/usr/bin/env bash\nprintf "%s\\n" "$FAKE_PS_ARGS"\n')
        _write_executable(self.bin / "ss", '#!/usr/bin/env bash\nprintf "%s" "$FAKE_SS_OUTPUT"\n')
        self.sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.sleeper.wait)
        self.addCleanup(self.sleeper.kill)
        self.pid_file = self.temp / "sub2api-ingress.pid"
        self.pid_file.write_text(f"{self.sleeper.pid}\n", encoding="utf-8")
        self.runs = self.temp / "runs"
        metadata = self.runs / "run-1" / "ingress" / "codex-ws" / "metadata.txt"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("run_id=run-1\nsubject=codex-ws\nupstream=http://sub2apiplus:8080\n", encoding="utf-8")

    def _ready(
        self, *, ps_args: str = INGRESS_COMMAND, listening: bool = True, run_id: str = "run-1", listener_pid: int | None = None
    ) -> int:
        owner = self.sleeper.pid if listener_pid is None else listener_pid
        ss_output = "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        ss_output += 'LISTEN 0 128 0.0.0.0:18080 0.0.0.0:* users:(("mitmdump",pid=1,fd=7))\n'
        if listening:
            ss_output += f'LISTEN 0 4096 0.0.0.0:18081 0.0.0.0:* users:(("mitmdump",pid={owner},fd=9))\n'
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_PS_ARGS": ps_args,
            "FAKE_SS_OUTPUT": ss_output,
        }
        result = subprocess.run(
            ["bash", "-c", self.script, "_", run_id, "codex-ws", str(self.pid_file), str(self.runs)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode

    def test_ready_when_all_facts_match(self) -> None:
        self.assertEqual(self._ready(), 0)

    def test_not_ready_for_dead_pid(self) -> None:
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.pid_file.write_text(f"{dead.pid}\n", encoding="utf-8")
        # 假 ss 把 18081 记在这个已退出的 PID 名下，单独验证存活校验（真实 ss 不会列出死进程，监听者核对另有用例）。
        self.assertEqual(self._ready(listener_pid=dead.pid), 1)

    def test_not_ready_for_foreign_command(self) -> None:
        self.assertEqual(self._ready(ps_args="/usr/bin/python3 /usr/bin/mitmdump --listen-port 18080 -s /capture/addons/mitm_capture.py"), 1)

    def test_not_ready_without_this_run_metadata(self) -> None:
        self.assertEqual(self._ready(run_id="run-2"), 1)

    def test_not_ready_without_listener(self) -> None:
        self.assertEqual(self._ready(listening=False), 1)

    def test_not_ready_when_port_held_by_other_pid(self) -> None:
        # 旧 ingress 收到 SIGTERM 后迟迟不退、仍占着 18081，新进程尚未因绑定失败退出：端口不属于 PID 文件里的进程即不就绪。
        self.assertEqual(self._ready(listener_pid=self.sleeper.pid + 100000), 1)


if __name__ == "__main__":
    unittest.main()
