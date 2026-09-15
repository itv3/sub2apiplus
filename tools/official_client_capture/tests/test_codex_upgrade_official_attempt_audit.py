"""official attempt 只读审计：五个章节的通过与失败关闭。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import (
    codex_upgrade_official_attempt_audit as audit,
)
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture.tests.test_codex_upgrade_live_request_provenance import (
    RESPONSES,
    _chmod_tree,
    _h1_post,
    _write_json,
    _ws_connection,
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_file(path: Path, value: object) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(raw)
    return raw


class AttemptAuditFixture:
    """一个 formal Campaign 加一份 awaiting_receipts 的 official attempt。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.runs = self.data / "runs"
        self.campaign_id = "c1"
        self.attempt_id = "20260914T232051Z-f735b7996999027b"
        self.campaign_dir = self.data / "evidence" / "campaigns" / self.campaign_id
        self.attempt_root = self.campaign_dir / "official" / "attempts" / self.attempt_id
        self.identity = {field: f"{field}-value" for field in audit.IDENTITY_FIELDS}
        self.identity["binary_sha256"] = "b" * 64
        self.identity["cli_version"] = "0.154.0"

    def build(self) -> None:
        # 证据：main 轨道 relay HTTP 两个 POST；lite 轨道 relay WS 两个 response.create。
        main_relay = self.runs / "c1-main" / "relay"
        main_relay.mkdir(parents=True, mode=0o700)
        (main_relay / "conn001.client_to_upstream.bin").write_bytes(
            _h1_post(RESPONSES, {"model": "gpt-5.5"}) + _h1_post(RESPONSES, {"model": "gpt-5.5"})
        )
        lite_relay = self.runs / "c1-lite" / "relay"
        lite_relay.mkdir(parents=True, mode=0o700)
        (lite_relay / "conn001.client_to_upstream.bin").write_bytes(
            _ws_connection([
                {"type": "response.create", "model": "gpt-6-astra"},
                {"type": "response.create", "model": "gpt-6-astra"},
            ])
        )
        # lite Job 的模型条件收据与其证据绑定。
        prewarm = self.runs / "c1-lite" / "model-catalog-prewarm.json"
        prewarm_raw = _canonical_file(prewarm, {"models": ["gpt-6-astra", "gpt-5.5"]})
        receipt = {
            "schema_version": "codex-model-condition-receipt/v1",
            "status": "success",
            "job_id": "official-lite-http-response",
            "run_id": "c1-lite",
            "track": "lite",
            "model_id": "gpt-6-astra",
            "models_response_sha256": "c" * 64,
            "use_responses_lite": True,
            "model_fallback": False,
            "observed_request_models": ["gpt-6-astra"],
            "evidence_root": "/capture/runs/c1-lite",
            "evidence_bindings": [
                {"path": "model-catalog-prewarm.json", "bytes": len(prewarm_raw), "sha256": _sha256(prewarm_raw), "roles": ["model_catalog_prewarm"]},
            ],
        }
        receipt_raw = _canonical_file(self.runs / "c1-lite" / "model-condition-receipt.json", receipt)
        main_prewarm = self.runs / "c1-main" / "model-catalog-prewarm.json"
        main_prewarm_raw = _canonical_file(main_prewarm, {"models": ["gpt-5.5"]})
        main_receipt = dict(
            receipt,
            job_id="official-relay-http-response",
            run_id="c1-main",
            track="main",
            model_id="gpt-5.5",
            use_responses_lite=False,
            observed_request_models=["gpt-5.5"],
            evidence_root="/capture/runs/c1-main",
            evidence_bindings=[
                {"path": "model-catalog-prewarm.json", "bytes": len(main_prewarm_raw), "sha256": _sha256(main_prewarm_raw), "roles": ["model_catalog_prewarm"]},
            ],
        )
        main_receipt_raw = _canonical_file(self.runs / "c1-main" / "model-condition-receipt.json", main_receipt)
        # Campaign。
        manifest = {
            "campaign_id": self.campaign_id,
            "campaign_mode": "formal",
            "campaign_purpose": "production_replacement",
            "target_version": "0.154.0",
            "created_at_utc": "2026-09-14T23:14:19Z",
            "official_identity": self.identity,
            "configuration": {"capture_root": "/capture", "codex_account_id": 22, "api_key_id": 4, "model": "gpt-5.5", "lite_model": "gpt-6-astra"},
            "jobs": [
                {"id": "official-relay-http-response", "phase": "official", "evidence_roots": ["/capture/runs/c1-main"]},
                {"id": "official-relay-http-response-plain", "phase": "official", "evidence_roots": ["/capture/runs/c1-main"]},
                {"id": "official-lite-http-response", "phase": "official", "evidence_roots": ["/capture/runs/c1-lite"]},
            ],
        }
        raw = _canonical_file(self.campaign_dir / "campaign.json", manifest)
        (self.campaign_dir / "campaign.sha256").write_text(_sha256(raw) + "\n", "ascii")
        # attempt 的环境文件。
        evidence = self.attempt_root / "evidence"
        environment: dict[str, object] = {"evidence_root": str(evidence)}
        for role, relative, payload in (
            ("before_probe", "environment/before/probe-manifest.json", {"probe": "before"}),
            ("after_probe", "environment/after/probe-manifest.json", {"probe": "after"}),
            ("arm64_before_receipt", "environment/arm64-before/receipt.json", {"status": "passed", "continuity_identity_sha256": "e" * 64}),
            ("arm64_after_receipt", "environment/arm64-after/receipt.json", {"status": "passed", "continuity_identity_sha256": "e" * 64}),
            ("restoration_report", "receipts/restoration-report.json", {"status": "passed"}),
        ):
            data = _canonical_file(evidence / relative, payload)
            environment[role] = {"path": relative, "bytes": len(data), "sha256": _sha256(data)}
        # checkpoint 链。
        store = incremental_recovery.CheckpointStore(self.attempt_root / "checkpoints", create=True)
        for job_id in ("official-relay-http-response", "official-relay-http-response-plain", "official-lite-http-response"):
            store.append({"item_id": job_id, "status": "complete", "disposition": "executed", "previous_checkpoint_sha256": (store.records()[-1]["checkpoint_sha256"] if store.records() else None)})
        records = store.records()
        binary = {"passed": True, "expected_sha256": "b" * 64, "expected_version": "0.154.0", "package": {}}
        _canonical_file(self.attempt_root / "official-binary-verification.json", binary)
        reservation = {
            "attempt_id": self.attempt_id,
            "campaign_id": self.campaign_id,
            "campaign_manifest_sha256": _sha256(raw),
            "phase": "official",
            "campaign_mode": "formal",
            "run_nonce": "n" * 64,
        }
        _canonical_file(self.attempt_root / "reservation.json", reservation)
        attempt = {
            "attempt_id": self.attempt_id,
            "campaign_id": self.campaign_id,
            "phase": "official",
            "status": "awaiting_receipts",
            "identity": self.identity,
            "binary_verification": binary,
            "environment": environment,
            "restoration_error": None,
            "execution_error": None,
            "evidence_roots": ["/capture/runs/c1-main", "/capture/runs/c1-lite"],
            "job_checkpoint": {"record_count": len(records), "last_sequence": len(records), "last_sha256": records[-1]["checkpoint_sha256"]},
            "results": [
                {
                    "id": "official-relay-http-response", "track": "main", "model_id": "gpt-5.5",
                    "expected_use_responses_lite": False, "status": "complete", "required_model_receipt": True,
                    "model_condition_receipt": {"path": "/capture/runs/c1-main/model-condition-receipt.json", "sha256": _sha256(main_receipt_raw)},
                },
                {"id": "official-relay-http-response-plain", "track": "main", "model_id": "gpt-5.5", "expected_use_responses_lite": False, "status": "complete", "required_model_receipt": False},
                {
                    "id": "official-lite-http-response", "track": "lite", "model_id": "gpt-6-astra",
                    "expected_use_responses_lite": True, "status": "complete", "required_model_receipt": True,
                    "model_condition_receipt": {"path": "/capture/runs/c1-lite/model-condition-receipt.json", "sha256": _sha256(receipt_raw)},
                },
            ],
        }
        _canonical_file(self.attempt_root / "attempt.json", attempt)
        _chmod_tree(self.data)

    def run(self, **kwargs: object) -> dict:
        return audit.audit_official_attempt(self.campaign_dir, attempt_id=self.attempt_id, **kwargs)


class OfficialAttemptAuditTests(unittest.TestCase):
    def test_complete_attempt_passes_all_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.run(expected_track_models={"main": "gpt-5.5", "lite": "gpt-6-astra"})
            self.assertEqual(receipt["status"], "passed", receipt.get("failed_sections"))
            self.assertEqual(receipt["identity"]["mismatched_fields"], [])
            self.assertTrue(receipt["account"]["passed"])
            self.assertEqual(receipt["account"]["codex_account_id"], 22)
            self.assertEqual(receipt["models"]["track_catalog"], {"lite": ["gpt-6-astra"], "main": ["gpt-5.5"]})
            plain = [j for j in receipt["models"]["jobs"] if j["job_id"] == "official-relay-http-response-plain"][0]
            self.assertNotIn("receipt", plain)
            # 同一 relay 根被两个 Job 引用，请求归首个 Job，无收据 Job 靠轨道目录交叉验证。
            self.assertEqual(plain["wire_models"], [])
            self.assertTrue(plain["track_catalog_consistent"])
            self.assertEqual(receipt["models"]["receipts_passed"], 2)
            self.assertEqual(receipt["environment"]["continuity"]["before_identity"], "e" * 64)
            self.assertEqual(receipt["integrity"]["checkpoint_count"], 3)
            self.assertEqual(receipt["integrity"]["inventory_file_count"], 6)
            self.assertEqual(receipt["requests"]["precise_total"], 4)
            self.assertEqual(receipt["permissions"]["nonconforming_entries"], 0)

    def test_main_track_without_receipt_fails_catalog_consistency(self) -> None:
        """未由收据冻结的轨道不能仅凭 wire 模型通过交叉验证。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            attempt_path = fixture.attempt_root / "attempt.json"
            attempt = json.loads(attempt_path.read_text("utf-8"))
            attempt["results"][0]["required_model_receipt"] = False
            attempt["results"][0].pop("model_condition_receipt")
            _canonical_file(attempt_path, attempt)
            attempt_path.chmod(0o600)
            receipt = fixture.run()
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["failed_sections"], ["models"])
            self.assertEqual(receipt["models"]["track_catalog"], {"lite": ["gpt-6-astra"]})

    def test_expected_track_model_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            receipt = fixture.run(expected_track_models={"lite": "gpt-5.6-luna"})
            self.assertIn("models", receipt["failed_sections"])
            self.assertFalse(receipt["models"]["expected_track_models"]["lite"]["passed"])

    def test_identity_drift_and_receipt_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            attempt_path = fixture.attempt_root / "attempt.json"
            attempt = json.loads(attempt_path.read_text("utf-8"))
            attempt["identity"]["git_commit"] = "drifted"
            attempt["results"][2]["model_condition_receipt"]["sha256"] = "0" * 64
            _canonical_file(attempt_path, attempt)
            attempt_path.chmod(0o600)
            receipt = fixture.run(expected_track_models={"lite": "gpt-6-astra"})
            self.assertEqual(receipt["identity"]["mismatched_fields"], ["git_commit"])
            lite = [j for j in receipt["models"]["jobs"] if j["job_id"] == "official-lite-http-response"][0]
            self.assertFalse(lite["receipt"]["passed"])
            self.assertIn("收据摘要与 attempt 绑定不一致", lite["receipt"]["problems"])
            self.assertEqual(sorted(receipt["failed_sections"]), ["identity", "models"])

    def test_environment_discontinuity_and_checkpoint_tail_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            attempt_path = fixture.attempt_root / "attempt.json"
            attempt = json.loads(attempt_path.read_text("utf-8"))
            after = fixture.attempt_root / "evidence" / "environment" / "arm64-after" / "receipt.json"
            data = _canonical_file(after, {"status": "passed", "continuity_identity_sha256": "f" * 64})
            after.chmod(0o600)
            attempt["environment"]["arm64_after_receipt"]["sha256"] = _sha256(data)
            attempt["environment"]["arm64_after_receipt"]["bytes"] = len(data)
            attempt["job_checkpoint"]["last_sha256"] = "1" * 64
            _canonical_file(attempt_path, attempt)
            attempt_path.chmod(0o600)
            receipt = fixture.run(expected_track_models={"lite": "gpt-6-astra"})
            self.assertIn("前后 ARM64 环境身份不连续", receipt["environment"]["problems"])
            self.assertIn("job_checkpoint.last_sha256 与链尾不一致", receipt["integrity"]["problems"])
            self.assertEqual(sorted(receipt["failed_sections"]), ["environment", "integrity"])

    def test_broken_checkpoint_chain_is_an_audit_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            second = fixture.attempt_root / "checkpoints" / "00000003.json"
            payload = json.loads(second.read_text("utf-8"))
            payload["status"] = "failed"
            second.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), "utf-8")
            second.chmod(0o600)
            with self.assertRaisesRegex(audit.AttemptAuditError, "checkpoint 链重放失败"):
                fixture.run()

    def test_cli_reports_status_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            output = fixture.root / "out" / "audit.json"
            code = audit.main([
                "--campaign-dir", str(fixture.campaign_dir),
                "--attempt-id", fixture.attempt_id,
                "--expected-track-model", "main=gpt-5.5",
                "--expected-track-model", "lite=gpt-6-astra",
                "--output", str(output),
            ])
            self.assertEqual(code, 0)
            payload = json.loads(output.read_text("utf-8"))
            self.assertEqual(payload["status"], "passed")
            self.assertEqual(audit.main([
                "--campaign-dir", str(fixture.campaign_dir),
                "--attempt-id", fixture.attempt_id,
                "--output", str(output),
            ]), 2)

    def _rebuild_main_relay(self, fixture: AttemptAuditFixture, *files: tuple[str, bytes]) -> None:
        """在 main 轨道的 relay 根内补写连接文件并恢复权限。"""

        relay = fixture.runs / "c1-main" / "relay"
        for name, raw in files:
            (relay / name).write_bytes(raw)
        _chmod_tree(fixture.data)

    def _write_controlled_catalog(self, fixture: AttemptAuditFixture, second: str) -> None:
        _canonical_file(
            fixture.runs / "c1-main" / "model-downshift-catalog.json",
            {"models": [{"slug": "gpt-5.5"}, {"slug": second}]},
        )
        _chmod_tree(fixture.data)

    def test_controlled_catalog_second_model_equal_to_campaign_lite_passes(self) -> None:
        """压缩场景切到受控目录声明的第二模型，且它等于 Campaign 冻结的 Lite 模型。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            self._write_controlled_catalog(fixture, "gpt-6-astra")
            self._rebuild_main_relay(
                fixture, ("conn002.client_to_upstream.bin", _h1_post(RESPONSES, {"model": "gpt-6-astra"}))
            )
            receipt = fixture.run()
            self.assertEqual(receipt["status"], "passed", receipt["models"]["problems"])
            main = [j for j in receipt["models"]["jobs"] if j["job_id"] == "official-relay-http-response"][0]
            self.assertEqual(main["declared_second_models"], ["gpt-6-astra"])
            self.assertEqual(main["user_thread_models"], ["gpt-5.5", "gpt-6-astra"])
            self.assertEqual(len(main["controlled_catalogs"]), 1)

    def test_controlled_catalog_second_model_must_equal_campaign_lite(self) -> None:
        """受控目录第二模型与 Campaign Lite 模型不同：如实标红并给出可复核理由。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            self._write_controlled_catalog(fixture, "gpt-5.3-codex-spark")
            self._rebuild_main_relay(
                fixture, ("conn002.client_to_upstream.bin", _h1_post(RESPONSES, {"model": "gpt-5.3-codex-spark"}))
            )
            receipt = fixture.run()
            self.assertEqual(receipt["failed_sections"], ["models"])
            main = [j for j in receipt["models"]["jobs"] if j["job_id"] == "official-relay-http-response"][0]
            self.assertFalse(main["wire_consistent"])
            self.assertEqual(len(main["problems"]), 1)
            self.assertIn("不等于 Campaign Lite 模型 gpt-6-astra", main["problems"][0])
            self.assertIn("gpt-5.3-codex-spark", main["problems"][0])

    def test_undeclared_user_thread_model_fails(self) -> None:
        """没有受控目录声明时，用户线程出现第二个模型就是失败。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            self._rebuild_main_relay(
                fixture, ("conn002.client_to_upstream.bin", _h1_post(RESPONSES, {"model": "gpt-6-astra"}))
            )
            receipt = fixture.run()
            self.assertEqual(receipt["failed_sections"], ["models"])
            self.assertEqual(
                receipt["models"]["problems"],
                ["official-relay-http-response: 用户线程出现未声明模型：['gpt-6-astra']"],
            )

    def test_system_thread_models_are_recorded_but_not_judged(self) -> None:
        """官方客户端自发的系统线程模型只记录不判定；缺少 model 字段的请求判失败。"""

        system_metadata = json.dumps({"thread_source": "system", "request_kind": "prewarm"})
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttemptAuditFixture(Path(directory).resolve())
            fixture.build()
            self._rebuild_main_relay(
                fixture,
                (
                    "conn002.client_to_upstream.bin",
                    _ws_connection([
                        {
                            "type": "response.create", "model": "gpt-5.6-luna",
                            "client_metadata": {"x-codex-turn-metadata": system_metadata},
                        }
                    ]),
                ),
            )
            receipt = fixture.run()
            self.assertEqual(receipt["status"], "passed", receipt["models"]["problems"])
            main = [j for j in receipt["models"]["jobs"] if j["job_id"] == "official-relay-http-response"][0]
            self.assertEqual(main["system_thread_models"], ["gpt-5.6-luna"])
            self.assertEqual(main["user_thread_models"], ["gpt-5.5"])
            self.assertEqual(main["wire_models"], ["gpt-5.5", "gpt-5.6-luna"])
            self.assertEqual(receipt["requests"]["precise_total"], 5)

            self._rebuild_main_relay(
                fixture, ("conn003.client_to_upstream.bin", _h1_post(RESPONSES, {"input": []}))
            )
            receipt = fixture.run()
            self.assertEqual(receipt["failed_sections"], ["models"])
            self.assertEqual(
                receipt["models"]["problems"],
                ["official-relay-http-response: 1 条模型端点请求缺少 model 字段"],
            )


if __name__ == "__main__":
    unittest.main()
