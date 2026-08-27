"""官方在线模型目录预热驱动的离线契约测试。"""

from __future__ import annotations

import json
import os
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
    build_online_model_catalog_command,
    find_captured_model_catalog,
    run_prewarm,
    start_online_model_catalog_refresh,
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
    def test_01491_正式场景使用临时_MITM_根并原子安装唯一制品(self) -> None:
        """补采不得把重试日志、生命周期或半成品直接写入冻结场景根。"""

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
        self.assertIn('CAPTURE_OUTPUT_DIR="$candidate_output_dir"', source)
        self.assertIn('--mitm-models-http "$candidate_models_file"', source)
        self.assertIn("thread_start_driver_status", source)
        self.assertIn('HOME="$home"', source)
        self.assertIn('CODEX_HOME="$home"', source)
        self.assertIn(
            'install -m 0600 "$candidate_models_file" "$staged_models_file"',
            source,
        )
        self.assertIn('mv -n -- "$staged_models_file" "$models_file"', source)
        self.assertIn('test ! -e "$models_file"', source)
        self.assertNotIn('CAPTURE_OUTPUT_DIR="$output_dir"', source)
        self.assertNotIn('>"$output_dir/model-prewarm-mitmdump.log"', source)
        self.assertNotIn('>>"$output_dir/model-prewarm-driver.log"', source)
        rendered = source.format(
            capture_root="/capture",
            campaign_id="c1491-r14-contract",
            repo_root="/repo",
            capture_codex_bin="/opt/codex-0.149.1/bin/codex",
            target_version="0.149.1",
            model="gpt-5.5",
        )
        syntax = subprocess.run(
            ["bash", "-n"],
            input=rendered,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_预热脚本绑定隔离_HOME_与_relay_原文(self) -> None:
        """模型条件任务必须先取得在线 200，不能依赖退出前的后台竞速请求。"""

        tool_root = Path(__file__).parents[1]
        source = (tool_root / "run_official_relay_scenario.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("drive_codex_model_catalog.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("prewarm_model_catalog", source)
        self.assertIn(
            'docker exec -e HOME="$home" -e CODEX_HOME="$home"',
            source,
        )
        self.assertIn("drive_codex_model_catalog.py", source)
        self.assertIn('--relay-dir "/capture/runs/$run_id/relay"', source)
        self.assertIn(
            '--model-catalog-prewarm "$work_dir/model-catalog-prewarm.json"',
            source,
        )
        self.assertIn("for attempt in 1 2 3", source)
        self.assertIn("--preconnect-upstream --preconnect-timeout 15", source)
        self.assertIn('relay/preconnect-ready.json', source)
        self.assertIn("模型目录上游 TLS 预连接未在 20 秒内就绪", source)
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

    def test_驱动固定使用内置_provider_的最小_app_server(self) -> None:
        """冷 HOME 必须保留内置 provider，并禁用无关插件和应用。"""

        command = build_online_model_catalog_command(
            "/opt/codex-0.149.1/bin/codex",
            "0.149.1",
        )
        self.assertEqual(
            command[:4],
            [
                "/opt/codex-0.149.1/bin/codex",
                "app-server",
                "--strict-config",
                "--stdio",
            ],
        )
        self.assertFalse(
            any("model_provider" in argument for argument in command),
            "app-server 必须保留内置 openai provider",
        )
        self.assertIn("features.plugins=false", command)
        self.assertIn("features.apps=false", command)
        self.assertNotIn("turn/start", command)

    def test_驱动只执行_thread_start_且不发送_turn(self) -> None:
        client = mock.Mock()
        client.wait_response.side_effect = [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
        ]
        thread_id = start_online_model_catalog_refresh(
            client,
            model="gpt-5.5",
            deadline=time.monotonic() + 1,
        )
        methods = [call.args[0]["method"] for call in client.send.call_args_list]
        self.assertEqual(methods, ["initialize", "initialized", "thread/start"])
        self.assertNotIn("turn/start", methods)
        self.assertEqual(thread_id, "thread-1")

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

            result, model_count = find_captured_model_catalog(
                relay,
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            self.assertEqual(result["connection_id"], 1)
            self.assertTrue(result["use_responses_lite"])
            self.assertEqual(model_count, 1)

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
        """thread/start 完成后仍必须等待 MITM 刷盘真实 HTTP 200。"""

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
            result, model_count = wait_for_mitm_model_catalog(
                models_http,
                expected_model="gpt-5.5",
                expected_lite=False,
                deadline=time.monotonic() + 1,
            )
            writer.join()
            self.assertEqual(result["source"], "mitm_models_http")
            self.assertEqual(result["path"], str(models_http))
            self.assertEqual(model_count, 1)

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
            result, model_count = wait_for_relay_model_catalog(
                relay,
                expected_model="gpt-5.6-luna",
                expected_lite=True,
                deadline=time.monotonic() + 1,
            )
            writer.join()
            self.assertEqual(result["connection_id"], 1)
            self.assertEqual(result["response_path"], "relay/conn001.upstream_to_client.bin")
            self.assertEqual(model_count, 1)

    def test_thread_start_成功且原始_HTTP_200_完整时才写摘要(self) -> None:
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
            client = mock.Mock()
            client.records = [
                {"id": 1, "result": {}},
                {"method": "thread/started"},
                {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            ]
            client.wait_response.side_effect = [
                client.records[0],
                client.records[2],
            ]
            output = root / "model-catalog-prewarm.json"
            with (
                mock.patch.dict(
                    os.environ,
                    {"OPENAI_API_KEY": "secret", "SUB2API_API_KEY": "secret"},
                ),
                mock.patch(
                    "tools.official_client_capture.drive_codex_model_catalog."
                    "AppServerClient",
                    return_value=client,
                ) as client_factory,
            ):
                summary = run_prewarm(
                    codex_bin="/opt/codex-0.149.1/bin/codex",
                    codex_version="0.149.1",
                    model="gpt-5.6-luna",
                    expected_lite=True,
                    relay_dir=relay,
                    output=output,
                    timeout=1,
                )
            command, environment = client_factory.call_args.args
            self.assertEqual(command[1:4], ["app-server", "--strict-config", "--stdio"])
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("SUB2API_API_KEY", environment)
            methods = [call.args[0]["method"] for call in client.send.call_args_list]
            self.assertEqual(methods, ["initialize", "initialized", "thread/start"])
            self.assertNotIn("turn/start", methods)
            self.assertEqual(summary["protocol_record_count"], 3)
            self.assertEqual(summary["model_count"], 1)
            self.assertTrue(output.is_file())
            client.close.assert_called_once_with()

    def test_thread_start_不能绕过原始响应门禁(self) -> None:
        """即使线程创建成功，缺少 /models HTTP 200 也必须失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = root / "relay"
            relay.mkdir()
            output = root / "model-catalog-prewarm.json"
            client = mock.Mock()
            client.records = [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            ]
            client.wait_response.side_effect = client.records
            with (
                mock.patch(
                    "tools.official_client_capture.drive_codex_model_catalog."
                    "AppServerClient",
                    return_value=client,
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
            client.close.assert_called_once_with()

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
