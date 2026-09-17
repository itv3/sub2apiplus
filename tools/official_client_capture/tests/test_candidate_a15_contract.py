"""A15 真实 Codex 入口与服务端缓存证据合同测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.official_client_capture.candidate_rule_assertion import (
    AssertionConfigurationError,
    load_observations,
)


VERSION = "0.154.0"
PREFIX = "candidate-vc5-a15"
CONTRACT = "real-entry-cache-v2"
SCHEMA = "codex-candidate-observation/v1"
BINARY_PATH = "/opt/codex-0.154.0/bin/codex"
BINARY_SHA256 = "a" * 64
VERSION_OUTPUT = f"codex-cli {VERSION}"


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class CandidateA15ContractTest(unittest.TestCase):
    def _refresh_surface_digests(self, record: dict[str, Any]) -> None:
        data = record["data"]
        argv = data["argv"]
        argv_sha256 = canonical_sha256(argv)
        launch_sha256 = canonical_sha256(
            {
                "argv": argv,
                "codex_binary_path": data["codex_binary_path"],
                "codex_binary_sha256": data["codex_binary_sha256"],
                "codex_version_output": data["codex_version_output"],
                "pid": data["pid"],
                "started_at": data["started_at"],
                "codex_home": data["codex_home"],
                "pty": data["pty"],
                "correlation_nonce": data["correlation_nonce"],
            }
        )
        request_sha256 = canonical_sha256(
            {
                "method": data["request_method"],
                "target": data["request_target"],
                "user_agent": data["user_agent"],
                "originator": data["originator"],
                "version": data["version_header"],
                "authorization_present": data["authorization_present"],
                "http_status": data["http_status"],
                "response_body_sha256": data["response_body_sha256"],
                "response_body_bytes": data["response_body_bytes"],
                "observed_at": data["observed_at"],
            }
        )
        data["argv_sha256"] = argv_sha256
        data["launch_sha256"] = launch_sha256
        data["request_sha256"] = request_sha256
        data["correlation_sha256"] = canonical_sha256(
            {
                "argv_sha256": argv_sha256,
                "launch_sha256": launch_sha256,
                "request_sha256": request_sha256,
                "correlation_nonce": data["correlation_nonce"],
                "pid": data["pid"],
            }
        )

    def _surface_record(
        self,
        *,
        variant: str,
        nonce: str,
        pid: int,
        response_sha256: str,
        response_bytes: int,
    ) -> dict[str, Any]:
        identities = {
            "exec-startup-models": (
                "exec",
                "models",
                "codex_exec",
                f"codex_exec/{VERSION}",
                "",
                "absent",
                False,
                "miss",
                0,
                1,
                VERSION,
                "models_http_200_observed",
            ),
            "tui-startup-models": (
                "tui",
                "models",
                "codex_cli_rs",
                f"codex_cli_rs/{VERSION}",
                "",
                "absent",
                True,
                "fresh_hit",
                1,
                1,
                VERSION,
                "models_and_post_initialize_identity_observed",
            ),
            "tui-post-initialize-identity": (
                "tui",
                "plugin_identity",
                "codex-tui",
                f"codex-tui/{VERSION}",
                f"(codex-tui; {VERSION})",
                "present",
                True,
                "not_applicable",
                None,
                None,
                "",
                "models_and_post_initialize_identity_observed",
            ),
        }
        (
            surface,
            endpoint,
            originator,
            prefix,
            suffix,
            suffix_state,
            use_pty,
            cache_result,
            before,
            after,
            version_header,
            termination_reason,
        ) = identities[variant]
        user_agent = f"{prefix} (Ubuntu 24.4.0; x86_64) unknown"
        if suffix:
            user_agent += f" {suffix}"
        base_url = (
            f'openai_base_url="http://127.0.0.1:18444/a15/{nonce}'
            '/backend-api/codex"'
        )
        process_kind = "exec" if variant == "exec-startup-models" else "tui"
        if process_kind == "exec":
            argv = [BINARY_PATH, "exec", "-c", base_url, "--ephemeral"]
        else:
            argv = [BINARY_PATH, "-c", base_url, "--no-alt-screen"]
        request_target = (
            f"/a15/{nonce}/backend-api/codex/models?client_version={VERSION}"
            if endpoint == "models"
            else f"/a15/{nonce}/backend-api/ps/plugins/installed?limit=200"
        )
        record = {
            "schema_version": SCHEMA,
            "record_id": f"a15-{variant}",
            "scenario_id": "A15",
            "record_type": "surface_identity",
            "data": {
                "contract_version": CONTRACT,
                "variant": variant,
                "surface": surface,
                "endpoint": endpoint,
                "originator": originator,
                "user_agent": user_agent,
                "user_agent_prefix": prefix,
                "user_agent_suffix": suffix,
                "suffix_state": suffix_state,
                "request_method": "GET",
                "request_target": request_target,
                "version_header": version_header,
                "authorization_present": True,
                "http_status": 200,
                "response_body_sha256": response_sha256,
                "response_body_bytes": response_bytes,
                "cache_result": cache_result,
                "upstream_calls_before": before,
                "upstream_calls_after": after,
                "codex_binary_path": BINARY_PATH,
                "codex_binary_sha256": BINARY_SHA256,
                "codex_version_output": VERSION_OUTPUT,
                "pid": pid,
                "started_at": "2026-09-17T00:00:00.000+00:00",
                "observed_at": "2026-09-17T00:00:01.000+00:00",
                "finished_at": "2026-09-17T00:00:02.000+00:00",
                "returncode": -15,
                "termination_reason": termination_reason,
                "codex_home": f"/tmp/a15-{process_kind}-{nonce}",
                "argv": argv,
                "pty": use_pty,
                "correlation_nonce": nonce,
                "argv_sha256": "",
                "launch_sha256": "",
                "request_sha256": "",
                "correlation_sha256": "",
            },
        }
        self._refresh_surface_digests(record)
        return record

    def _write_manifest(self, fixture: dict[str, Any]) -> None:
        trace_path = fixture["trace_path"]
        trace_path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                + "\n"
                for record in fixture["records"]
            ),
            encoding="utf-8",
        )
        fixture["manifest"]["artifacts"][0]["sha256"] = bytes_sha256(
            trace_path.read_bytes()
        )
        fixture["manifest_path"].write_text(
            json.dumps(
                fixture["manifest"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def _refresh_contract_digest(self, fixture: dict[str, Any]) -> None:
        aggregate = fixture["records"][-1]["data"]
        payload = dict(aggregate)
        payload.pop("contract_sha256", None)
        aggregate["contract_sha256"] = canonical_sha256(payload)

    def _fixture(self, root: Path) -> dict[str, Any]:
        evidence_root = root / "evidence"
        scenario_root = evidence_root / PREFIX / "scenarios" / "A15"
        relay_root = scenario_root / "relay"
        relay_root.mkdir(parents=True)

        response_body = b'{"models":[]}'
        response_sha256 = bytes_sha256(response_body)
        identity_body = b'{"items":[]}'
        identity_sha256 = bytes_sha256(identity_body)
        canonical_user_agent = (
            f"codex_exec/{VERSION} (Ubuntu 24.4.0; x86_64) unknown "
            f"(codex_exec; {VERSION})"
        )
        request_bytes = (
            f"GET /backend-api/codex/models?client_version={VERSION} HTTP/1.1\r\n"
            "host: chatgpt.com\r\n"
            "authorization: Bearer <secret>\r\n"
            f"originator: codex_exec\r\nuser-agent: {canonical_user_agent}\r\n"
            f"version: {VERSION}\r\n\r\n"
        ).encode("ascii")
        response_bytes = (
            b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
            + f"content-length: {len(response_body)}\r\n\r\n".encode("ascii")
            + response_body
        )
        intervention_bytes = (
            b'{"type":"synthetic_core_response","action":"models_manifest",'
            b'"production_forwarded":false}\n'
        )

        request_path = relay_root / "conn001.client_to_upstream.bin"
        response_path = relay_root / "conn001.upstream_to_client.bin"
        intervention_path = relay_root / "intervention.jsonl"
        request_path.write_bytes(request_bytes)
        response_path.write_bytes(response_bytes)
        intervention_path.write_bytes(intervention_bytes)

        records = [
            self._surface_record(
                variant="exec-startup-models",
                nonce="1" * 32,
                pid=1101,
                response_sha256=response_sha256,
                response_bytes=len(response_body),
            ),
            self._surface_record(
                variant="tui-startup-models",
                nonce="2" * 32,
                pid=1102,
                response_sha256=response_sha256,
                response_bytes=len(response_body),
            ),
            self._surface_record(
                variant="tui-post-initialize-identity",
                nonce="2" * 32,
                pid=1102,
                response_sha256=identity_sha256,
                response_bytes=len(identity_body),
            ),
        ]
        request_source = (
            f"{PREFIX}/scenarios/A15/relay/conn001.client_to_upstream.bin"
        )
        response_source = (
            f"{PREFIX}/scenarios/A15/relay/conn001.upstream_to_client.bin"
        )
        intervention_source = f"{PREFIX}/scenarios/A15/relay/intervention.jsonl"
        aggregate_data = {
            "contract_version": CONTRACT,
            "entry_record_ids": [record["record_id"] for record in records[:2]],
            "post_initialize_identity_record_id": records[2]["record_id"],
            "entry_count": 2,
            "success_count": 2,
            "cache_hit_count": 1,
            "upstream_call_count": 1,
            "response_body_sha256": response_sha256,
            "codex_binary_path": BINARY_PATH,
            "codex_binary_sha256": BINARY_SHA256,
            "codex_version_output": VERSION_OUTPUT,
            "relay_request_path": request_source,
            "relay_request_sha256": bytes_sha256(request_bytes),
            "relay_response_path": response_source,
            "relay_response_sha256": bytes_sha256(response_bytes),
            "intervention_path": intervention_source,
            "intervention_sha256": bytes_sha256(intervention_bytes),
            "canonical_upstream_user_agent": canonical_user_agent,
            "canonical_upstream_originator": "codex_exec",
            "canonical_upstream_version": VERSION,
        }
        aggregate_data["contract_sha256"] = canonical_sha256(aggregate_data)
        records.append(
            {
                "schema_version": SCHEMA,
                "record_id": "a15-real-entry-cache-contract-v2",
                "scenario_id": "A15",
                "record_type": "connection_lifecycle",
                "data": aggregate_data,
                "source_artifacts": [
                    request_source,
                    response_source,
                    intervention_source,
                ],
            }
        )

        trace_path = scenario_root / "process-trace.jsonl"
        manifest_path = root / "capture-manifest.json"
        artifacts = [
            {
                "path": f"{PREFIX}/scenarios/A15/process-trace.jsonl",
                "sha256": "0" * 64,
                "kind": "process_trace",
                "parser": "observation_jsonl",
                "scenario_ids": ["A15"],
                "labels": {
                    "a15_contract": CONTRACT,
                    "provider": "openai_oauth",
                },
            },
            {
                "path": request_source,
                "sha256": bytes_sha256(request_bytes),
                "kind": "relay_binary",
                "parser": "opaque_bound_source",
                "scenario_ids": ["A15"],
                "labels": {"provider": "openai_oauth"},
            },
            {
                "path": response_source,
                "sha256": bytes_sha256(response_bytes),
                "kind": "wire_dump",
                "parser": "opaque_bound_source",
                "scenario_ids": ["A15"],
                "labels": {"provider": "openai_oauth"},
            },
            {
                "path": intervention_source,
                "sha256": bytes_sha256(intervention_bytes),
                "kind": "stdout_log",
                "parser": "opaque_bound_source",
                "scenario_ids": ["A15"],
                "labels": {"provider": "openai_oauth"},
            },
        ]
        fixture = {
            "evidence_root": evidence_root,
            "trace_path": trace_path,
            "manifest_path": manifest_path,
            "manifest": {
                "schema_version": "codex-candidate-capture-manifest/v1",
                "codex_version": VERSION,
                "capture_id": "a15-contract-test",
                "status": "complete",
                "artifacts": artifacts,
            },
            "records": records,
            "request_path": request_path,
            "response_path": response_path,
            "intervention_path": intervention_path,
        }
        self._write_manifest(fixture)
        return fixture

    def _load(self, fixture: dict[str, Any]) -> list[Any]:
        _, observations = load_observations(
            fixture["manifest_path"],
            fixture["evidence_root"],
            expected_codex_version=VERSION,
        )
        return observations

    def test_valid_real_entry_cache_contract_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            observations = self._load(fixture)
        self.assertEqual(len(observations), 4)
        self.assertEqual(
            {observation.record_type for observation in observations},
            {"surface_identity", "connection_lifecycle"},
        )

    def test_legacy_v1_label_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            fixture["manifest"]["artifacts"][0]["labels"]["a15_contract"] = (
                "real-entry-cache-v1"
            )
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "v1"):
                self._load(fixture)

    def test_exec_startup_models_suffix_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            record = fixture["records"][0]
            data = record["data"]
            suffix = f"(codex_exec; {VERSION})"
            data["user_agent"] += f" {suffix}"
            data["user_agent_suffix"] = suffix
            data["suffix_state"] = "present"
            self._refresh_surface_digests(record)
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "入口合同不匹配"):
                self._load(fixture)

    def test_tui_startup_models_cannot_claim_post_initialize_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            record = fixture["records"][1]
            data = record["data"]
            suffix = f"(codex-tui; {VERSION})"
            data["originator"] = "codex-tui"
            data["user_agent_prefix"] = f"codex-tui/{VERSION}"
            data["user_agent_suffix"] = suffix
            data["suffix_state"] = "present"
            data["user_agent"] = (
                f"codex-tui/{VERSION} (Ubuntu 24.4.0; x86_64) unknown {suffix}"
            )
            self._refresh_surface_digests(record)
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "入口合同不匹配"):
                self._load(fixture)

    def test_tui_post_initialize_identity_requires_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            record = fixture["records"][2]
            data = record["data"]
            data["user_agent"] = data["user_agent"].rsplit(" ", 1)[0]
            data["user_agent_suffix"] = ""
            data["suffix_state"] = "absent"
            self._refresh_surface_digests(record)
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "入口合同不匹配"):
                self._load(fixture)

    def test_tui_records_must_share_one_process_receipt(self) -> None:
        fields = {
            "pid": 9999,
            "argv": [BINARY_PATH, "-c", "drift", "--no-alt-screen"],
            "correlation_nonce": "9" * 32,
            "codex_home": "/tmp/a15-tui-drift",
        }
        for field, value in fields.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture = self._fixture(Path(directory))
                record = fixture["records"][2]
                record["data"][field] = value
                if field == "correlation_nonce":
                    record["data"]["request_target"] = record["data"][
                        "request_target"
                    ].replace("2" * 32, "9" * 32)
                self._refresh_surface_digests(record)
                self._write_manifest(fixture)
                with self.assertRaises(AssertionConfigurationError):
                    self._load(fixture)

    def test_missing_real_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            fixture["records"].pop(2)
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "三条"):
                self._load(fixture)

    def test_second_upstream_connection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            extra = fixture["request_path"].with_name(
                "conn002.client_to_upstream.bin"
            )
            extra.write_bytes(fixture["request_path"].read_bytes())
            with self.assertRaisesRegex(AssertionConfigurationError, "一次上游请求"):
                self._load(fixture)

    def test_nonce_argv_and_correlation_drift_are_rejected(self) -> None:
        cases = ("nonce", "argv", "correlation")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture = self._fixture(Path(directory))
                record = fixture["records"][0]
                data = record["data"]
                if case == "nonce":
                    data["request_target"] = data["request_target"].replace(
                        "1" * 32, "9" * 32
                    )
                    self._refresh_surface_digests(record)
                elif case == "argv":
                    data["argv"].append("--drift")
                else:
                    data["correlation_sha256"] = "0" * 64
                self._write_manifest(fixture)
                with self.assertRaises(AssertionConfigurationError):
                    self._load(fixture)

    def test_curl_argv_is_rejected_even_with_valid_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            record = fixture["records"][0]
            record["data"]["argv"].append("/usr/bin/curl")
            self._refresh_surface_digests(record)
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "curl"):
                self._load(fixture)

    def test_non_200_surface_response_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            record = fixture["records"][1]
            record["data"]["http_status"] = 500
            self._refresh_surface_digests(record)
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "入口合同不匹配"):
                self._load(fixture)

    def test_server_cache_count_proof_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            fixture["records"][1]["data"]["upstream_calls_after"] = 2
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "入口合同不匹配"):
                self._load(fixture)

    def test_relay_and_intervention_digest_drift_are_rejected(self) -> None:
        for field in ("relay_response_sha256", "intervention_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture = self._fixture(Path(directory))
                fixture["records"][-1]["data"][field] = "f" * 64
                self._refresh_contract_digest(fixture)
                self._write_manifest(fixture)
                with self.assertRaisesRegex(AssertionConfigurationError, field):
                    self._load(fixture)

    def test_intervention_must_prove_one_local_models_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            changed = (
                b'{"type":"synthetic_core_response","action":"other",'
                b'"production_forwarded":false}\n'
            )
            fixture["intervention_path"].write_bytes(changed)
            fixture["manifest"]["artifacts"][3]["sha256"] = bytes_sha256(changed)
            aggregate = fixture["records"][-1]["data"]
            aggregate["intervention_sha256"] = bytes_sha256(changed)
            self._refresh_contract_digest(fixture)
            self._write_manifest(fixture)
            with self.assertRaisesRegex(AssertionConfigurationError, "models_manifest"):
                self._load(fixture)


if __name__ == "__main__":
    unittest.main()
