"""direct 证据提取与结构比较测试。"""

from __future__ import annotations

import subprocess
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tools.official_client_capture.capturelib.analysis import (
    TSHARK_CLIENT_HELLO_FIELDS,
    compare_normalized,
    compare_official_egress_contract,
    extract_client_hellos,
    normalize_mitm_record,
    validate_tshark_client_hello_fields,
)


class AnalysisTest(unittest.TestCase):
    def test_normalization_removes_http_and_websocket_query_values(self) -> None:
        http = normalize_mitm_record(
            {
                "request": {
                    "path": "/v1/messages?api_key=CANARY-SECRET&mode=fast",
                }
            }
        )
        websocket = normalize_mitm_record(
            {
                "_websocket": True,
                "path": "/v1/responses?token=CANARY-SECRET",
            }
        )
        self.assertNotIn("CANARY-SECRET", str(http))
        self.assertNotIn("CANARY-SECRET", str(websocket))
        self.assertIn("api_key", http["request"]["path"])
        self.assertIn("mode", http["request"]["path"])

    def test_tshark_uses_tab_separator_matching_parser(self) -> None:
        output = (
            "1\t203.0.113.10\t\t443\tapi.anthropic.com\t4865,4866"
            "\t0,11,16\th2,http/1.1\t29,23\t0\t1027,2052"
            "\t772,771\t29\t1\n"
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr=""
        )
        with patch(
            "tools.official_client_capture.capturelib.analysis.subprocess.run",
            return_value=completed,
        ) as run:
            records = extract_client_hellos(
                pcap_path=Path("/capture/traffic.pcap"),
                target_hosts=("api.anthropic.com",),
                tshark_bin="/usr/bin/tshark",
            )

        command = run.call_args.args[0]
        self.assertIn("separator=/t", command)
        self.assertNotIn("separator=\\t", command)
        self.assertEqual(records[0]["sni"], "api.anthropic.com")
        self.assertEqual(records[0]["cipher_suites"], ["4865", "4866"])
        self.assertEqual(records[0]["curves"], ["29", "23"])
        self.assertEqual(records[0]["point_formats"], ["0"])
        self.assertEqual(records[0]["signature_algorithms"], ["1027", "2052"])
        self.assertEqual(records[0]["supported_versions"], ["772", "771"])
        self.assertEqual(records[0]["key_share_groups"], ["29"])
        self.assertEqual(records[0]["psk_modes"], ["1"])
        for field in TSHARK_CLIENT_HELLO_FIELDS:
            self.assertIn(field, command)

    def test_tshark_field_preflight_fails_before_capture_when_incomplete(self) -> None:
        complete = "\n".join(
            f"F\tlabel\t{field}\tFT_NONE\ttls" for field in TSHARK_CLIENT_HELLO_FIELDS
        )
        with patch(
            "tools.official_client_capture.capturelib.analysis.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, complete, ""),
        ):
            self.assertEqual(
                validate_tshark_client_hello_fields(
                    tshark_bin="/usr/bin/tshark", environment={}
                ),
                TSHARK_CLIENT_HELLO_FIELDS,
            )

        incomplete = complete.replace("tls.extension.psk_ke_mode", "missing.field")
        with patch(
            "tools.official_client_capture.capturelib.analysis.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, incomplete, ""),
        ), self.assertRaises(RuntimeError):
            validate_tshark_client_hello_fields(
                tshark_bin="/usr/bin/tshark", environment={}
            )

    def test_direct_compare_uses_tls_shape_not_runtime_location(self) -> None:
        baseline = {
            "client_hellos": [
                {
                    "frame": "1",
                    "destination": "203.0.113.10",
                    "port": "443",
                    "sni": "api.anthropic.com",
                    "cipher_suites": ["4865", "4866"],
                    "extension_types": ["0", "11", "16"],
                    "alpn": ["h2", "http/1.1"],
                    "curves": ["29", "23"],
                    "point_formats": ["0"],
                    "signature_algorithms": ["1027", "2052"],
                    "supported_versions": ["772", "771"],
                    "key_share_groups": ["29"],
                    "psk_modes": ["1"],
                }
            ]
        }
        candidate = {
            "client_hellos": [
                {
                    "frame": "99",
                    "destination": "2001:db8::10",
                    "port": "443",
                    "sni": "api.anthropic.com",
                    "cipher_suites": ["4865", "4866"],
                    "extension_types": ["0", "11", "16"],
                    "alpn": ["h2", "http/1.1"],
                    "curves": ["29", "23"],
                    "point_formats": ["0"],
                    "signature_algorithms": ["1027", "2052"],
                    "supported_versions": ["772", "771"],
                    "key_share_groups": ["29"],
                    "psk_modes": ["1"],
                }
            ]
        }
        result = compare_normalized(baseline, candidate)
        self.assertTrue(result["equal"])
        self.assertEqual(result["baseline_evidence_kind"], "direct_tls")

        candidate["client_hellos"][0]["alpn"] = ["http/1.1"]
        self.assertFalse(compare_normalized(baseline, candidate)["equal"])
        candidate["client_hellos"][0]["alpn"] = ["h2", "http/1.1"]
        candidate["client_hellos"][0]["curves"] = ["29"]
        self.assertFalse(compare_normalized(baseline, candidate)["equal"])

    def test_direct_compare_normalizes_grease_values(self) -> None:
        baseline = {
            "client_hellos": [
                {
                    "port": "443",
                    "sni": "chatgpt.com",
                    "cipher_suites": ["0x0a0a", "4865"],
                    "extension_types": ["2570", "16"],
                    "alpn": ["h2"],
                }
            ]
        }
        candidate = {
            "client_hellos": [
                {
                    "port": "443",
                    "sni": "CHATGPT.COM",
                    "cipher_suites": ["0x2a2a", "4865"],
                    "extension_types": ["10794", "16"],
                    "alpn": ["h2"],
                }
            ]
        }
        self.assertTrue(compare_normalized(baseline, candidate)["equal"])

    def test_direct_compare_deduplicates_retries_but_reports_observations(self) -> None:
        hello = {
            "cipher_suites": ["4865"],
            "extension_types": ["0", "16"],
            "alpn": ["h2"],
        }
        result = compare_normalized(
            {"client_hellos": [hello]}, {"client_hellos": [hello, hello]}
        )
        self.assertTrue(result["equal"])
        self.assertEqual(result["baseline_observation_count"], 1)
        self.assertEqual(result["candidate_observation_count"], 2)

    def test_mitm_compare_uses_only_canonical_client_request(self) -> None:
        request = {
            "method": "POST",
            "host": "<target-host>",
            "path": "/v1/responses",
            "http_version": "HTTP/2",
            "headers": [["user-agent", "codex_cli_rs/0.145.0"]],
            "body_length": 100,
            "json_shape": {"model": "gpt-test"},
        }
        baseline = {
            "records": [
                {
                    "kind": "http_exchange",
                    "task": "oauth",
                    "boundary": "official",
                    "subject": "codex-http",
                    "request": request,
                    "response": {"status": 200},
                },
                {
                    "kind": "websocket_frame",
                    "from_client": False,
                    "json_shape": {"server": "one"},
                },
            ]
        }
        candidate_request = dict(request, body_length=999)
        candidate = {
            "records": [
                {
                    "kind": "http_exchange",
                    "task": "api",
                    "boundary": "sub2api",
                    "subject": "other-subject",
                    "request": candidate_request,
                    "response": {"status": 503},
                },
                {
                    "kind": "websocket_frame",
                    "from_client": False,
                    "json_shape": {"server": "two"},
                },
            ]
        }
        self.assertTrue(compare_normalized(baseline, candidate)["equal"])

    def test_oauth_claude_contract_uses_paired_ingress_for_conversation(self) -> None:
        headers = [
            ["user-agent", "claude-cli/2.1.220 (external, sdk-cli)"],
            ["content-length", "<dynamic>"],
        ]
        baseline = {
            "records": [
                _http_record(
                    "/v1/messages",
                    headers,
                    {
                        "model": "claude-sonnet-5",
                        "system": [{"type": "text", "text": "<text:12>"}],
                        "messages": [
                            {
                                "role": "assistant",
                                "content": [{"type": "tool_use", "id": "<dynamic:str>"}],
                            }
                        ],
                    },
                    "HTTP/1.1",
                )
            ]
        }
        candidate_shape = {
            "model": "claude-sonnet-5",
            "system": [{"type": "text", "text": "<text:99>"}],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "<text:123>"},
                        {"type": "tool_use", "id": "<dynamic:str>"},
                    ],
                }
            ],
        }
        candidate = {
            "records": [
                _http_record(
                    "/v1/messages", headers, candidate_shape, "HTTP/1.1"
                )
            ]
        }
        ingress = {
            "records": [
                _http_record(
                    "/v1/messages", [], candidate_shape, "HTTP/1.1"
                )
            ]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-claude-http"
        )
        self.assertFalse(result["raw_equal"])
        self.assertTrue(result["contract_equal"])
        self.assertTrue(result["candidate_semantic_preserved"])

    def test_oauth_codex_http_contract_declares_cookie_but_rejects_semantic_loss(
        self,
    ) -> None:
        baseline_shape = {
            "model": "gpt-5.6-luna",
            "store": False,
            "input": [{"type": "message"}, {"type": "message"}],
        }
        candidate_shape = {
            "model": "gpt-5.6-luna",
            "store": False,
            "input": [{"type": "message"}],
        }
        baseline = {
            "records": [
                _http_record(
                    "/backend-api/codex/responses",
                    [
                        ["user-agent", "codex_cli_rs/0.145.0"],
                        ["cookie", "<secret>"],
                    ],
                    baseline_shape,
                    "HTTP/2",
                )
            ]
        }
        candidate = {
            "records": [
                _http_record(
                    "/backend-api/codex/responses",
                    [["user-agent", "codex_cli_rs/0.145.0"]],
                    candidate_shape,
                    "HTTP/2",
                )
            ]
        }
        ingress = {
            "records": [
                _http_record(
                    "/v1/responses", [], deepcopy(candidate_shape), "HTTP/2"
                )
            ]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-http"
        )
        self.assertTrue(result["contract_equal"])
        self.assertIn(
            "runtime_cookie_jar",
            {item["kind"] for item in result["declared_differences"]},
        )

        ingress["records"][0]["request"]["json_shape"]["input"].append(
            {"type": "message"}
        )
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-http"
        )
        self.assertFalse(result["contract_equal"])
        self.assertFalse(result["candidate_semantic_preserved"])

    def test_oauth_codex_ws_contract_requires_business_item_turn_metadata(
        self,
    ) -> None:
        prewarm = {
            "type": "response.create",
            "generate": False,
            "input": [
                {"type": "additional_tools", "role": "developer"},
                {"type": "message", "role": "developer"},
            ],
            "client_metadata": {"turn_id": "<dynamic:str>"},
        }
        official_business = {
            "type": "response.create",
            "input": [
                _ws_item("message", "developer"),
                _ws_item("message", "user"),
            ],
            "client_metadata": {"turn_id": "<dynamic:str>"},
        }
        candidate_business = {
            "type": "response.create",
            "input": [_ws_item("message", "user")],
            "client_metadata": {"turn_id": "<dynamic:str>"},
        }
        ingress_business = {
            "type": "response.create",
            "input": [{"type": "message", "role": "user"}],
            "client_metadata": {"turn_id": "<dynamic:str>"},
        }
        baseline = {
            "records": [
                _ws_record("/backend-api/codex/responses", prewarm),
                _ws_record("/backend-api/codex/responses", official_business),
            ]
        }
        candidate = {
            "records": [
                _ws_record("/backend-api/codex/responses", prewarm),
                _ws_record("/backend-api/codex/responses", candidate_business),
            ]
        }
        ingress = {
            "records": [
                _ws_record("/v1/responses", prewarm),
                _ws_record("/v1/responses", ingress_business),
            ]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-ws"
        )
        self.assertTrue(result["contract_equal"])
        self.assertTrue(result["ws_item_turn_metadata_valid"])

        del candidate_business["input"][0][
            "internal_chat_message_metadata_passthrough"
        ]
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-ws"
        )
        self.assertFalse(result["contract_equal"])
        self.assertFalse(result["ws_item_turn_metadata_valid"])

    def test_oauth_contract_rejects_unapproved_static_header_difference(self) -> None:
        shape = {"model": "claude-sonnet-5", "messages": []}
        baseline = {
            "records": [
                _http_record(
                    "/v1/messages",
                    [["user-agent", "claude-cli/2.1.220"]],
                    shape,
                    "HTTP/1.1",
                )
            ]
        }
        candidate = {
            "records": [
                _http_record(
                    "/v1/messages",
                    [["user-agent", "unapproved-client/1.0"]],
                    shape,
                    "HTTP/1.1",
                )
            ]
        }
        ingress = {
            "records": [_http_record("/v1/messages", [], shape, "HTTP/1.1")]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-claude-http"
        )
        self.assertFalse(result["contract_equal"])
        self.assertTrue(result["candidate_semantic_preserved"])

    def test_compare_rejects_mixed_evidence_kinds(self) -> None:
        result = compare_normalized({"records": []}, {"client_hellos": []})
        self.assertFalse(result["equal"])

    def test_compare_rejects_unknown_documents(self) -> None:
        self.assertFalse(compare_normalized({}, {})["equal"])


def _http_record(
    path: str,
    headers: list[list[str]],
    shape: dict[str, object],
    http_version: str,
) -> dict[str, object]:
    return {
        "kind": "http_exchange",
        "request": {
            "method": "POST",
            "host": "<target-host>",
            "path": path,
            "http_version": http_version,
            "headers": headers,
            "json_shape": shape,
        },
        "response": {"status": 200},
    }


def _ws_record(path: str, shape: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "websocket_frame",
        "from_client": True,
        "host": "<target-host>",
        "path": path,
        "json_shape": shape,
    }


def _ws_item(item_type: str, role: str) -> dict[str, object]:
    return {
        "type": item_type,
        "role": role,
        "internal_chat_message_metadata_passthrough": {
            "turn_id": "<dynamic:str>"
        },
    }


if __name__ == "__main__":
    unittest.main()
