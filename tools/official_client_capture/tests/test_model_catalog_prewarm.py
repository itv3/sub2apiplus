"""官方在线模型目录预热驱动的离线契约测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.capturelib.identity import (
    CAPTURE_SOURCE_RELATIVE_PATHS,
)
from tools.official_client_capture.drive_codex_model_catalog import (
    ModelCatalogPrewarmError,
    find_captured_model_catalog,
)


def h1_response(*, lite: bool) -> bytes:
    """构造只含目标模型的最小官方目录响应。"""

    body = json.dumps(
        {
            "models": [
                {"slug": "gpt-5.6-luna", "use_responses_lite": lite},
            ]
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return (
        b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
        + f"content-length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


class ModelCatalogPrewarmTest(unittest.TestCase):
    def test_预热脚本绑定隔离_HOME_与_relay_原文(self) -> None:
        """模型条件任务必须先取得在线 200，不能依赖退出前的后台竞速请求。"""

        tool_root = Path(__file__).parents[1]
        source = (tool_root / "run_official_relay_scenario.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("drive_codex_model_catalog.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("prewarm_model_catalog", source)
        self.assertIn('docker exec -e CODEX_HOME="$home"', source)
        self.assertIn("drive_codex_model_catalog.py", source)
        self.assertIn('--relay-dir "/capture/runs/$run_id/relay"', source)
        self.assertIn(
            '--model-catalog-prewarm "$work_dir/model-catalog-prewarm.json"',
            source,
        )
        self.assertIn("for attempt in 1 2 3", source)
        self.assertIn('rm -rf -- "$model_catalog_home"', source)
        result = subprocess.run(
            ["bash", "-n", str(tool_root / "run_official_relay_scenario.sh")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_驱动可从仓库外直接执行(self) -> None:
        """容器通过绝对路径启动脚本，不能依赖仓库根目录恰好在 sys.path。"""

        script = Path(__file__).parents[1] / "drive_codex_model_catalog.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd="/",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_完整在线目录与目标_Lite_条件同时成立(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = Path(directory) / "relay"
            relay.mkdir()
            (relay / "conn001.client_to_upstream.bin").write_bytes(
                b"GET /backend-api/codex/models?client_version=0.149.1 HTTP/1.1\r\n"
                b"host: chatgpt.com\r\n\r\n"
            )
            (relay / "conn001.upstream_to_client.bin").write_bytes(
                h1_response(lite=True)
            )

            result = find_captured_model_catalog(
                relay,
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            self.assertEqual(result["connection_id"], 1)
            self.assertTrue(result["use_responses_lite"])

    def test_只有单向请求时不能把_RPC_结果冒充在线证据(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = Path(directory) / "relay"
            relay.mkdir()
            (relay / "conn001.client_to_upstream.bin").write_bytes(
                b"GET /backend-api/codex/models HTTP/1.1\r\nhost: chatgpt.com\r\n\r\n"
            )
            with self.assertRaisesRegex(ModelCatalogPrewarmError, "尚未捕获"):
                find_captured_model_catalog(
                    relay,
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
                )

    def test_完整响应的_Lite_条件不符时失败关闭(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = Path(directory) / "relay"
            relay.mkdir()
            (relay / "conn001.client_to_upstream.bin").write_bytes(
                b"GET /backend-api/codex/models HTTP/1.1\r\nhost: chatgpt.com\r\n\r\n"
            )
            (relay / "conn001.upstream_to_client.bin").write_bytes(
                h1_response(lite=False)
            )
            with self.assertRaisesRegex(ModelCatalogPrewarmError, "未给出目标模型"):
                find_captured_model_catalog(
                    relay,
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
                )


if __name__ == "__main__":
    unittest.main()
