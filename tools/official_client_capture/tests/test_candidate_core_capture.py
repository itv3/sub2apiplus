"""候选核心抓包 wrapper 的离线安全约束测试。"""

from __future__ import annotations

import json
import subprocess
import tempfile
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
        self.assertIn('f"codex_exec/{codex_version}": (\n                            "",\n'
                      '                            f"(codex_exec; {codex_version})",', a15)
        # TUI 启动 models 的 UA 形态按目标版本精确区分，与 candidate_rule_assertion 同口径。
        self.assertIn("ua_forms=tui_startup_ua_forms(),", a15)
        self.assertIn("A15_CORE_STARTUP_RACES_INITIALIZE_MIN_VERSION = (0, 157, 0)", a15)
        self.assertIn('forms = {f"codex-tui/{codex_version}": ("", f"(codex-tui; {codex_version})")}', a15)
        self.assertIn('forms[f"codex_cli_rs/{codex_version}"] = ("",)', a15)
        self.assertNotIn("expected_prefix", a15)
        self.assertNotIn('variant == "app-server"', a15)
        self.assertIn('"auth_mode": "chatgpt"', a15)
        self.assertNotIn('"auth_mode": "chatgptAuthTokens"', a15)
        self.assertIn("stdin=subprocess.DEVNULL", a15)
        self.assertNotIn("stdin=subprocess.PIPE", a15)

    def test_a15_witness_is_https_and_answers_workspace_routing_locally(self) -> None:
        """Codex 0.157 起 TUI 启动先做 workspace 路由发现：accounts/check 须含当前账号条目，
        backend origin 须为 HTTPS，否则以 account/read 失败退出（194249z frozen-core 三连败）。

        witness 改为临时 CA 签发的 HTTPS；accounts/check 在 localhost 受控应答 NO_CONSTRAINT，
        不转发 Candidate；临时 CA 只经环境变量交给 codex 子进程，TLS 目录在任何退出路径删除。
        """

        start = self.source.index("# A15 要证明的是 exec 与 PTY TUI")
        end = self.source.index("# 冻结动作和无生产转发门禁", start)
        a15 = self.source[start:end]
        self.assertIn("f'openai_base_url=\"https://127.0.0.1:{witness_port}/a15/{nonce}'", a15)
        self.assertIn("f'chatgpt_base_url=\"https://127.0.0.1:{witness_port}/a15/{nonce}'", a15)
        self.assertNotIn("http://127.0.0.1:{witness_port}", a15)
        self.assertIn("server.socket = witness_context.wrap_socket(server.socket, server_side=True)", a15)
        self.assertIn('environment["SSL_CERT_FILE"] = str(witness_ca_bundle)', a15)
        self.assertIn('environment["CODEX_CA_CERTIFICATE"] = str(witness_ca_bundle)', a15)
        self.assertNotIn("update-ca-certificates", a15[a15.index("def prepare_witness_tls"):])
        branch = a15[
            a15.index("if public_target == A15_ACCOUNTS_CHECK_TARGET:"):
            a15.index("if public_target in TUI_PLUGIN_TARGETS | TUI_AUXILIARY_PLUGIN_TARGETS:")
        ]
        self.assertIn("return", branch)
        self.assertNotIn("connection_type", branch)
        self.assertNotIn("service_prefix", branch)
        self.assertIn('A15_ACCOUNTS_CHECK_TARGET = "/backend-api/wham/accounts/check"', a15)
        self.assertIn('"workspace_backend_origin": "NO_CONSTRAINT"', a15)
        self.assertIn('"account_routing_override": "NO_CONSTRAINT"', a15)
        self.assertIn('"default_account_id": A15_ACCOUNT_ID', a15)
        self.assertIn('"account_id": A15_ACCOUNT_ID', a15)
        self.assertIn("observed_account_checks.pop(nonce, None)", a15)
        self.assertIn("for sample in all_samples + account_samples:", a15)
        self.assertEqual(a15.count("shutil.rmtree(witness_tls_dir, ignore_errors=True)"), 3)

    def test_a15_witness_selects_contract_entry_by_originator(self) -> None:
        """A15 合同入口按登记的 originator 选样本，TUI 的 codex-tui 并发预取不再抢占首个样本。

        Codex 0.154 的 PTY TUI 会在 core（codex_cli_rs）之前以 originator=codex-tui
        预取同一 models 清单；按"首个样本"判定曾在两次 Campaign 首跑失败、重试通过
        （根因 rc1-f71cc39d58ddb64a5a03）。全部样本必须原样落盘为证据，不得丢弃。
        """

        start = self.source.index("# A15 要证明的是 exec 与 PTY TUI")
        end = self.source.index("# 冻结动作和无生产转发门禁", start)
        a15 = self.source[start:end]
        self.assertIn('("codex_exec",) if variant == "exec" else tui_startup_originators()', a15)
        self.assertIn('expected_originators = tuple(known_entry.get("expected_originators", ()))', a15)
        self.assertIn(
            'if not observations and observation["originator"] in expected_originators:',
            a15,
        )
        # 0.157 起启动 models 与 TUI initialize 并发，入口 originator 可能已是 codex-tui；早期版本仍只认 core。
        self.assertIn(
            'return ("codex_cli_rs", "codex-tui") if core_startup_races_initialize() else ("codex_cli_rs",)',
            a15,
        )
        self.assertNotIn("if not observations:\n                observations.append(observation)", a15)
        self.assertIn("observed_models_all.setdefault(nonce, []).append(observation)", a15)
        self.assertIn('witness_path = trace_path.with_name("witness-observations.jsonl")', a15)
        self.assertIn('"contract_entry": sample in entry_samples', a15)
        # 合同入口的 originator 断言与"恰好一个入口样本"判定保持不变。
        self.assertIn("expected_originators=tui_startup_originators(),", a15)
        self.assertIn("if originator not in expected_originators:", a15)
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
            'if scenario in {"A03", "A06", "A07", "A15", "A20", "A21", "A22"} and actual != minimum:',
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
        self.assertIn('scenario in {"A03", "A06", "A07", "A15", "A20", "A21", "A22"}', self.source)

    def test_ws_replay_scenarios_use_the_plan_driver_on_one_connection(self) -> None:
        """A20～A22（0.165.0 起）：每个场景一条入站 WS 连接、网关预热加驱动帧数的 response.create、恰一次握手。"""

        self.assertIn("run_response_ws_plan()", self.source)
        self.assertIn("target_ws_replay=$(python3", self.source)
        self.assertIn("if (( target_ws_replay == 1 )); then", self.source)
        for scenario, plan, creates in (
            ("A20", "model-switch", 4), ("A21", "compact-continue", 6), ("A22", "effort-change", 3),
        ):
            with self.subTest(scenario=scenario):
                self.assertIn(f"start_capture {scenario}", self.source)
                self.assertIn(f"run_response_ws_plan {scenario} {plan}", self.source)
                self.assertIn(f"wait_action {scenario} responses_ws_handshake_success", self.source)
                self.assertIn(f"wait_action {scenario} responses_ws_response_create {creates}", self.source)
                self.assertIn(
                    f'minimums["{scenario}"] = {{"responses_ws_handshake_success": 1, "responses_ws_response_create": {creates}}}',
                    self.source,
                )
        # 计划驱动的受控响应 id 前缀由场景号推出，序号从 0002 起（0001 是网关预热）；驱动自己逐帧核对。
        self.assertIn("--expected-id-prefix", self.source)
        self.assertIn("--second-model \"$lite_model\"", self.source)
        self.assertIn("--no-update-model \"$no_update_model\"", self.source)
        # 场景只在 0.165.0 起运行，更早的版本场景集合与改造前相同。
        replay_block = self.source[self.source.index("if (( target_ws_replay == 1 )); then"):self.source.index("# A07：单次生产入口调用")]
        self.assertIn("start_capture A20", replay_block)
        self.assertNotIn("start_capture A07", replay_block)
        # A20～A22 排在 A06 之后、A07 之前，中间不重启网关：A05 建立的 Cookie jar 继续有效。
        self.assertLess(self.source.index("start_capture A06"), self.source.index("start_capture A20"))
        self.assertLess(self.source.index("start_capture A22"), self.source.index("start_capture A07"))
        self.assertNotIn("restart_service", replay_block)

    def test_ws_replay_models_are_validated_before_any_docker_call(self) -> None:
        validation = self.source.index("no_update_model=${NO_UPDATE_MODEL:-gpt-5.6-sol}")
        self.assertLess(validation, self.source.index("work_dir=\"$capture_root/runs/$run_id\""))
        self.assertIn("A20 换模型需要两个不同的模型", self.source)
        self.assertIn("候选网关 WebSocket 驱动器没有计划模式", self.source)

    def test_summary_and_exact_counts_cover_replay_scenarios_only_from_0165(self) -> None:
        self.assertIn('scenario_ids += ["A20", "A21", "A22"]', self.source)
        self.assertIn("(0, 165, 0)", self.source)

    def _run_replay_block(self, version: str) -> list[list[str]]:
        """桩环境真实执行 A20～A22 段（含版本开关行），返回按序记录的调用。"""

        gate = next(line for line in self.source.splitlines() if line.startswith("target_ws_replay=$("))
        start = self.source.index("if (( target_ws_replay == 1 )); then\n  # A20")
        end = self.source.index("# A07：单次生产入口调用")
        harness = r"""
set -e -o pipefail
codex_version=$1
LOG=$2
main_model=main-model
lite_model=lite-model
no_update_model=no-update-model
record() { local IFS=$'\t'; printf '%s\n' "$*" >>"$LOG"; }
start_capture() { record start_capture "$1"; }
stop_capture() { record stop_capture; }
wait_action() { record wait_action "$@"; }
run_response_ws_plan() { record run_response_ws_plan "$@"; }
"""
        with tempfile.TemporaryDirectory(prefix="replay-harness-") as tmp:
            log = Path(tmp) / "calls.tsv"
            log.touch()
            result = subprocess.run(
                ["bash", "-c", harness + gate + "\n" + self.source[start:end], "replay-harness", version, str(log)],
                capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            return [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]

    def test_replay_block_runs_only_from_0165_and_in_order(self) -> None:
        for version in ("0.157.0", "0.163.0", "0.164.0"):
            with self.subTest(version=version):
                self.assertEqual(self._run_replay_block(version), [])
        for version in ("0.165.0", "0.165.1", "0.166.0"):
            with self.subTest(version=version):
                self.assertEqual(self._run_replay_block(version), [
                    ["start_capture", "A20"], ["run_response_ws_plan", "A20", "model-switch", "--second-model", "lite-model"],
                    ["wait_action", "A20", "responses_ws_handshake_success"],
                    ["wait_action", "A20", "responses_ws_response_create", "4"], ["stop_capture"],
                    ["start_capture", "A21"], ["run_response_ws_plan", "A21", "compact-continue"],
                    ["wait_action", "A21", "responses_ws_handshake_success"],
                    ["wait_action", "A21", "responses_ws_response_create", "6"], ["stop_capture"],
                    ["start_capture", "A22"], ["run_response_ws_plan", "A22", "effort-change", "--no-update-model", "no-update-model"],
                    ["wait_action", "A22", "responses_ws_handshake_success"],
                    ["wait_action", "A22", "responses_ws_response_create", "3"], ["stop_capture"],
                ])

    def test_model_validation_runs_before_work_dir_with_clear_errors(self) -> None:
        """模型名与 A20 的两个模型必须不同，在任何 docker 调用之前拒绝（真实执行校验段）。"""

        start = self.source.index("no_update_model=${NO_UPDATE_MODEL:-gpt-5.6-sol}")
        end = self.source.index('work_dir="$capture_root/runs/$run_id"')
        for version, main, lite, extra, message in (
            ("0.165.0", "gpt-6.1-sol", "gpt-6.1-sol", "", "LITE_MODEL 不能等于 MAIN_MODEL"),
            ("0.165.0", "gpt-6.1-sol", "bad model", "", "只能包含字母、数字"),
            ("0.165.0", "gpt-6.1-sol", "gpt-6-astra", "-bad", "只能包含字母、数字"),
        ):
            with self.subTest(main=main, lite=lite, extra=extra):
                script = "\n".join([
                    "set -e", f"codex_version={version}", f"main_model='{main}'", f"lite_model='{lite}'",
                    f"export NO_UPDATE_MODEL='{extra}'" if extra else ":",
                    self.source[start:end],
                ])
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
        for version, lite in (("0.165.0", "gpt-6-astra"), ("0.164.0", "gpt-6.1-sol")):
            with self.subTest(version=version):
                script = "\n".join(["set -e", f"codex_version={version}", "main_model=gpt-6.1-sol", f"lite_model={lite}", self.source[start:end]])
                self.assertEqual(subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False).returncode, 0)

    def test_0165_replay_scenarios_are_declared_and_labeled_for_the_candidate(self) -> None:
        """A20～A22 在 candidate-frozen-core 名下有证据条目：scenario_ids 追加、每个场景一条 WS 连接加 pcap，
        标签与帧标签与 A06 同形；期望清单里选 A20～A22 的候选侧判据因此有证据来源。"""

        root = Path(__file__).parents[1]
        manifest = json.loads((root / "codex_upgrade_scenarios_0_165_0.json").read_text(encoding="utf-8"))
        job = next(item for item in manifest["capture_jobs"] if item["id"] == "candidate-frozen-core")
        self.assertEqual(job["scenario_ids"][-3:], ["A20", "A21", "A22"])
        self.assertEqual(job["scenario_ids"][:8], ["A03", "A04", "A05", "A06", "A07", "A08", "A10", "A15"])
        environment = job["steps"][0]["environment"]
        self.assertEqual(environment["LITE_MODEL"], "{lite_model}")
        self.assertEqual(environment["MAIN_MODEL"], "{model}")
        for dependency in ("drive_candidate_gateway_ws.py", "upstream_byte_relay.py", "run_candidate_core_capture.sh"):
            self.assertIn(dependency, job["tool_dependencies"])
        labels = json.loads((root / "codex_upgrade_evidence_labels_0_165_0.json").read_text(encoding="utf-8"))
        entry = next(item for item in labels["entries"] if item["job_id"] == "candidate-frozen-core")
        rules = {rule["glob"]: rule for rule in entry["rules"]}
        a06 = rules["scenarios/A06/relay/conn001.client_to_upstream.bin"]
        for scenario in ("A20", "A21", "A22"):
            with self.subTest(scenario=scenario):
                rule = rules[f"scenarios/{scenario}/relay/conn001.client_to_upstream.bin"]
                self.assertEqual(rule["scenario_ids"], [scenario])
                for key in ("kind", "parser", "derive", "labels", "frame_labels"):
                    self.assertEqual(rule[key], a06[key], key)
                self.assertEqual(rule["labels"]["transport"], "websocket")
                pcap = rules[f"scenarios/{scenario}/egress.pcap"]
                self.assertEqual((pcap["kind"], pcap["parser"]), ("pcap", "pcap_client_hello"))
        # 期望清单里选 A20～A22 且只在候选侧机器验的规则，都落在这三个场景的 WS 证据上（record_type 为 websocket_frame／http_request）。
        expectations = json.loads((root / "candidate_rule_expectations_0_165_0.json").read_text(encoding="utf-8"))
        selected = {
            scenario
            for rule in expectations["rules"]
            for check in rule["checks"]
            for scenario in check["select"].get("scenario_ids", [])
            if scenario in {"A20", "A21", "A22"}
        }
        self.assertEqual(selected, {"A20", "A21", "A22"})

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
        self.assertIn('scenario in {"A03", "A06", "A07", "A15", "A20", "A21", "A22"}', self.source)
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
            self.source.index('if mode in {"lite", "lite_manifest_default"}:') :
            self.source.index('elif mode == "non_lite":')
        ]
        self.assertIn('payload.pop("instructions")', lite_branch)
        self.assertIn('payload.pop("tools")', lite_branch)
        self.assertIn('"type": "additional_tools"', lite_branch)
        self.assertIn('"role": "developer"', lite_branch)
        self.assertIn('"type": "input_text"', lite_branch)
        self.assertIn('payload["parallel_tool_calls"] = False', lite_branch)
        self.assertIn('payload["reasoning"]["context"] = "all_turns"', lite_branch)

    def test_a03_lite_requests_leave_reasoning_defaults_to_manifest(self) -> None:
        """A03 的 Lite 请求必须与官方客户端同条件：不显式发 effort／summary／text。

        官方对 Lite 模型按清单默认定型（effort=medium、summary 缺席、text.verbosity=low
        由 Lite 画像派生）；候选若写死 high／auto，网关按"显式优先"保留，出站永远与官方
        不等价。该模式只用于 A03 的 Astra Lite 请求，其他场景保持原夹具。
        """
        lite_branch = self.source[
            self.source.index('if mode in {"lite", "lite_manifest_default"}:') :
            self.source.index('elif mode == "non_lite":')
        ]
        self.assertIn('if mode == "lite_manifest_default":', lite_branch)
        self.assertIn('payload["reasoning"] = {"context": "all_turns"}', lite_branch)
        a03 = self.source[self.source.index("start_capture A03") : self.source.index("start_capture A04")]
        self.assertIn('"$lite_model" lite_manifest_default a03-cookie-prime', a03)
        self.assertIn('"$lite_model" lite_manifest_default a03-turn', a03)
        self.assertIn('"$main_model" non_lite a03-default', a03)
        rest = self.source[self.source.index("start_capture A04") :]
        self.assertNotIn("lite_manifest_default", rest, "专用模式不得扩散到 A03 之外")

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
    监督器按完整容器 ID 钉住 CAPTURE_CONTAINER 之后（C-04），这个变量改为 ``capture_host``
    （该容器实例的真实名称），ID 不能当主机名用。
    """

    def test_proxy_row_uses_the_reachability_check_host_variable(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "run_sub2api_openai_mitm_matrix.sh"
        ).read_text()
        insert = next(
            line for line in source.splitlines() if "insert into proxies" in line
        )
        self.assertIn("'$capture_host'", insert)
        self.assertNotIn("'capture-cli'", insert)
        self.assertIn('getent hosts "$capture_host"', source)


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


def _zstd_compress(data: bytes) -> bytes:
    try:
        import zstandard

        return zstandard.ZstdCompressor().compress(data)
    except ImportError:
        result = subprocess.run(["zstd", "-c"], input=data, capture_output=True, check=False)
        if result.returncode != 0:
            raise unittest.SkipTest("没有 zstd 压缩能力")
        return result.stdout


def _request(path: str, headers: list[str], body: bytes = b"") -> bytes:
    head = f"POST {path} HTTP/1.1\r\n" + "".join(f"{line}\r\n" for line in headers)
    return head.encode() + b"\r\n" + body


LITE_HEADERS = [
    "version: 0.163.0",
    "content-encoding: zstd",
    "x-openai-internal-codex-responses-lite: true",
]


class CoreCaptureLiteMainTrackChecksTest(unittest.TestCase):
    """0.163.0 起主线模型是 Lite：A04 的 Responses 请求全是 zstd 压缩的 Lite 请求，A10 的 trigger 要解压后再数。

    候选核心采集脚本收尾处原先把 A04 当“非 Lite 明文”（出现 zstd 就失败），并在压缩后的原始字节里数 A10 的
    compaction_trigger；主线改 Lite 时漏改了这个脚本，VC-5 批次 29 的 candidate-frozen-core 因此失败。
    """

    @classmethod
    def setUpClass(cls) -> None:
        source = (Path(__file__).parents[1] / "run_candidate_core_capture.sh").read_text(encoding="utf-8")
        start = source.index("def _decoded_request_body(data):")
        end = source.index("\nPY\n\ncapture_status=complete", start)
        cls.block = source[start:end]
        cls.source = source

    def test_old_non_lite_plaintext_assertion_is_gone(self) -> None:
        self.assertNotIn("A04 非 Lite 明文场景意外出现 zstd", self.source)
        self.assertIn("A04 主线 Lite 样本应为 zstd 压缩的 Lite 请求", self.source)

    def _run(self, a04: list[bytes], a10: list[bytes]) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for scenario, requests in (("A04", a04), ("A10", a10)):
                relay = root / "scenarios" / scenario / "relay"
                relay.mkdir(parents=True)
                for index, data in enumerate(requests, 1):
                    (relay / f"conn{index:03d}.client_to_upstream.bin").write_bytes(data)
            exec(compile(self.block, "core-capture-tail", "exec"), {"root": root, "Path": Path})

    def _lite_post(self, text: bytes) -> bytes:
        body = _zstd_compress(text)
        return _request("/backend-api/codex/responses", LITE_HEADERS + [f"content-length: {len(body)}"], body)

    def test_lite_zstd_a04_and_decompressed_a10_triggers_pass(self) -> None:
        # 可压缩的大正文：压缩后原始字节里只留一份字面量，三个请求合计看不到四个；解压后才有 120 个。
        needle = b'"type":"compaction_trigger"'
        a10 = [self._lite_post(b"{" + (needle + b",") * 40 + b"}") for _ in range(3)]
        self.assertLess(sum(raw.count(needle) for raw in a10), 4)
        self._run(
            a04=[
                _request("/backend-api/codex/models?client_version=0.163.0", ["version: 0.163.0"]).replace(
                    b"POST", b"GET", 1
                ),
                self._lite_post(b'{"model":"gpt-6.1-sol"}'),
            ],
            a10=a10,
        )

    def test_chunked_zstd_bodies_are_decoded_before_counting(self) -> None:
        trigger = b'{"input":[{"type":"compaction_trigger"}]}'
        compressed = _zstd_compress(trigger)
        chunked = (
            f"{len(compressed):x}\r\n".encode() + compressed + b"\r\n0\r\n\r\n"
        )
        data = _request(
            "/backend-api/codex/responses",
            LITE_HEADERS + ["transfer-encoding: chunked"],
            chunked,
        )
        self._run(a04=[self._lite_post(b"{}")], a10=[data] * 4)

    def test_plaintext_a04_post_fails_closed(self) -> None:
        plain = _request("/backend-api/codex/responses", ["version: 0.163.0"], b"{}")
        with self.assertRaises(SystemExit) as caught:
            self._run(a04=[plain], a10=[self._lite_post(b'"type":"compaction_trigger"')] * 4)
        self.assertIn("A04 主线 Lite 样本应为 zstd 压缩的 Lite 请求", str(caught.exception))

    def test_a04_without_lite_header_fails_closed(self) -> None:
        body = _zstd_compress(b"{}")
        no_lite = _request(
            "/backend-api/codex/responses",
            ["version: 0.163.0", "content-encoding: zstd"],
            body,
        )
        with self.assertRaises(SystemExit):
            self._run(a04=[no_lite], a10=[self._lite_post(b'"type":"compaction_trigger"')] * 4)

    def test_a04_without_responses_request_fails_closed(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self._run(a04=[], a10=[self._lite_post(b'"type":"compaction_trigger"')] * 4)
        self.assertIn("A04 没有抓到 Responses 请求", str(caught.exception))

    def test_too_few_a10_triggers_fail_closed(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self._run(
                a04=[self._lite_post(b"{}")],
                a10=[self._lite_post(b'"type":"compaction_trigger"')] * 3,
            )
        self.assertIn("A10 未抓到四个真实 compaction_trigger", str(caught.exception))
