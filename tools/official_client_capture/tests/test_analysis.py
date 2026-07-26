"""direct 证据提取与结构比较测试。"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.official_client_capture.capturelib.analysis import (
    TSHARK_CLIENT_HELLO_FIELDS,
    compare_normalized,
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

    def test_compare_rejects_mixed_evidence_kinds(self) -> None:
        result = compare_normalized({"records": []}, {"client_hellos": []})
        self.assertFalse(result["equal"])

    def test_compare_rejects_unknown_documents(self) -> None:
        self.assertFalse(compare_normalized({}, {})["equal"])


if __name__ == "__main__":
    unittest.main()
