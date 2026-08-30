"""候选辅助抓包 wrapper 的离线安全约束测试。"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class CandidateAuxCaptureScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = Path(__file__).parents[1] / "run_candidate_aux_capture.sh"
        cls.source = cls.script.read_text(encoding="utf-8")

    def test_shell_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_synthetic_boundary_and_restoration_are_fail_closed(self) -> None:
        self.assertIn("ENABLE_CANDIDATE_AUX_SYNTHETIC", self.source)
        self.assertIn("--synthetic-profile candidate-aux-v1", self.source)
        self.assertIn("--allow-synthetic-responses", self.source)
        self.assertIn("trap restore_environment EXIT ERR INT TERM", self.source)
        self.assertIn("exit 97", self.source)
        self.assertNotIn("--upstream-ip", self.source)
        self.assertNotIn("--upstream-map", self.source)

    def test_read_only_preflight_finishes_before_restoration_trap(self) -> None:
        snapshot = self.source.index("original_ca_hash=")
        trap = self.source.index("trap restore_environment EXIT ERR INT TERM")
        first_mutation = self.source.index('docker stop "$keeper_container"', trap)
        self.assertLess(snapshot, trap)
        self.assertLess(trap, first_mutation)

    def test_aux_group_capabilities_are_checked_before_capture(self) -> None:
        live_gate = self.source.index("API Key 分组未启用 Live")
        image_gate = self.source.index("API Key 分组未启用图片生成")
        snapshot = self.source.index('docker cp "$service_container:/etc/hosts"')
        self.assertLess(live_gate, snapshot)
        self.assertLess(image_gate, snapshot)

    def test_a12_counts_target_profile_settings_request(self) -> None:
        self.assertIn('"wham_settings_user": 2', self.source)
        self.assertIn('"wham_usage": 2', self.source)
        self.assertIn('"wham_credit_details": 2', self.source)
        self.assertIn('"wham_safe_consume": 1', self.source)

    def test_compact_trigger_carries_strict_official_identity(self) -> None:
        self.assertIn('"prompt_cache_key":"%s"', self.source)
        self.assertIn('"text":{"verbosity":"low"}', self.source)
        self.assertIn("compact_installation_id=", self.source)
        self.assertIn("compact_session_id=", self.source)
        self.assertIn("compact_turn_id=", self.source)
        self.assertIn('-H "X-Codex-Installation-ID: $compact_installation_id"', self.source)
        self.assertIn('-H "Session-Id: $compact_session_id"', self.source)
        self.assertIn('-H "Thread-Id: $compact_session_id"', self.source)
        self.assertIn('-H "X-Codex-Window-Id: $compact_window_id"', self.source)
        self.assertIn('-H "X-Codex-Turn-Metadata: $compact_turn_metadata"', self.source)
        self.assertIn("for variant in prime default beta turn_state", self.source)
        self.assertIn(
            "beta | turn_state) compact_turn_id=22222222-2222-4222-8222-222222222221",
            self.source,
        )
        self.assertNotIn("X-Codex-Turn-State: candidate-aux-turn-state", self.source)
        self.assertIn('"legacy_compact": 4', self.source)

    def test_text_and_image_scenarios_use_separate_models(self) -> None:
        # 文本模型由 Campaign 显式注入；这里只要求两者是不同变量。
        self.assertIn("model=${MODEL:?", self.source)
        self.assertIn("image_model=${IMAGE_MODEL:-gpt-image-2}", self.source)

        compact = self.source[
            self.source.index("compact_body=$(printf") : self.source.index(
                "for variant in", self.source.index("compact_body=$(printf")
            )
        ]
        self.assertIn('"$model" "$compact_session_id"', compact)
        self.assertNotIn("$image_model", compact)

        search = self.source[
            self.source.index("for phase in 1 2") : self.source.index(
                'code=$(request_with_token "$api_key" --output "$trigger_root/image-generation.json"',
                self.source.index("for phase in 1 2"),
            )
        ]
        self.assertIn('"$model" "$phase"', search)
        self.assertNotIn("$image_model", search)

        generation = self.source[
            self.source.index('image-generation.json') : self.source.index(
                "assert_2xx A09-image-generation"
            )
        ]
        self.assertIn("background", generation)
        self.assertIn("quality", generation)
        self.assertIn('\\"model\\":\\"$image_model\\"', generation)
        self.assertNotIn('\\"model\\":\\"$model\\"', generation)

        edit = self.source[
            self.source.index('image-edit.json') : self.source.index(
                "assert_2xx A09-image-edit"
            )
        ]
        self.assertIn("background=auto", edit)
        self.assertIn("quality=high", edit)
        self.assertIn("size=1024x1024", edit)
        self.assertIn('-F "model=$image_model"', edit)
        self.assertNotIn('-F "model=$model"', edit)

    def test_image_model_allowlist_fixture_is_exactly_restored(self) -> None:
        self.assertIn("original_model_mapping_state=", self.source)
        self.assertIn("model_mapping_restore_armed=1", self.source)
        self.assertIn("jsonb_build_object('$image_model','$image_model')", self.source)
        self.assertIn("convert_from(decode('$model_mapping_hex','hex'),'UTF8')::jsonb", self.source)
        self.assertIn("credentials = coalesce(credentials,'{}'::jsonb) - 'model_mapping'", self.source)
        self.assertIn("account_model_mapping_equal", self.source)
        self.assertIn(
            '[[ $restored_model_mapping_state == "$original_model_mapping_state" ]]',
            self.source,
        )

    def test_live_session_ended_must_close_record_and_release_all_leases(self) -> None:
        self.assertIn("wait_live_cleanup()", self.source)
        self.assertIn('call_key="live:call:$call_hash"', self.source)
        self.assertIn('ZSCORE "concurrency:live:account:$account_id"', self.source)
        self.assertIn('ZSCORE "concurrency:live:user:$user_id"', self.source)
        self.assertIn('ZSCORE "concurrency:live:api_key:$api_key_id"', self.source)
        self.assertIn("controller_closed=true", self.source)
        sideband = self.source.index("wait_action A11 realtime_sideband")
        cleanup = self.source.index("wait_live_cleanup \"$live_call_id\"", sideband)
        stop = self.source.index("stop_capture", cleanup)
        self.assertLess(sideband, cleanup)
        self.assertLess(cleanup, stop)


if __name__ == "__main__":
    unittest.main()
