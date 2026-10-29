"""候选采集与 wire 探针脚本对 keeper（账号保活容器）缺席的处理。

keeper 已下线不再部署。六个宿主脚本（五个 Codex 脚本与 Claude 候选脚本）在读 keeper 运行状态时：

* 容器在场：与以往一致，记录运行状态（在跑则先停、退出时按原状态恢复）；
* docker 明确报告容器不存在（no such object／container，大小写不敏感）：如实视为未部署，``keeper_was_running=false``，
  后面的停、启与恢复核验自然全部跳过；
* 其它失败（守护进程不可达等）：不得当成缺席，照旧失败退出。

本测试把各脚本里这段检测原样取出，用仿真 docker 在 bash 里逐个执行，并核对每个脚本的停启都只受
``keeper_was_running == true`` 守卫。
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "run_candidate_core_capture.sh",
    "run_candidate_aux_capture.sh",
    "run_h1_wire_probe.sh",
    "run_images_wire_probe.sh",
    "run_sub2api_openai_mitm_matrix.sh",
    "claude_run_candidate_capture.sh",
)
# 检测块：从 ``if keeper_probe=$(docker inspect`` 起到与它配对的 ``fi`` 止。
BLOCK_RE = re.compile(r"^if keeper_probe=\$\(docker inspect.*?^fi\n", re.MULTILINE | re.DOTALL)

FAKE_DOCKER = r"""#!/bin/sh
# 仿真 docker：只支持 inspect -f '{{.State.Running}}' <容器>。KEEPER_MODE 决定 keeper 的状态。
case "$KEEPER_MODE" in
  running) echo true ;;
  stopped) echo false ;;
  absent) echo "Error response from daemon: No such object: $4" >&2; exit 1 ;;
  absent_legacy) echo "Error: No such container: $4" >&2; exit 1 ;;
  # 真实 Docker 29.1.2（ARM64 采集主机，只读 ``docker inspect -f '{{.State.Running}}' <不存在的名字>`` 取样）：
  # 退出码 1，标准输出一个空行，标准错误是全小写的 ``error: no such object: <名>``。
  absent_docker29) echo; echo "error: no such object: $4" >&2; exit 1 ;;
  broken) echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock" >&2; exit 1 ;;
  # Docker 29 CLI 守护进程不可达的措辞（含 ``no such file or directory``，但不是容器缺席，必须失败关闭）。
  broken_docker29) echo "failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is correct and if the daemon is running: dial unix /var/run/docker.sock: connect: no such file or directory" >&2; exit 1 ;;
esac
"""


class KeeperOptionalScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bin = Path(directory.name)
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    def block(self, script: str) -> str:
        text = (TOOL_DIR / script).read_text(encoding="utf-8")
        matches = BLOCK_RE.findall(text)
        self.assertEqual(len(matches), 1, f"{script} 必须恰有一处 keeper 检测块")
        return matches[0]

    def run_block(self, script: str, mode: str) -> subprocess.CompletedProcess[str]:
        program = (
            "set -Eeuo pipefail\n"
            'keeper_container=sub2apiplus-keeper\nkeeper_was_running=unset\n'
            f"{self.block(script)}"
            'echo "keeper_was_running=$keeper_was_running"\n'
        )
        return subprocess.run(
            ["bash", "-c", program],
            env={"PATH": f"{self.bin}:{os.environ['PATH']}", "KEEPER_MODE": mode},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_detection_block_is_identical_in_every_script(self) -> None:
        blocks = {script: self.block(script) for script in SCRIPTS}
        self.assertEqual(len(set(blocks.values())), 1, "六个脚本的 keeper 检测块必须逐字一致")

    def test_present_keeps_previous_behavior(self) -> None:
        for script in SCRIPTS:
            for mode, expected in (("running", "true"), ("stopped", "false")):
                with self.subTest(script=script, mode=mode):
                    result = self.run_block(script, mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), f"keeper_was_running={expected}")
                    self.assertEqual(result.stderr, "")

    def test_absent_is_treated_as_not_deployed_and_skips_stop_start(self) -> None:
        for script in SCRIPTS:
            for mode in ("absent", "absent_legacy", "absent_docker29"):
                with self.subTest(script=script, mode=mode):
                    result = self.run_block(script, mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "keeper_was_running=false")
                    self.assertIn("keeper 容器不存在（未部署），跳过停启", result.stderr)

    def test_daemon_failure_is_not_mistaken_for_absence(self) -> None:
        for script in SCRIPTS:
            for mode in ("broken", "broken_docker29"):
                with self.subTest(script=script, mode=mode):
                    result = self.run_block(script, mode)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("无法读取 keeper 容器状态", result.stderr)
                    self.assertNotIn("keeper_was_running=", result.stdout)

    def test_docker29_lowercase_absence_regression(self) -> None:
        """0.165 的 VC-5 第一次失败：Docker 29.1.2 对不存在的容器输出小写 ``error: no such object:``，
        旧判断只认大写 ``No such object`` 而落到“无法读取 keeper 容器状态”退出 1。六个脚本都必须识别。"""

        for script in SCRIPTS:
            with self.subTest(script=script):
                result = self.run_block(script, "absent_docker29")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "keeper_was_running=false")
                self.assertNotIn("无法读取 keeper 容器状态", result.stderr)

    def test_stop_and_start_are_only_guarded_by_keeper_was_running(self) -> None:
        """每个 docker stop／start keeper 都紧跟在 ``keeper_was_running == true``（或停过才启的标志）守卫之内。"""

        for script in SCRIPTS:
            lines = (TOOL_DIR / script).read_text(encoding="utf-8").splitlines()
            touched = [
                index
                for index, line in enumerate(lines)
                if re.search(r'docker (stop|start) "\$keeper_container"', line)
            ]
            self.assertTrue(touched, f"{script} 应当仍有 keeper 停启")
            for index in touched:
                guard = lines[index - 1].strip()
                # Claude 脚本的恢复段用「停过才启」标志 keeper_stopped（只在 keeper_was_running 为真时置位）。
                self.assertRegex(
                    guard,
                    r"keeper_was_running\s*==\s*true|keeper_stopped\s*==\s*1",
                    f"{script}:{index + 1} 停启缺少守卫",
                )


class KeeperPlanCoordinateTests(unittest.TestCase):
    """plan 阶段：keeper 名字只是一个坐标，不要求容器存在，也不触碰 docker。"""

    def test_codex_plan_keeps_default_coordinate_and_documents_absence(self) -> None:
        import argparse

        from tools.official_client_capture import codex_upgrade

        parser = codex_upgrade._build_parser()
        subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        action = next(
            item for item in subparsers.choices["plan"]._actions if "--keeper-container" in item.option_strings
        )
        self.assertEqual(action.default, "sub2apiplus-keeper")
        self.assertIn("未部署", action.help)
        self.assertIn("在场时行为与以往完全一致", action.help)

    def test_claude_candidate_variables_do_not_depend_on_keeper_presence(self) -> None:
        """Claude 候选变量只校验名字合法与非空，不查容器；缺席与否由运行时探针如实记录。"""

        from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

        configuration = {
            "service_container": "sub2apiplus",
            "keeper_container": "sub2apiplus-keeper",
            "postgres_container": "sub2apiplus-postgres",
            "claude_account_id": "7",
            "api_key_id": "9",
        }
        with unittest.mock.patch("subprocess.run", side_effect=AssertionError("plan 阶段不得调用 docker")):
            variables = candidate_capture.candidate_variables({"configuration": configuration})
        self.assertEqual(variables["keeper_container"], "sub2apiplus-keeper")


if __name__ == "__main__":
    unittest.main()
