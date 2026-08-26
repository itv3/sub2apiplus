"""ARM64 受管抓包运行时的静态安全与装配契约。"""

from __future__ import annotations

import subprocess
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
