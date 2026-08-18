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
    compare_official_egress_tls_contract,
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
                },
                "response": {"headers": [["Set-Cookie", "session=secret"]]},
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
        self.assertEqual(http["response"]["headers"][0][0], "set-cookie")
        self.assertNotIn("session=secret", str(http["response"]["headers"]))

    def test_all_host_inventory_preserves_observed_host_and_transport(self) -> None:
        record = normalize_mitm_record(
            {
                "_capture_host_scope": "all",
                "request": {
                    "method": "POST",
                    "scheme": "https",
                    "host": "API.Anthropic.COM",
                    "port": 443,
                    "path": "/v1/messages",
                },
            }
        )
        self.assertEqual(record["capture_host_scope"], "all")
        self.assertEqual(record["request"]["host"], "api.anthropic.com")
        self.assertEqual(record["request"]["scheme"], "https")
        self.assertEqual(record["request"]["port"], 443)

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

    def test_direct_ws_contract_separates_auxiliary_http_and_random_order(self) -> None:
        http_profile = _tls_profile(30, ["0", "11", "16"], [])
        ws_profile = _tls_profile(10, ["0", "11", "16"], [])
        candidate_ws = deepcopy(ws_profile)
        candidate_ws["extension_types"] = ["16", "0", "11"]
        baseline = {"client_hellos": [http_profile, ws_profile]}
        candidate = {"client_hellos": [candidate_ws]}

        result = compare_official_egress_tls_contract(
            baseline, candidate, "codex-ws"
        )
        self.assertTrue(result["equal"])
        self.assertEqual(result["baseline_auxiliary_observation_count"], 1)
        self.assertEqual(result["candidate_auxiliary_observation_count"], 0)

        candidate_ws["cipher_suites"][0] = "0xffff"
        result = compare_official_egress_tls_contract(
            baseline, candidate, "codex-ws"
        )
        self.assertFalse(result["equal"])

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

    def test_oauth_codex_http_contract_requires_cookie_and_rejects_semantic_loss(
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
                    [
                        ["user-agent", "codex_cli_rs/0.145.0"],
                        ["cookie", "<secret>"],
                    ],
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

        candidate["records"][0]["request"]["headers"] = [
            ["user-agent", "codex_cli_rs/0.145.0"]
        ]
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-http"
        )
        self.assertFalse(result["contract_equal"])

        candidate["records"][0]["request"]["headers"].append(
            ["cookie", "<secret>"]
        )

        ingress["records"][0]["request"]["json_shape"]["input"].append(
            {"type": "message"}
        )
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-http"
        )
        self.assertFalse(result["contract_equal"])
        self.assertFalse(result["candidate_semantic_preserved"])

    def test_oauth_codex_contract_distinguishes_semantics_from_profile(self) -> None:
        original = {
            "model": "gpt-5.5",
            "input": [{"type": "message", "role": "user"}],
            "tools": [{"type": "function", "name": "lookup"}],
            "parallel_tool_calls": True,
            "store": True,
            "stream": False,
            "max_output_tokens": 2048,
            "reasoning": {"effort": "high"},
            "text": {"verbosity": "high"},
            "tool_choice": "required",
            "include": ["file_search_call.results"],
        }
        mutations = {
            "tools": ([], False),
            "parallel_tool_calls": (False, True),
            "store": (False, True),
            "stream": (True, True),
            "max_output_tokens": (None, True),
            "reasoning": ({"effort": "low", "context": "all_turns"}, False),
            "reasoning_context": ({"effort": "high", "context": "all_turns"}, True),
            "text": ({"verbosity": "low"}, False),
            "tool_choice": ("auto", True),
            "include": (["reasoning.encrypted_content"], True),
        }
        for field, (replacement, expected_semantic) in mutations.items():
            with self.subTest(field=field):
                egress_shape = deepcopy(original)
                target_field = "reasoning" if field == "reasoning_context" else field
                if replacement is None:
                    del egress_shape[target_field]
                else:
                    egress_shape[target_field] = replacement
                baseline = {
                    "records": [
                        _http_record(
                            "/backend-api/codex/responses",
                            [],
                            deepcopy(egress_shape),
                            "HTTP/2",
                        )
                    ]
                }
                candidate = deepcopy(baseline)
                ingress = {
                    "records": [
                        _http_record(
                            "/v1/responses", [], deepcopy(original), "HTTP/2"
                        )
                    ]
                }

                result = compare_official_egress_contract(
                    baseline, candidate, ingress, "oauth-codex-http"
                )
                self.assertEqual(
                    expected_semantic,
                    result["candidate_semantic_preserved"],
                )
                self.assertEqual(expected_semantic, result["contract_equal"])

    def test_oauth_codex_http_allows_independent_tools_but_preserves_candidate(self) -> None:
        baseline_shape = {
            "model": "gpt-5.4",
            "input": [{"type": "message"}],
            "tools": [{"type": "function", "name": "official_tool"}],
        }
        candidate_shape = {
            "model": "gpt-5.4",
            "input": [{"type": "message"}],
            "tools": [{"type": "function", "name": "candidate_tool"}],
        }
        baseline = {
            "records": [
                _http_record(
                    "/backend-api/codex/responses", [], baseline_shape, "HTTP/2"
                )
            ]
        }
        candidate = {
            "records": [
                _http_record(
                    "/backend-api/codex/responses", [], candidate_shape, "HTTP/2"
                )
            ]
        }
        ingress = {
            "records": [
                _http_record("/v1/responses", [], deepcopy(candidate_shape), "HTTP/2")
            ]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-http"
        )
        self.assertTrue(result["contract_equal"])
        self.assertTrue(result["candidate_semantic_preserved"])
        self.assertIn(
            "independent_client_tool_catalog",
            {item["kind"] for item in result["declared_differences"]},
        )

        ingress["records"][0]["request"]["json_shape"]["tools"] = []
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-http"
        )
        self.assertFalse(result["contract_equal"])
        self.assertFalse(result["candidate_semantic_preserved"])

    def test_oauth_codex_http_contract_accepts_proven_cold_cookie_jar(self) -> None:
        shape = {
            "model": "gpt-5.6-luna",
            "store": False,
            "input": [{"type": "message"}],
        }
        baseline = {
            "records": [
                _http_record(
                    "/backend-api/codex/responses",
                    [["cookie", "<secret>"]],
                    deepcopy(shape),
                    "HTTP/2",
                )
                for _ in range(2)
            ]
        }
        candidate = {
            "records": [
                _http_record(
                    "/backend-api/codex/responses", [], deepcopy(shape), "HTTP/2"
                ),
                _http_record(
                    "/backend-api/codex/responses",
                    [["cookie", "<secret>"]],
                    deepcopy(shape),
                    "HTTP/2",
                ),
            ]
        }
        candidate["records"][0]["response"]["headers"] = [
            ["set-cookie", "<secret>"]
        ]
        ingress = {
            "records": [
                _http_record("/v1/responses", [], deepcopy(shape), "HTTP/2")
                for _ in range(2)
            ]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-http"
        )
        self.assertTrue(result["contract_equal"])
        self.assertIn(
            "cold_cookie_jar_bootstrap",
            {item["kind"] for item in result["declared_differences"]},
        )

        candidate["records"][0]["response"]["headers"] = []
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-http"
        )
        self.assertFalse(result["contract_equal"])

    def test_oauth_codex_compact_contract_covers_identity_and_body(self) -> None:
        shape = {
            "model": "gpt-5.5",
            "input": [{"type": "message", "role": "user"}],
            "prompt_cache_key": "<dynamic:str>",
        }
        headers = [
            ["user-agent", "codex_exec/0.145.0"],
            ["x-codex-installation-id", "<dynamic:str>"],
        ]
        baseline = {
            "records": [
                _http_record(
                    "/backend-api/codex/responses/compact",
                    headers,
                    shape,
                    "HTTP/2",
                )
            ]
        }
        candidate = deepcopy(baseline)
        ingress = {
            "records": [
                _http_record(
                    "/v1/responses/compact",
                    [],
                    {"model": "gpt-5.5", "input": deepcopy(shape["input"])},
                    "HTTP/2",
                )
            ]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-compact-http"
        )
        self.assertTrue(result["contract_equal"])

        del candidate["records"][0]["request"]["json_shape"]["prompt_cache_key"]
        candidate["records"][0]["request"]["headers"] = [headers[0]]
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-compact-http"
        )
        self.assertFalse(result["contract_equal"])

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
            ],
            "turn_state_lifecycle": {
                "response_state_count": 1,
                "matched_client_frame_count": 1,
                "unmatched_client_frame_count": 0,
            },
        }
        candidate = {
            "records": [
                _ws_record("/backend-api/codex/responses", prewarm),
                _ws_record("/backend-api/codex/responses", candidate_business),
            ],
            "turn_state_lifecycle": {
                "response_state_count": 1,
                "matched_client_frame_count": 1,
                "unmatched_client_frame_count": 0,
            },
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

    def test_oauth_codex_ws_contract_declares_independent_response_lineage(self) -> None:
        baseline_business = {
            "type": "response.create",
            "input": [_ws_item("message", "user")],
        }
        candidate_business = deepcopy(baseline_business)
        candidate_business["previous_response_id"] = "<dynamic:str>"
        ingress_business = deepcopy(candidate_business)
        baseline = {
            "records": [
                _ws_record("/backend-api/codex/responses", baseline_business)
            ],
            "turn_state_lifecycle": {
                "response_state_count": 1,
                "matched_client_frame_count": 1,
                "unmatched_client_frame_count": 0,
            },
        }
        candidate = {
            "records": [
                _ws_record("/backend-api/codex/responses", candidate_business)
            ],
            "turn_state_lifecycle": {
                "response_state_count": 1,
                "matched_client_frame_count": 1,
                "unmatched_client_frame_count": 0,
            },
        }
        ingress = {
            "records": [_ws_record("/v1/responses", ingress_business)]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-ws"
        )
        self.assertTrue(result["contract_equal"])
        self.assertTrue(result["candidate_semantic_preserved"])
        self.assertIn(
            "independent_response_lineage",
            {item["kind"] for item in result["declared_differences"]},
        )

        candidate["turn_state_lifecycle"]["matched_client_frame_count"] = 0
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-ws"
        )
        self.assertFalse(result["contract_equal"])
        self.assertFalse(result["ws_turn_state_lifecycle_valid"])

    def test_oauth_codex_ws_contract_accepts_mutual_absence_of_turn_state(self) -> None:
        shape = {
            "type": "response.create",
            "input": [_ws_item("message", "user")],
        }
        lifecycle = {
            "response_state_count": 0,
            "matched_client_frame_count": 0,
            "unmatched_client_frame_count": 0,
        }
        baseline = {
            "records": [_ws_record("/backend-api/codex/responses", shape)],
            "turn_state_lifecycle": deepcopy(lifecycle),
        }
        candidate = {
            "records": [_ws_record("/backend-api/codex/responses", shape)],
            "turn_state_lifecycle": deepcopy(lifecycle),
        }
        ingress = {"records": [_ws_record("/v1/responses", shape)]}

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-codex-ws"
        )
        self.assertTrue(result["contract_equal"])
        self.assertTrue(result["ws_turn_state_lifecycle_valid"])

    def test_oauth_claude_contract_rejects_system_semantic_loss(self) -> None:
        ingress_shape = {
            "model": "claude-sonnet-5",
            "system": [{"type": "text", "text": "<text:17>"}],
            "messages": [{"role": "user", "content": "<text:5>"}],
        }
        egress_shape = {
            "model": "claude-sonnet-5",
            "system": [
                {"type": "text", "text": "<text:80>"},
                {"type": "text", "text": "<text:70>"},
                {"type": "text", "text": "<text:90>"},
            ],
            "messages": [{"role": "user", "content": "<text:5>"}],
        }
        baseline = {
            "records": [
                _http_record("/v1/messages", [], deepcopy(egress_shape), "HTTP/1.1")
            ]
        }
        candidate = deepcopy(baseline)
        ingress = {
            "records": [
                _http_record("/v1/messages", [], ingress_shape, "HTTP/1.1")
            ]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-claude-http"
        )
        self.assertFalse(result["contract_equal"])
        self.assertFalse(result["candidate_semantic_preserved"])

        candidate["records"][0]["request"]["json_shape"]["messages"].insert(
            0,
            {"role": "user", "content": [{"type": "text", "text": "<text:17>"}]},
        )
        ingress["records"][0]["request"]["json_shape"]["messages"].insert(
            0,
            {"role": "user", "content": [{"type": "text", "text": "<text:17>"}]},
        )
        result = compare_official_egress_contract(
            candidate, candidate, ingress, "oauth-claude-http"
        )
        self.assertTrue(result["candidate_semantic_preserved"])

    def test_oauth_claude_system_reassembles_split_marker_without_block_bypass(
        self,
    ) -> None:
        marker = "# Text output (does not apply to tool calls)"

        def normalized(payload: dict) -> dict:
            return normalize_mitm_record(
                {
                    "request": {
                        "method": "POST",
                        "host": "api.anthropic.com",
                        "path": "/v1/messages",
                        "http_version": "HTTP/1.1",
                        "headers": [],
                        "body": {"json": payload},
                    },
                    "response": {},
                }
            )

        ingress_payload = {
            "model": "claude-sonnet-5",
            "system": [
                {"type": "text", "text": "billing"},
                {"type": "text", "text": "identity"},
                {"type": "text", "text": f"global\n\n{marker}\nlocal"},
            ],
            "messages": [],
        }
        egress_payload = deepcopy(ingress_payload)
        egress_payload["system"] = [
            {"type": "text", "text": "billing"},
            {"type": "text", "text": "identity"},
            {"type": "text", "text": "global"},
            {"type": "text", "text": f"{marker}\nlocal"},
        ]
        candidate_record = normalized(egress_payload)
        result = compare_official_egress_contract(
            {"records": [candidate_record]},
            {"records": [deepcopy(candidate_record)]},
            {"records": [normalized(ingress_payload)]},
            "oauth-claude-http",
        )
        self.assertTrue(result["candidate_semantic_preserved"])

        broken = deepcopy(candidate_record)
        broken["request"]["semantic_summary"]["anthropic_system"][
            "official_profile_tail_digest"
        ] = "0" * 64
        result = compare_official_egress_contract(
            {"records": [candidate_record]},
            {"records": [broken]},
            {"records": [normalized(ingress_payload)]},
            "oauth-claude-http",
        )
        self.assertFalse(result["candidate_semantic_preserved"])

    def test_oauth_claude_three_block_third_party_system_moves_to_messages(
        self,
    ) -> None:
        marker = "# Text output (does not apply to tool calls)"

        def normalized(payload: dict) -> dict:
            return normalize_mitm_record(
                {
                    "request": {
                        "method": "POST",
                        "path": "/v1/messages",
                        "http_version": "HTTP/1.1",
                        "headers": [],
                        "body": {"json": payload},
                    },
                    "response": {},
                }
            )

        ingress_payload = {
            "model": "claude-sonnet-5",
            "system": [
                {"type": "text", "text": "part-a"},
                {"type": "text", "text": "part-b"},
                {"type": "text", "text": "part-c"},
            ],
            "messages": [{"role": "user", "content": "hello"}],
        }
        egress_payload = {
            "model": "claude-sonnet-5",
            "system": [
                {"type": "text", "text": "billing"},
                {"type": "text", "text": "identity"},
                {"type": "text", "text": "global"},
                {"type": "text", "text": marker},
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "part-a\n\npart-b\n\npart-c"}
                    ],
                },
                {"role": "user", "content": "hello"},
            ],
        }
        candidate_record = normalized(egress_payload)
        ingress_record = normalized(ingress_payload)
        result = compare_official_egress_contract(
            {"records": [candidate_record]},
            {"records": [deepcopy(candidate_record)]},
            {"records": [ingress_record]},
            "oauth-claude-http",
        )
        self.assertTrue(result["candidate_semantic_preserved"])

    def test_oauth_claude_contract_requires_dynamic_tool_search_beta(self) -> None:
        shape = {
            "model": "claude-sonnet-5",
            "system": [],
            "messages": [],
            "tools": [{"name": "mcp__docs", "defer_loading": True}],
        }
        baseline = {
            "records": [
                _http_record(
                    "/v1/messages",
                    [["anthropic-beta", "advanced-tool-use-2025-11-20"]],
                    shape,
                    "HTTP/1.1",
                )
            ]
        }
        candidate = deepcopy(baseline)
        ingress = {
            "records": [
                _http_record("/v1/messages", [], deepcopy(shape), "HTTP/1.1")
            ]
        }
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-claude-http"
        )
        self.assertTrue(result["anthropic_dynamic_beta_exercised"])
        self.assertTrue(result["anthropic_dynamic_beta_valid"])

        candidate["records"][0]["request"]["headers"] = []
        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-claude-http"
        )
        self.assertFalse(result["contract_equal"])
        self.assertFalse(result["anthropic_dynamic_beta_valid"])

    def test_oauth_claude_dynamic_beta_is_not_a_static_profile_difference(
        self,
    ) -> None:
        shape = {
            "model": "claude-sonnet-5",
            "system": [],
            "messages": [],
            "tools": [{"name": "mcp__docs", "custom": {"defer_loading": True}}],
        }
        baseline = {
            "records": [
                _http_record(
                    "/v1/messages",
                    [["anthropic-beta", "claude-code-20250219"]],
                    {**shape, "tools": []},
                    "HTTP/1.1",
                )
            ]
        }
        candidate = {
            "records": [
                _http_record(
                    "/v1/messages",
                    [[
                        "anthropic-beta",
                        "claude-code-20250219,advanced-tool-use-2025-11-20",
                    ]],
                    shape,
                    "HTTP/1.1",
                )
            ]
        }
        ingress = {
            "records": [
                _http_record("/v1/messages", [], deepcopy(shape), "HTTP/1.1")
            ]
        }

        result = compare_official_egress_contract(
            baseline, candidate, ingress, "oauth-claude-http"
        )
        self.assertTrue(result["contract_equal"])
        self.assertTrue(result["anthropic_dynamic_beta_exercised"])
        self.assertTrue(result["anthropic_dynamic_beta_valid"])

    def test_oauth_claude_empty_tools_do_not_claim_dynamic_beta_coverage(self) -> None:
        shape = {
            "model": "claude-sonnet-5",
            "system": [],
            "messages": [],
            "tools": [],
        }
        record = _http_record("/v1/messages", [], shape, "HTTP/1.1")
        result = compare_official_egress_contract(
            {"records": [record]},
            {"records": [deepcopy(record)]},
            {"records": [deepcopy(record)]},
            "oauth-claude-http",
        )
        self.assertFalse(result["anthropic_dynamic_beta_exercised"])
        self.assertIsNone(result["anthropic_dynamic_beta_valid"])

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

    def test_anthropic_non_thinking_sampling_deletion_is_not_semantic(self) -> None:
        ingress_shape = {
            "model": "claude-sonnet-4-6",
            "messages": [],
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
        }
        egress_shape = deepcopy(ingress_shape)
        del egress_shape["top_p"]
        result = compare_official_egress_contract(
            {"records": [_http_record("/v1/messages", [], egress_shape, "HTTP/1.1")]},
            {"records": [_http_record("/v1/messages", [], egress_shape, "HTTP/1.1")]},
            {"records": [_http_record("/v1/messages", [], ingress_shape, "HTTP/1.1")]},
            "oauth-claude-http",
        )
        self.assertFalse(result["candidate_semantic_preserved"])

    def test_anthropic_thinking_sampling_deletion_is_allowed(self) -> None:
        ingress_shape = {
            "model": "claude-sonnet-4-6",
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "temperature": 1,
            "top_p": 0.9,
            "top_k": 40,
        }
        egress_shape = deepcopy(ingress_shape)
        for field in ("temperature", "top_p", "top_k"):
            del egress_shape[field]
        result = compare_official_egress_contract(
            {"records": [_http_record("/v1/messages", [], egress_shape, "HTTP/1.1")]},
            {"records": [_http_record("/v1/messages", [], egress_shape, "HTTP/1.1")]},
            {"records": [_http_record("/v1/messages", [], ingress_shape, "HTTP/1.1")]},
            "oauth-claude-http",
        )
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


def _tls_profile(
    cipher_count: int, extension_types: list[str], alpn: list[str]
) -> dict[str, object]:
    return {
        "cipher_suites": [f"0x{index:04x}" for index in range(cipher_count)],
        "extension_types": extension_types,
        "alpn": alpn,
        "curves": ["0x001d"],
        "point_formats": ["0x00"],
        "signature_algorithms": ["0x0804"],
        "supported_versions": ["0x0304"],
        "key_share_groups": ["0x001d"],
        "psk_modes": ["0x01"],
    }


if __name__ == "__main__":
    unittest.main()
