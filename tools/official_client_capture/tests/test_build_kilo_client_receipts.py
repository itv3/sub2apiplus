"""Kilo 双协议收据只能承接既有事实，不得编造或放宽。

`capture-candidate seal` 要求五份 Kilo 事实，此前只有 runtime-audit 有产出方，其余四份
没有任何生成入口——candidate seal 因此从未走通。补上的产出方必须守住两条：内容全部来自
服务端记录与客户端安装事实；标识一律内容寻址，同一批事实必然复算出同一份产物。
"""

from __future__ import annotations

import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))

import build_kilo_client_receipts as builder  # noqa: E402


IDENTITY = {
    "campaign_id": "codex-0145-to-0147-20260809T042514Z-k29",
    "attempt_id": "20260809T050204Z-9b00b5337841ae32",
    "run_nonce": "ed357e12eb0ab97a497d423f35cb7d10d1edc9b8886eb6a8f3b60c92675cccce",
    "candidate_id": "candidate-20260809T050149Z-k29-r1",
    "target_version": "0.147.0",
    "profile_id": "codex-0.147.0-official-k29-v1",
    "profile_digest": "0d86e033716ab2b7d2161a7015ad000bc0d7cedfaa9e130342eec4ba0637ef9f",
    "candidate_image_id": "sha256:" + "a" * 64,
    "source_tree_sha256": "b" * 64,
    "build_id": "0.1.171-7-docker",
    "deployed_version": "0.1.171-7",
}

INSTALLATION_FACTS = {
    "executable_path": "/Users/czs/.vscode/extensions/itv3.zlfcode-7.4.2001-darwin-arm64/bin/kilo",
    "executable_sha256": "c2ea83f28ca0a93d2d48b9d1c05fbea707a79a9324442ba83de21ad0d254ea5a",
    "client_version": "7.4.2001",
    "display_name": "ZLF Code",
    "observed_at_utc": "2026-08-09T05:30:00Z",
}

OBSERVATION = {
    "entrypoint": "/v1/chat/completions",
    "user_agent": "Kilo-Code/7.4.2001 ai-sdk/provider-utils/4.0.27 runtime/bun/1.3.14",
    "model": "gpt-5.4",
    "request_id": "1e740daf-03e6-4558-9be9-795d0d8a50d7",
    "response_id": "9436dbc4-2a7d-4746-a2b0-d15369e68bdd",
    "usage_id": "126786",
    "oauth_account_id": 90,
    "http_status": 200,
    "received_at_utc": "2026-08-09T13:22:52.521+08:00",
    "completed_at_utc": "2026-08-09T13:22:52.725+08:00",
    "recorded_at_utc": "2026-08-09T13:22:52.725+08:00",
    "upstream_endpoint": "/v1/responses",
    "transport": "http",
}


def _installation() -> dict:
    return builder.build_installation(**INSTALLATION_FACTS)


class KiloReceiptBuilderTest(unittest.TestCase):
    def test_收据绑定本轮采集坐标(self) -> None:
        receipts = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-compatible",
            observation=OBSERVATION,
            installation=_installation(),
        )
        for kind, payload in receipts.items():
            with self.subTest(kind=kind):
                for field in ("campaign_id", "attempt_id", "run_nonce"):
                    self.assertEqual(payload[field], IDENTITY[field])

    def test_协议与入口取自契约而非观测(self) -> None:
        """入口写错会让收据描述的协议与实际调用不符，必须以契约为准并校验一致。"""

        receipts = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-compatible",
            observation=OBSERVATION,
            installation=_installation(),
        )
        self.assertEqual(receipts["ingress"]["protocol"], "openai-compatible")
        self.assertEqual(receipts["ingress"]["entrypoint"], "/v1/chat/completions")

        mismatched = {**OBSERVATION, "entrypoint": "/v1/responses"}
        with self.assertRaises(builder.KiloReceiptError):
            builder.build_client_receipts(
                identity=IDENTITY,
                client_id="kilo-compatible",
                observation=mismatched,
                installation=_installation(),
            )

    def test_SDK_形态的_UA_回落到安装事实(self) -> None:
        """Responses 的 WS 入口走 OpenAI 官方 JS SDK，UA 描述的是 SDK 而非宿主客户端。

        两条入口用不同 SDK 是 Kilo 的真实行为；此时客户端版本只能来自本机安装事实，
        UA 原文仍如实写进收据备查。既不自报 Kilo 也不是已知 SDK 的一律拒绝。
        """

        receipts = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-responses",
            observation={
                **OBSERVATION,
                "entrypoint": "/v1/responses",
                "user_agent": "OpenAI/JS 6.45.0",
                "transport": "websocket",
            },
            installation=_installation(),
        )
        self.assertEqual(receipts["ingress"]["client_version"], "7.4.2001")

        with self.assertRaises(builder.KiloReceiptError):
            builder.build_client_receipts(
                identity=IDENTITY,
                client_id="kilo-responses",
                observation={
                    **OBSERVATION,
                    "entrypoint": "/v1/responses",
                    "user_agent": "curl/8.1.2",
                },
                installation=_installation(),
            )

    def test_客户端版本必须与安装事实一致(self) -> None:
        """服务端观测到的版本与本机安装文件对不上，收据就不能成立。"""

        drifted = {
            **OBSERVATION,
            "user_agent": "Kilo-Code/7.4.1701 ai-sdk/provider-utils/4.0.27",
        }
        with self.assertRaises(builder.KiloReceiptError):
            builder.build_client_receipts(
                identity=IDENTITY,
                client_id="kilo-compatible",
                observation=drifted,
                installation=_installation(),
            )

    def test_非_2xx_响应不得生成收据(self) -> None:
        failed = {**OBSERVATION, "http_status": 500}
        with self.assertRaises(builder.KiloReceiptError):
            builder.build_client_receipts(
                identity=IDENTITY,
                client_id="kilo-compatible",
                observation=failed,
                installation=_installation(),
            )

    def test_标识内容寻址且可复算(self) -> None:
        """同一批事实重复运行必须得到同一份产物，否则收据无法复核。"""

        first = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-compatible",
            observation=OBSERVATION,
            installation=_installation(),
        )
        second = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-compatible",
            observation=OBSERVATION,
            installation=_installation(),
        )
        self.assertEqual(first, second)
        self.assertEqual(_installation(), _installation())

    def test_事实变化必须改变标识(self) -> None:
        base = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-compatible",
            observation=OBSERVATION,
            installation=_installation(),
        )
        other = builder.build_client_receipts(
            identity={**IDENTITY, "attempt_id": "20260809T999999Z-0000000000000000"},
            client_id="kilo-compatible",
            observation=OBSERVATION,
            installation=_installation(),
        )
        self.assertNotEqual(
            base["ingress"]["witness_id"], other["ingress"]["witness_id"]
        )
        self.assertNotEqual(base["usage"]["event_id"], other["usage"]["event_id"])

    def test_时间统一归一到_UTC(self) -> None:
        """服务端记录带本地偏移；收据混用偏移会让时序核对失去意义。"""

        receipts = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-compatible",
            observation=OBSERVATION,
            installation=_installation(),
        )
        self.assertTrue(receipts["ingress"]["received_at_utc"].endswith("Z"))
        self.assertEqual(
            receipts["response"]["completed_at_utc"], "2026-08-09T05:22:52.725Z"
        )

    def test_安装事实要求绝对路径与内容摘要(self) -> None:
        with self.assertRaises(builder.KiloReceiptError):
            builder.build_installation(
                **{**INSTALLATION_FACTS, "executable_path": "bin/kilo"}
            )
        with self.assertRaises(builder.KiloReceiptError):
            builder.build_installation(
                **{**INSTALLATION_FACTS, "executable_sha256": "not-a-digest"}
            )

    def test_WebSocket_升级的_101_是成功状态(self) -> None:
        """Kilo 的 Responses 入口走 WS，101 是该链路成功的唯一正确状态码。"""

        receipts = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-responses",
            observation={
                **OBSERVATION,
                "entrypoint": "/v1/responses",
                "http_status": 101,
                "transport": "websocket",
            },
            installation=_installation(),
        )
        self.assertEqual(receipts["response"]["http_status"], 101)

        for rejected in (100, 102, 302, 500):
            with self.subTest(status=rejected):
                with self.assertRaises(builder.KiloReceiptError):
                    builder.build_client_receipts(
                        identity=IDENTITY,
                        client_id="kilo-responses",
                        observation={
                            **OBSERVATION,
                            "entrypoint": "/v1/responses",
                            "http_status": rejected,
                        },
                        installation=_installation(),
                    )

    def test_两个客户端产出不同的入口契约(self) -> None:
        responses = builder.build_client_receipts(
            identity=IDENTITY,
            client_id="kilo-responses",
            observation={**OBSERVATION, "entrypoint": "/v1/responses", "transport": "websocket"},
            installation=_installation(),
        )
        self.assertEqual(responses["ingress"]["protocol"], "openai-responses")
        self.assertEqual(responses["ingress"]["entrypoint"], "/v1/responses")


if __name__ == "__main__":
    unittest.main()
