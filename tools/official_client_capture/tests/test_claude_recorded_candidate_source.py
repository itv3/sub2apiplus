"""候选录制控制来源的轻量合同拒绝例；全部来源为显式合成，不登记重链或正式版本认证。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest import mock
import unittest

from tools.official_client_capture.tests import claude_recorded_candidate_source as candidate
from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import test_claude_vc1_recorded_chain as source_fixture

PROBE = "v4-thinking-display-omitted"


class RecordedCandidateSourceTests(unittest.TestCase):
    """候选预约与历史来源必须分别闭合；不继承旧用例类，避免默认发现重复运行。"""

    def setUp(self) -> None:
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import claude_upgrade_plan as plan

        fixture = source_fixture.RecordedSourceContractTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.data, self.state = fixture.data, copy.deepcopy(fixture.state)
        original_path = fixture.fixture.supplement / f"attempts/{PROBE}/attempt-001/relay-manifest.json"
        manifest = source.read(original_path)
        manifest.update(secret_scan={"passed": True}, cleanup={"relay_stopped": True, "hosts_restored": True},
                        m_binding={"complete": True}, upstream={"host": "api.anthropic.com", "live_requests": True})
        original_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.state["snapshot"] = replay.build_snapshot(self.data / "control/candidate-snapshot", recorded=fixture.fixture.recorded)
        self.state["inputs"] = {"candidate_jobs": {"candidate-fixture": [PROBE]},
                                "candidate_configuration": {"service_container": "sub2apiplus", "keeper_container": "sub2apiplus-keeper",
                                "postgres_container": "sub2apiplus-postgres", "claude_account_id": "7", "api_key_id": "9"}}
        attempt = self.data / "evidence/campaigns/fixture/candidates/cand-r1/attempts/attempt-fixture"
        reservation = source.write(attempt / "reservation.json", {
            "schema_version": capture.CAPTURE_RESERVATION_SCHEMA, "mode": capture.MODE_FORMAL,
            "attempt_id": attempt.name, "phase": "candidate", "candidate_id": "cand-r1",
            "run_nonce": "a" * 64, "planned_jobs": [{"id": "candidate-fixture"}],
        })
        nonce = capture._step_run_nonce("a" * 64, "candidate-fixture", 1, 1)
        runtime = source.write(attempt / "runtime.json", {
            "schema_version": capture.HOST_RUNTIME_RECEIPT_SCHEMA, "run_nonce": nonce,
            "runtime_image_reference": "current@sha256:" + "8" * 64, "runtime_image_id": "sha256:" + "8" * 64,
            "container": {"name": "capture-cli", "id": "9" * 64}, "docker_server": {"os": "linux", "arch": "arm64"},
            "repo_digest_verified": True, "capture_source_bundle": {"sha256": "e" * 64, "files": []},
        })
        self.projection = copy.deepcopy(fixture.projection)
        self.projection.update(operation="job:candidate-fixture:step-1:attempt-1", job_id="candidate-fixture",
                               side="candidate", candidate_id="cand-r1", probe_id=PROBE,
                               output_root=str(self.data / "runs/candidate-fixture" / PROBE))
        self.projection["runtime"].update(receipt=source.binding(runtime), run_nonce=nonce,
                                          reservation=source.binding(reservation), attempt_id=attempt.name)
        origin = source.origin_probe(self.state, PROBE)
        old_path = attempt / "origin-runtime.json"
        old_path.write_bytes(origin["receipt_path"].read_bytes())
        self.projection["origin"].update(receipt=source.binding(old_path), run_nonce=origin["receipt"]["run_nonce"],
                                         source_id=origin["row"]["source_id"], attempt=origin["row"]["attempt"])
        self.projection["argv"] = [
            "python3", "/fixture/tools/claude_fw_e_relay.py", "capture", "--probe-id", PROBE,
            "--run-id", "fixture-cand-r1-" + PROBE, "--model", "claude-sonnet-5",
            "--expected-version", source.VERSION, "--expected-sha256", self.state["freeze"]["binary_sha256"],
            "--output-root", self.projection["output_root"], "--upstream-ip", "160.79.104.10",
        ]
        self.projection["candidate"] = {
            "run_id": "fixture-cand-r1-" + PROBE, "probe_id": PROBE, "model": "claude-sonnet-5",
            "account_id": 7, "api_key_id": 9, "base_url": "http://sub2apiplus:8080",
            "trigger": plan.candidate_trigger(PROBE), "relay_port": int(plan.CANDIDATE_RELAY_PORT), "live": True,
        }

    def test_valid_candidate_keeps_independent_origin_and_runtime(self) -> None:
        origin = candidate.verify_candidate_projection(self.state, self.projection)
        manifest = candidate._candidate_manifest(self.state, self.projection, origin)
        self.assertEqual(manifest["runtime"]["host_runtime_receipt"]["run_nonce"], self.projection["runtime"]["run_nonce"])
        self.assertNotEqual(manifest["runtime"]["host_runtime_receipt"]["run_nonce"], origin["receipt"]["run_nonce"])
        self.assertEqual(manifest["candidate_gateway"]["account_id"], 7)
        self.assertEqual(source.origin_probe(self.state, PROBE)["manifest"], origin["manifest"])

    def test_wrong_job_nonce_gateway_or_side_fails_closed(self) -> None:
        variants = []
        for field, value in (("side", "official"), ("job_id", "unknown"), ("candidate_id", "another")):
            variants.append({**copy.deepcopy(self.projection), field: value})
        gateway = copy.deepcopy(self.projection)
        gateway["candidate"]["account_id"] = 8
        variants.append(gateway)
        runtime = copy.deepcopy(self.projection)
        runtime["runtime"]["run_nonce"] = "f" * 64
        variants.append(runtime)
        for variant in variants:
            with self.subTest(variant=variant["operation"]), self.assertRaises(replay.ReplayError):
                candidate.verify_candidate_projection(self.state, variant)

    def test_incomplete_origin_cannot_be_projected_to_complete_candidate(self) -> None:
        origin = candidate.verify_candidate_projection(self.state, self.projection)
        origin["manifest"]["m_binding"]["complete"] = False
        with self.assertRaisesRegex(replay.ReplayError, "历史 relay 来源未具备"):
            candidate._candidate_manifest(self.state, self.projection, origin)

    def test_copy_preserves_all_original_attachment_bytes(self) -> None:
        self.state_path = source.write(self.data / "control/candidate-state.json", self.state)
        projection_path = source.write(self.data / "control/candidate-projection.json", self.projection)
        original_loader = source.load_state
        with mock.patch.object(source, "load_state", side_effect=lambda path, digest: original_loader(path, digest, isolation=False)):
            candidate.copy_registered(self.state_path, source.binding(self.state_path)["sha256"],
                                      projection_path, source.binding(projection_path)["sha256"])
        copied = source.read(projection_path.with_suffix(".copied.json"))
        self.assertFalse(copied["formal_version_semantics_certified"])
        origin = source.origin_probe(self.state, PROBE)
        for item in copied["files"]:
            original = Path(origin["index"]["root"]) / item["original_relative"]
            self.assertEqual(source.bound(item["copied"]).read_bytes(), original.read_bytes())
        self.assertNotEqual(source.read(source.bound(copied["candidate_manifest"])), origin["manifest"])
        replay.assert_source_untouched(self.state["snapshot"])


if __name__ == "__main__":
    unittest.main()
