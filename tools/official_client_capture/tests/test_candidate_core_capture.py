"""候选核心抓包 wrapper 的离线安全约束测试。"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class CandidateCoreCaptureScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = Path(__file__).parents[1] / "run_candidate_core_capture.sh"
        cls.source = cls.script.read_text(encoding="utf-8")

    def test_shell_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_zstd_generator_forwards_stdin_into_capture_container(self) -> None:
        self.assertIn(
            'docker exec -i "$capture_container" python3 -',
            self.source,
        )

    def test_request_identity_header_is_bound_to_body_metadata(self) -> None:
        self.assertIn('metadata_path.write_text(turn_metadata + "\\n"', self.source)
        self.assertIn('turn_metadata=$(<"$body.turn-metadata")', self.source)
        self.assertIn('headers+=(-H "X-Codex-Turn-Metadata: $turn_metadata")', self.source)
        self.assertIn('payload["client_metadata"]["x-openai-subagent"]', self.source)
        self.assertIn('payload["client_metadata"]["x-codex-parent-thread-id"]', self.source)
        self.assertNotIn("-H 'X-Codex-Turn-Metadata:", self.source)
        self.assertNotIn("candidate-core-driver/1.0", self.source)
        self.assertIn("gateway_driver_ua='sub2apiplus-candidate-capture/1.0'", self.source)
        self.assertIn("gateway_driver_originator='sub2apiplus_candidate_capture'", self.source)
        self.assertIn(
            '"$gateway_driver_ua" "$gateway_driver_originator"',
            self.source,
        )
        self.assertIn('"tool_choice": "auto"', self.source)
        self.assertNotIn('"tool_choice": "required"', self.source)

    def test_child_identity_uses_distinct_thread_and_matching_headers(self) -> None:
        self.assertIn(
            'if thread_source in {"memory_consolidation", "subagent"}',
            self.source,
        )
        self.assertIn('thread_path.write_text(thread_id + "\\n"', self.source)
        self.assertIn('window_path.write_text(metadata["window_id"] + "\\n"', self.source)
        self.assertIn('thread_id=$(<"$body.thread-id")', self.source)
        self.assertIn('window_id=$(<"$body.window-id")', self.source)
        self.assertIn(
            'official_headers "$ua" "$originator" "$thread_id" "$window_id"',
            self.source,
        )
        self.assertIn('"X-Client-Request-Id: $thread_id"', self.source)
        self.assertNotIn('"X-Client-Request-Id: $session_id"', self.source)

    def test_requires_two_independent_synthetic_switches(self) -> None:
        self.assertIn("ENABLE_CANDIDATE_CORE_SYNTHETIC", self.source)
        self.assertIn("required_gate=YES_I_ACCEPT_SYNTHETIC_ONLY", self.source)
        self.assertIn("--synthetic-profile candidate-core-v1", self.source)
        self.assertIn("--allow-synthetic-responses", self.source)

    def test_has_closed_scenario_set_and_no_remote_operation(self) -> None:
        for scenario in ("A03", "A04", "A05", "A06", "A07", "A08", "A10", "A15"):
            self.assertIn(f"start_capture {scenario}", self.source)
        self.assertNotIn("ssh ", self.source)
        self.assertNotIn("--upstream-ip", self.source)
        self.assertNotIn("--upstream-map", self.source)

    def test_restoration_is_fail_closed(self) -> None:
        for expected in (
            "original_proxy_state",
            "original_extra_hex",
            "original_hosts_hash",
            "original_ca_hash",
            "keeper_was_running",
            "exit 97",
        ):
            self.assertIn(expected, self.source)
        self.assertIn("trap restore_environment EXIT ERR INT TERM", self.source)
        self.assertIn('"account_proxy_equal": sys.argv[6] == "true"', self.source)
        self.assertIn('"account_extra_equal": sys.argv[7] == "true"', self.source)
        self.assertNotIn('"account_extra_equal": True', self.source)

    def test_pcap_is_drained_before_tcpdump_stops(self) -> None:
        relay_stop = self.source.index("relay_started=0", self.source.index("stop_capture()"))
        drain = self.source.index("sleep 1", relay_stop)
        pcap_stop = self.source.index("if [[ $pcap_started == 1 ]]", drain + 1)
        self.assertLess(relay_stop, drain)
        self.assertLess(drain, pcap_stop)

    def test_pcap_must_contain_a_packet_and_is_private(self) -> None:
        self.assertIn("<= 24", self.source)
        self.assertIn('tcpdump -nn -r \\', self.source)
        self.assertIn('"$container_scenario_root/egress.pcap" -c 1', self.source)
        self.assertIn('chmod 0600 "$path"', self.source)
        self.assertGreaterEqual(self.source.count("umask 077"), 3)

    def test_background_process_shutdown_is_fail_closed(self) -> None:
        self.assertIn("stop_container_process()", self.source)
        self.assertIn('kill -KILL "$pid"', self.source)
        self.assertIn("环境恢复不能视为成功", self.source)
        self.assertIn("restore_failed=1", self.source)

    def test_ca_cleanup_is_armed_before_bundle_update(self) -> None:
        copy = self.source.index('if ! docker cp "$ca_cert"')
        armed = self.source.index("\nfi\nca_installed=1\n", copy)
        update = self.source.index("update-ca-certificates", armed)
        self.assertLess(armed, update)
        self.assertIn("custom_ca_baseline_absent=1", self.source)
        self.assertIn('test ! -e "$custom_ca_path"', self.source)
        self.assertIn("service_restart_needed", self.source)

    def test_a08_and_token_budget_do_not_claim_synthetic_facts(self) -> None:
        self.assertIn("只采真实跨上层调用连接", self.source)
        self.assertIn("TokenBudget 零出站不造包", self.source)
        self.assertIn("结构化测试补证", self.source)

    def test_a06_uses_one_gateway_websocket_and_exact_three_frames(self) -> None:
        self.assertIn('gateway_ws_driver="$script_dir/drive_candidate_gateway_ws.py"', self.source)
        self.assertIn("run_response_ws_session()", self.source)
        self.assertIn("--path /v1/responses", self.source)
        self.assertIn("--api-key-fd 3", self.source)
        self.assertIn("prepare_a06_bodies", self.source)
        self.assertIn('"type": "additional_tools"', self.source)
        self.assertIn("wait_action A06 responses_ws_response_create 3", self.source)
        self.assertIn("resp_candidate_core_a06_0002", self.source)
        self.assertIn("resp_candidate_core_a06_0003", self.source)
        self.assertIn('scenario in {"A03", "A06", "A07"}', self.source)

    def test_a03_primes_cookie_before_two_lite_turns_and_requires_four(self) -> None:
        prime = self.source.index(
            'write_request_body "$trigger_root/prime.json" "$main_model" non_lite'
        )
        default = self.source.index(
            'write_request_body "$trigger_root/default.json" "$main_model" non_lite'
        )
        lite = self.source.index(
            'write_request_body "$trigger_root/lite.json" "$lite_model" lite'
        )
        self.assertLess(prime, default)
        self.assertLess(default, lite)
        self.assertIn("run_response_request A03 prime", self.source)
        self.assertIn("run_response_request A03 default", self.source)
        self.assertIn('run_response_request A03 "lite-turn-$turn"', self.source)
        self.assertIn("wait_action A03 responses_http_success 4", self.source)
        self.assertIn('"A03": {"responses_http_success": 4}', self.source)
        self.assertIn('scenario in {"A03", "A06", "A07"}', self.source)
        self.assertIn('event.get("set_cookie_names") == ["_cfuvid"]', self.source)
        self.assertIn(
            'any(b"\\r\\ncookie: <secret>" not in request',
            self.source,
        )
        self.assertIn('b"_cfuvid" in data', self.source)
        self.assertIn('turn_state not in a03_pairs[2][1]', self.source)

    def test_lite_fixture_is_already_shaped_like_codex_client(self) -> None:
        """严格入口前的 Lite 夹具必须是官方客户端形态，不能依赖网关迁移字段。"""
        lite_branch = self.source[
            self.source.index('if mode == "lite":') :
            self.source.index('elif mode == "non_lite":')
        ]
        self.assertIn('payload.pop("instructions")', lite_branch)
        self.assertIn('payload.pop("tools")', lite_branch)
        self.assertIn('"type": "additional_tools"', lite_branch)
        self.assertIn('"role": "developer"', lite_branch)
        self.assertIn('"type": "input_text"', lite_branch)
        self.assertIn('payload["parallel_tool_calls"] = False', lite_branch)
        self.assertIn('payload["reasoning"]["context"] = "all_turns"', lite_branch)

    def test_api_key_is_not_exported_for_driver_or_secret_scan(self) -> None:
        self.assertIn("set +x", self.source)
        self.assertGreaterEqual(self.source.count("3< <(printf '%s' \"$api_key\")"), 2)
        self.assertIn('needle = os.fdopen(3, "rb").read()', self.source)
        self.assertNotIn("CANDIDATE_CORE_API_KEY", self.source)


if __name__ == "__main__":
    unittest.main()


class CandidateCoreAccountGateTest(unittest.TestCase):
    """熔断清理必须覆盖每一条触发路径，HTTP 与 WS 不能只顾一头。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1] / "run_candidate_core_capture.sh"
        ).read_text()

    def test_ws_session_clears_account_gate_before_driving(self) -> None:
        start = self.source.index("run_response_ws_session() {")
        driver = self.source.index('python3 "$gateway_ws_driver"', start)
        self.assertIn("clear_account_gate", self.source[start:driver])

    def test_http_request_helper_clears_account_gate(self) -> None:
        body = self.source[
            self.source.index("request_with_token() {") : self.source.index(
                "assert_2xx() {"
            )
        ]
        self.assertIn("clear_account_gate", body)


class MitmMatrixProxyHostTest(unittest.TestCase):
    """临时代理的 host 必须与可达性检查用的是同一个变量。

    两处不一致时 DNS 检查照样通过，真正出站却解析不到，账号被判 upstream transport
    error 而熔断，后续 job 一路 503／WS 1013——症状离根因很远（k54）。
    """

    def test_proxy_row_uses_capture_container_variable(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "run_sub2api_openai_mitm_matrix.sh"
        ).read_text()
        insert = next(
            line for line in source.splitlines() if "insert into proxies" in line
        )
        self.assertIn("'$capture_container'", insert)
        self.assertNotIn("'capture-cli'", insert)
