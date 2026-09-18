"""候选核心抓包 wrapper 的离线安全约束测试。"""

from __future__ import annotations

import json
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

    def test_a15_restarts_service_to_clear_models_manifest_cache(self) -> None:
        """A15 前必须重启候选服务：候选网关按账号 + 出站身份缓存 models 清单，
        A03～A08 的 responses 已填满缓存，不重启则两次启动 models 都会命中。"""
        a10_stop = self.source.index("stop_capture", self.source.index("wait_action A10 responses_http_success 4"))
        a15_start = self.source.index("start_capture A15")
        restart = self.source.index("restart_service", a10_stop)
        self.assertLess(a10_stop, restart)
        self.assertLess(restart, a15_start)
        self.assertIn("同一网关缓存", self.source[a10_stop:a15_start])

    def test_a15_uses_real_exec_and_tui_processes_and_no_curl(self) -> None:
        """A15 入口证据必须来自真实 Codex 子进程，不能手写身份头。"""

        start = self.source.index("# A15 要证明的是 exec 与 PTY TUI")
        end = self.source.index("# 冻结动作和无生产转发门禁", start)
        a15 = self.source[start:end]
        self.assertNotIn("curl", a15.lower())
        self.assertIn('"exec",\n                *overrides,', a15)
        self.assertIn('if variant == "tui":', a15)
        self.assertIn("pty.openpty()", a15)
        self.assertIn("termios.TIOCSWINSZ", a15)
        self.assertIn('"codex_exec"', a15)
        self.assertIn('"codex-tui"', a15)
        self.assertIn('"codex_cli_rs"', a15)
        self.assertIn('expected_suffixes=("", f"(codex_exec; {codex_version})")', a15)
        self.assertIn('expected_suffixes=("", f"(codex-tui; {codex_version})")', a15)
        self.assertNotIn('variant == "app-server"', a15)
        self.assertIn('"auth_mode": "chatgpt"', a15)
        self.assertNotIn('"auth_mode": "chatgptAuthTokens"', a15)
        self.assertIn("stdin=subprocess.DEVNULL", a15)
        self.assertNotIn("stdin=subprocess.PIPE", a15)

    def test_a15_witness_selects_contract_entry_by_originator(self) -> None:
        """A15 合同入口按登记的 originator 选样本，TUI 的 codex-tui 并发预取不再抢占首个样本。

        Codex 0.154 的 PTY TUI 会在 core（codex_cli_rs）之前以 originator=codex-tui
        预取同一 models 清单；按"首个样本"判定曾在两次 Campaign 首跑失败、重试通过
        （根因 rc1-f71cc39d58ddb64a5a03）。全部样本必须原样落盘为证据，不得丢弃。
        """

        start = self.source.index("# A15 要证明的是 exec 与 PTY TUI")
        end = self.source.index("# 冻结动作和无生产转发门禁", start)
        a15 = self.source[start:end]
        self.assertIn('"codex_exec" if variant == "exec" else "codex_cli_rs"', a15)
        self.assertIn('expected_originator = str(known_entry.get("expected_originator", ""))', a15)
        self.assertIn(
            'if not observations and observation["originator"] == expected_originator:',
            a15,
        )
        self.assertNotIn("if not observations:\n                observations.append(observation)", a15)
        self.assertIn("observed_models_all.setdefault(nonce, []).append(observation)", a15)
        self.assertIn('witness_path = trace_path.with_name("witness-observations.jsonl")', a15)
        self.assertIn('"contract_entry": sample in models_requests', a15)
        # 合同入口的 originator 断言与"恰好一个入口样本"判定保持不变。
        self.assertIn('expected_originator="codex_cli_rs"', a15)
        self.assertIn("if len(models_requests) != 1:", a15)

    def test_a15_binds_nonce_digests_and_server_cache_counts(self) -> None:
        start = self.source.index("# A15 要证明的是 exec 与 PTY TUI")
        end = self.source.index("# 冻结动作和无生产转发门禁", start)
        a15 = self.source[start:end]
        for expected in (
            "openai_base_url=",
            "chatgpt_base_url=",
            "secrets.token_hex(16)",
            '"argv_sha256"',
            '"launch_sha256"',
            '"request_sha256"',
            '"correlation_sha256"',
            '"upstream_calls_before"',
            '"upstream_calls_after"',
            "wait_stable_models_count",
            'records.extend(launch_one("exec", witness_port, 0, 1))',
            'records.extend(launch_one("tui", witness_port, 1, 1))',
            '"tui-post-initialize-identity"',
            'record_cache_result="not_applicable"',
            '"models_event": models_event',
            '"identity_event": identity_event',
            "observations = observed_models.setdefault(nonce, [])",
        ):
            self.assertIn(expected, a15)
        self.assertIn('"A15": {"models_manifest": 1}', self.source)
        self.assertIn(
            'if scenario in {"A03", "A06", "A07", "A15"} and actual != minimum:',
            self.source,
        )

    def test_a15_evidence_catalog_is_closed_over_exact_originals(self) -> None:
        declaration_path = (
            Path(__file__).parents[1]
            / "codex_upgrade_evidence_labels_0_154_0.json"
        )
        declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
        candidate_core = next(
            entry
            for entry in declaration["entries"]
            if entry["job_id"] == "candidate-frozen-core"
        )
        rules = {rule["glob"]: rule for rule in candidate_core["rules"]}
        self.assertNotIn(
            "scenarios/A15/relay/conn*.client_to_upstream.bin",
            rules,
        )
        self.assertEqual(
            rules["scenarios/A15/relay/conn001.client_to_upstream.bin"]["parser"],
            "opaque_bound_source",
        )
        self.assertEqual(
            rules["scenarios/A15/process-trace.jsonl"]["labels"]["a15_contract"],
            "real-entry-cache-v2",
        )
        self.assertEqual(
            rules["scenarios/A15/relay/conn001.upstream_to_client.bin"]["kind"],
            "wire_dump",
        )
        self.assertEqual(
            rules["scenarios/A15/relay/intervention.jsonl"]["parser"],
            "opaque_bound_source",
        )
        # 2026-09-18：go test 日志改由 candidate-trace-test 独立 Job 产出，
        # candidate-frozen-core 名下不再登记该规则；A15 仍不接受 Go 静态事实。
        self.assertNotIn("candidate-go-test.jsonl", rules)
        trace_entry = next(
            entry
            for entry in declaration["entries"]
            if entry["job_id"] == "candidate-trace-test"
        )
        trace_rules = {rule["glob"]: rule for rule in trace_entry["rules"]}
        self.assertEqual(list(trace_rules), ["candidate-go-test.jsonl"])
        self.assertNotIn("A15", trace_rules["candidate-go-test.jsonl"]["scenario_ids"])
        self.assertEqual(trace_rules["candidate-go-test.jsonl"]["parser"], "opaque_bound_source")

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
        self.assertIn('scenario in {"A03", "A06", "A07", "A15"}', self.source)

    def test_a03_uses_cold_lite_prime_before_cookie_replay(self) -> None:
        prime = self.source.index(
            'write_request_body "$trigger_root/prime.json" "$lite_model" lite'
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
        self.assertIn('scenario in {"A03", "A06", "A07", "A15"}', self.source)
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


class MitmMatrixCodexIsolationTest(unittest.TestCase):
    """MITM Job 必须隔离 0.151 的插件／MCP 后台流量并保留足够时限。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1]
            / "run_sub2api_openai_mitm_matrix.sh"
        ).read_text()

    def test_uses_isolated_codex_home_for_both_drivers(self) -> None:
        self.assertIn(
            'isolated_codex_home="$capture_root/runtime/codex-mitm-home-$window_id"',
            self.source,
        )
        self.assertGreaterEqual(
            self.source.count('-e CODEX_HOME="$container_codex_home"'),
            2,
        )
        self.assertIn("plugins = false", self.source)
        self.assertIn('rm -rf -- "$isolated_codex_home"', self.source)

    def test_defines_capture_mount_before_container_home(self) -> None:
        assignment = "capture_mount=${CAPTURE_MOUNT:-/capture}"
        use = 'container_codex_home="$capture_mount/runtime/codex-mitm-home-$window_id"'
        self.assertIn(assignment, self.source)
        self.assertIn(use, self.source)
        self.assertLess(self.source.index(assignment), self.source.index(use))

    def test_mitm_scenario_timeout_has_explicit_floor(self) -> None:
        self.assertIn(
            "scenario_timeout_seconds=${SCENARIO_TIMEOUT_SECONDS:-120}",
            self.source,
        )
        self.assertIn("scenario_timeout_seconds < 120", self.source)
        self.assertNotIn("--timeout 70", self.source)

    def test_each_subject_scenario_has_an_independent_checkpoint(self) -> None:
        self.assertIn("mitm_scenario_checkpoint.py", self.source)
        self.assertIn(
            'run_id="$run_id_prefix-$subject-$scenario-a$attempt_index-$window_id"',
            self.source,
        )
        self.assertIn('-e CAPTURE_SCENARIO="$scenario"', self.source)
        self.assertIn("incremental_noop=true", self.source)
        self.assertIn("pcap_scanned_bytes=0", self.source)
        self.assertIn("preserve_active_failure()", self.source)
        self.assertNotIn("WS pcap", self.source)
