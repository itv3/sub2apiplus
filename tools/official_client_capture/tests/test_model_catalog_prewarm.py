"""官方在线模型目录预热驱动的离线契约测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.capturelib.identity import (
    CAPTURE_SOURCE_RELATIVE_PATHS,
)
from tools.official_client_capture.drive_codex_model_catalog import (
    ModelCatalogPrewarmError,
    build_debug_models_command,
    find_captured_model_catalog,
    parse_debug_models_catalog,
    run_prewarm,
    wait_for_mitm_model_catalog,
    wait_for_relay_model_catalog,
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
    def test_01491_正式场景固定容器_H2_同步_MITM_完成等待(self) -> None:
        """同步命令必须保留 rustls/H2，并在抓包容器内等待完整响应。"""

        scenario_path = (
            Path(__file__).parents[1] / "codex_upgrade_scenarios_0_149_1.json"
        )
        payload = json.loads(scenario_path.read_text(encoding="utf-8"))
        job = next(
            item for item in payload["capture_jobs"] if item["id"] == "official-core"
        )
        self.assertEqual(len(job["steps"]), 2)
        supplement = job["steps"][1]
        self.assertEqual(
            supplement["argv"][:5],
            ["docker", "exec", "{capture_container}", "bash", "-c"],
        )
        source = supplement["argv"][5]
        self.assertNotIn("--set http2=false", source)
        self.assertIn('--mitm-models-http "$models_file"', source)
        self.assertIn("debug_models_driver_status", source)

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
        self.assertIn("model_catalog_only=${MODEL_CATALOG_ONLY:-0}", source)
        self.assertIn(
            "MODEL_CATALOG_ONLY=1 必须同时启用 "
            "REQUIRE_MODEL_CONDITION_RECEIPT=1",
            source,
        )
        model_only_start = source.index(
            'if [[ $model_catalog_only == 1 ]]; then',
            source.index("for h in $RELAY_HOSTS"),
        )
        model_only_end = source.index("\nfi", model_only_start)
        model_only = source[model_only_start:model_only_end]
        self.assertIn("prewarm_model_catalog", model_only)
        self.assertIn("stop_relay", model_only)
        self.assertIn("scrub_relay_bytes", model_only)
        self.assertIn("exit 0", model_only)
        self.assertNotIn("codex exec", model_only)
        self.assertLess(
            model_only_start,
            source.index('case "$scenario" in'),
        )
        result = subprocess.run(
            ["bash", "-n", str(tool_root / "run_official_relay_scenario.sh")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_驱动固定使用在线_debug_models_且禁止_bundled(self) -> None:
        """冷 HOME 必须同步刷新在线目录，不能显式选择内置目录。"""

        command = build_debug_models_command(
            "/opt/codex-0.149.1/bin/codex",
            "0.149.1",
        )
        self.assertEqual(
            command[:3],
            ["/opt/codex-0.149.1/bin/codex", "debug", "models"],
        )
        self.assertNotIn("--bundled", command)
        self.assertFalse(
            any("model_provider" in argument for argument in command),
            "debug models 必须保留内置 openai provider",
        )

    def test_debug_models_JSON_同时核验目标模型与_Lite(self) -> None:
        raw = json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.5", "use_responses_lite": False},
                    {"slug": "gpt-5.6-luna", "use_responses_lite": True},
                ]
            }
        )
        models = parse_debug_models_catalog(
            raw,
            expected_model="gpt-5.6-luna",
            expected_lite=True,
        )
        self.assertEqual(len(models), 2)

        with self.assertRaisesRegex(ModelCatalogPrewarmError, "预期 Lite"):
            parse_debug_models_catalog(
                raw,
                expected_model="gpt-5.6-luna",
                expected_lite=False,
            )

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

    def test_只有单向请求时不能把命令结果冒充在线证据(self) -> None:
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

    def test_等待_MITM_完整响应后才允许写成功摘要(self) -> None:
        """debug models 完成后仍必须等待 MITM 刷盘真实 HTTP 200。"""

        with tempfile.TemporaryDirectory() as directory:
            models_http = Path(directory) / "models-http.jsonl"
            payload = {
                "request": {
                    "method": "GET",
                    "path": "/backend-api/codex/models?client_version=0.149.1",
                },
                "response": {
                    "status": 200,
                    "body": {
                        "json": {
                            "models": [
                                {
                                    "slug": "gpt-5.5",
                                    "use_responses_lite": False,
                                }
                            ]
                        }
                    },
                },
            }

            def delayed_write() -> None:
                time.sleep(0.05)
                models_http.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            writer = threading.Thread(target=delayed_write)
            writer.start()
            result = wait_for_mitm_model_catalog(
                models_http,
                expected_model="gpt-5.5",
                expected_lite=False,
                deadline=time.monotonic() + 1,
            )
            writer.join()
            self.assertEqual(result["source"], "mitm_models_http")
            self.assertEqual(result["path"], str(models_http))

    def test_等待_MITM_响应超时后失败关闭(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ModelCatalogPrewarmError, "等待 MITM"):
                wait_for_mitm_model_catalog(
                    Path(directory) / "models-http.jsonl",
                    expected_model="gpt-5.5",
                    expected_lite=False,
                    deadline=time.monotonic() + 0.01,
                )

    def test_等待_relay_完整响应后才允许写成功摘要(self) -> None:
        """普通字节中继也必须等待完整 body，不能只封存 HTTP 200 响应头。"""

        with tempfile.TemporaryDirectory() as directory:
            relay = Path(directory) / "relay"
            relay.mkdir()
            request_path = relay / "conn001.client_to_upstream.bin"
            response_path = relay / "conn001.upstream_to_client.bin"
            request_path.write_bytes(
                b"GET /backend-api/codex/models HTTP/1.1\r\n"
                b"host: chatgpt.com\r\n\r\n"
            )
            complete = h1_response(lite=True)
            response_path.write_bytes(complete[:80])

            def delayed_completion() -> None:
                time.sleep(0.05)
                response_path.write_bytes(complete)

            writer = threading.Thread(target=delayed_completion)
            writer.start()
            result = wait_for_relay_model_catalog(
                relay,
                expected_model="gpt-5.6-luna",
                expected_lite=True,
                deadline=time.monotonic() + 1,
            )
            writer.join()
            self.assertEqual(result["connection_id"], 1)
            self.assertEqual(result["response_path"], "relay/conn001.upstream_to_client.bin")

    def test_debug_models_成功且原始_HTTP_200_完整时才写摘要(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = root / "relay"
            relay.mkdir()
            (relay / "conn001.client_to_upstream.bin").write_bytes(
                b"GET /backend-api/codex/models HTTP/1.1\r\n"
                b"host: chatgpt.com\r\n\r\n"
            )
            (relay / "conn001.upstream_to_client.bin").write_bytes(
                h1_response(lite=True)
            )
            completed = subprocess.CompletedProcess(
                args=["codex", "debug", "models"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-luna",
                                "use_responses_lite": True,
                            }
                        ]
                    }
                ),
                stderr="",
            )
            output = root / "model-catalog-prewarm.json"
            with mock.patch(
                "tools.official_client_capture.drive_codex_model_catalog."
                "subprocess.run",
                return_value=completed,
            ) as runner:
                summary = run_prewarm(
                    codex_bin="/opt/codex-0.149.1/bin/codex",
                    codex_version="0.149.1",
                    model="gpt-5.6-luna",
                    expected_lite=True,
                    relay_dir=relay,
                    output=output,
                    timeout=1,
                )
            command = runner.call_args.args[0]
            self.assertEqual(command[1:3], ["debug", "models"])
            self.assertNotIn("--bundled", command)
            self.assertEqual(summary["protocol_record_count"], 1)
            self.assertEqual(summary["model_count"], 1)
            self.assertTrue(output.is_file())

    def test_debug_models_即使返回内置目录也不能绕过原始响应门禁(self) -> None:
        """网络失败回退到内置目录时，缺少 HTTP 200 必须失败关闭。"""

        completed = subprocess.CompletedProcess(
            args=["codex", "debug", "models"],
            returncode=0,
            stdout=json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-luna",
                            "use_responses_lite": True,
                        }
                    ]
                }
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = root / "relay"
            relay.mkdir()
            output = root / "model-catalog-prewarm.json"
            with (
                mock.patch(
                    "tools.official_client_capture.drive_codex_model_catalog."
                    "subprocess.run",
                    return_value=completed,
                ),
                mock.patch(
                    "tools.official_client_capture.drive_codex_model_catalog."
                    "wait_for_relay_model_catalog",
                    side_effect=ModelCatalogPrewarmError("缺少原始 HTTP 200"),
                ),
            ):
                with self.assertRaisesRegex(ModelCatalogPrewarmError, "缺少原始"):
                    run_prewarm(
                        codex_bin="/opt/codex-0.149.1/bin/codex",
                        codex_version="0.149.1",
                        model="gpt-5.6-luna",
                        expected_lite=True,
                        relay_dir=relay,
                        output=output,
                        timeout=1,
                    )
            self.assertFalse(output.exists())

    def test_等待_relay_响应超时后失败关闭(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = Path(directory) / "relay"
            relay.mkdir()
            with self.assertRaisesRegex(ModelCatalogPrewarmError, "等待 relay"):
                wait_for_relay_model_catalog(
                    relay,
                    expected_model="gpt-5.5",
                    expected_lite=False,
                    deadline=time.monotonic() + 0.01,
                )


if __name__ == "__main__":
    unittest.main()
