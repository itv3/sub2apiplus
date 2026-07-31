from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.official_client_capture import candidate_evidence_guard
from tools.official_client_capture import codex_upgrade_receipt_finalizer as finalizer


class ReceiptFinalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.sha_a = "a" * 64
        self.sha_b = "b" * 64
        self.image_id = f"sha256:{'c' * 64}"
        self.image_reference = f"registry/sub2api@sha256:{'d' * 64}"
        self.attempt_binding = {
            "campaign_id": "campaign-146",
            "attempt_id": "attempt-146-1",
            "run_nonce": "e" * 64,
        }
        self.time_window = {
            "attempt_started_at_utc": "2026-07-31T00:00:00Z",
            "client_checkpoint_at_utc": "2026-07-31T00:00:10Z",
        }
        self.identity = {
            "candidate_id": "candidate-r1",
            "target_version": "0.146.0",
            "profile_id": "codex-cli-0.146.0",
            "profile_digest": self.sha_a,
            "source_tree_sha256": self.sha_b,
            "build_id": "build-146",
            "deployed_version": "release-146",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, arguments: list[str]) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return finalizer.main(arguments)

    def _replace_cli_value(
        self,
        arguments: list[str],
        option: str,
        value: str,
    ) -> None:
        arguments[arguments.index(option) + 1] = value

    def _write_json(self, name: str, payload: object) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _write_snapshot(self, name: str, state: dict[str, object]) -> Path:
        path = self.root / name
        path.write_bytes(candidate_evidence_guard.normalize_state(state))
        path.chmod(0o600)
        return path

    def _primary_key_fingerprint(self, *values: int) -> str:
        return hashlib.sha256(
            json.dumps(list(values), separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _database_state(
        self,
        *,
        account_ids: tuple[int, ...] = (1, 2),
        usage_row_count: int = 10,
        usage_max_id: int = 10,
    ) -> dict[str, object]:
        protected_tables: list[dict[str, object]] = []
        for name, columns in sorted(finalizer.DATABASE_PROTECTED_TABLES.items()):
            if name == "accounts":
                rows = [(value,) for value in account_ids]
            elif name == "account_groups":
                rows = [(1, 1)]
            else:
                rows = [(1,)]
            fingerprints = sorted(
                self._primary_key_fingerprint(*row) for row in rows
            )
            protected_tables.append(
                {
                    "exists": True,
                    "name": name,
                    "primary_key_columns": list(columns),
                    "primary_key_fingerprints": fingerprints,
                    "row_count": len(fingerprints),
                }
            )
        watermarks = []
        for name in sorted(finalizer.DATABASE_WATERMARK_TABLES):
            row_count = usage_row_count if name == "usage_logs" else 3
            max_id = usage_max_id if name == "usage_logs" else 3
            watermarks.append(
                {
                    "exists": True,
                    "max_id": max_id,
                    "name": name,
                    "row_count": row_count,
                }
            )
        return {
            "append_only_watermarks": watermarks,
            "comparison_policy": dict(finalizer.DATABASE_COMPARISON_POLICY),
            "probe_kind": "database",
            "protected_tables": protected_tables,
        }

    def _restoration_argv(self, output: str = "restoration-report.json") -> list[str]:
        arguments = [
            "restoration",
            "--evidence-root",
            str(self.root),
            "--output",
            output,
            "--phase",
            "candidate",
            "--candidate-id",
            self.identity["candidate_id"],
        ]
        for check_id, before_name, after_name, _ in finalizer.RESTORATION_INPUTS:
            if check_id == "database_state_preserved":
                before_state = self._database_state()
                after_state = self._database_state(
                    account_ids=(1, 2, 3),
                    usage_row_count=11,
                    usage_max_id=11,
                )
            else:
                before_state = {"check": check_id, "value": 1}
                after_state = dict(before_state)
            before = self._write_snapshot(f"{before_name}.json", before_state)
            after = self._write_snapshot(f"{after_name}.json", after_state)
            arguments.extend(
                [
                    f"--{before_name.replace('_', '-')}",
                    before.name,
                    f"--{after_name.replace('_', '-')}",
                    after.name,
                ]
            )
        return arguments

    def _observed_event(self) -> dict[str, object]:
        return {
            "schema_version": finalizer.RUNTIME_AUDIT_SCHEMA,
            "source": "sub2api-runtime",
            "event_type": "profile_activated",
            "event_id": "profile-event-1",
            **self.attempt_binding,
            **self.identity,
            "image_id": self.image_id,
            "image_reference": self.image_reference,
            "observed_at_utc": "2026-07-31T00:00:01Z",
        }

    def _observed_argv(self, output: str = "observed-profile.json") -> list[str]:
        runtime = self._write_json("profile-runtime.json", self._observed_event())
        return [
            "observed-profile",
            "--evidence-root",
            str(self.root),
            "--output",
            output,
            "--campaign-id",
            self.attempt_binding["campaign_id"],
            "--attempt-id",
            self.attempt_binding["attempt_id"],
            "--run-nonce",
            self.attempt_binding["run_nonce"],
            "--attempt-started-at-utc",
            self.time_window["attempt_started_at_utc"],
            "--client-checkpoint-at-utc",
            self.time_window["client_checkpoint_at_utc"],
            "--candidate-id",
            self.identity["candidate_id"],
            "--target-version",
            self.identity["target_version"],
            "--profile-id",
            self.identity["profile_id"],
            "--profile-digest",
            self.identity["profile_digest"],
            "--image-id",
            self.image_id,
            "--image-reference",
            self.image_reference,
            "--source-tree-sha256",
            self.identity["source_tree_sha256"],
            "--build-id",
            self.identity["build_id"],
            "--deployed-version",
            self.identity["deployed_version"],
            "--runtime-audit",
            runtime.name,
        ]

    def _kilo_facts(self) -> dict[str, Path]:
        installation = self._write_json(
            "kilo-installation.json",
            {
                "schema_version": finalizer.KILO_INSTALLATION_SCHEMA,
                "source": "kilo-installation",
                "installation_id": "kilo-install-1",
                "product_id": "kilo",
                "display_name": "Kilo Code",
                "client_version": "4.0.0",
                "executable_path": "/Applications/Kilo Code.app/Contents/MacOS/Kilo",
                "executable_sha256": "d" * 64,
                "observed_at_utc": "2026-07-30T23:59:59Z",
            },
        )
        ingress = self._write_json(
            "kilo-ingress.json",
            {
                "schema_version": finalizer.KILO_INGRESS_SCHEMA,
                "source": "kilo-ingress",
                "witness_id": "ingress-witness-1",
                "request_id": "request-1",
                **self.attempt_binding,
                "installation_id": "kilo-install-1",
                "client_id": "kilo-compatible",
                "client_version": "4.0.0",
                "protocol": "openai-compatible",
                "entrypoint": "/v1/chat/completions",
                "model": "gpt-5.6-luna",
                "candidate_id": self.identity["candidate_id"],
                "target_version": self.identity["target_version"],
                "received_at_utc": "2026-07-31T00:00:01Z",
            },
        )
        runtime = self._write_json(
            "request-runtime.json",
            {
                "schema_version": finalizer.RUNTIME_AUDIT_SCHEMA,
                "source": "sub2api-runtime",
                "event_type": "oauth_request_forwarded",
                "event_id": "runtime-event-1",
                "request_id": "request-1",
                **self.attempt_binding,
                "ingress_witness_id": "ingress-witness-1",
                "installation_id": "kilo-install-1",
                "client_id": "kilo-compatible",
                "protocol": "openai-compatible",
                "entrypoint": "/v1/chat/completions",
                "model": "gpt-5.6-luna",
                "candidate_id": self.identity["candidate_id"],
                "target_version": self.identity["target_version"],
                "profile_id": self.identity["profile_id"],
                "profile_digest": self.identity["profile_digest"],
                "image_id": self.image_id,
                "source_tree_sha256": self.identity["source_tree_sha256"],
                "build_id": self.identity["build_id"],
                "deployed_version": self.identity["deployed_version"],
                "auth_mode": "oauth",
                "oauth_account_id": 90,
                "upstream_endpoint": "/backend-api/codex/responses",
                "transport": "http",
                "affected_branches": ["http"],
                "observed_at_utc": "2026-07-31T00:00:02Z",
            },
        )
        response = self._write_json(
            "kilo-response.json",
            {
                "schema_version": finalizer.KILO_RESPONSE_SCHEMA,
                "source": "kilo-response",
                "witness_id": "response-witness-1",
                "request_id": "request-1",
                **self.attempt_binding,
                "installation_id": "kilo-install-1",
                "client_id": "kilo-compatible",
                "candidate_id": self.identity["candidate_id"],
                "http_status": 200,
                "response_id": "response-1",
                "completed_at_utc": "2026-07-31T00:00:03Z",
            },
        )
        usage = self._write_json(
            "usage-audit.json",
            {
                "schema_version": finalizer.USAGE_AUDIT_SCHEMA,
                "source": "sub2api-usage",
                "event_id": "usage-event-1",
                "request_id": "request-1",
                **self.attempt_binding,
                "response_id": "response-1",
                "candidate_id": self.identity["candidate_id"],
                "usage_id": "usage-1",
                "oauth_account_id": 90,
                "recorded_at_utc": "2026-07-31T00:00:04Z",
            },
        )
        return {
            "installation": installation,
            "ingress": ingress,
            "runtime_audit": runtime,
            "response_witness": response,
            "usage_audit": usage,
        }

    def _kilo_argv(
        self,
        facts: dict[str, Path] | None = None,
        output: str = "kilo-compatible.json",
        client_id: str = "kilo-compatible",
    ) -> list[str]:
        facts = facts or self._kilo_facts()
        return [
            "kilo-binding",
            "--evidence-root",
            str(self.root),
            "--output",
            output,
            "--campaign-id",
            self.attempt_binding["campaign_id"],
            "--attempt-id",
            self.attempt_binding["attempt_id"],
            "--run-nonce",
            self.attempt_binding["run_nonce"],
            "--attempt-started-at-utc",
            self.time_window["attempt_started_at_utc"],
            "--client-checkpoint-at-utc",
            self.time_window["client_checkpoint_at_utc"],
            "--client-id",
            client_id,
            "--candidate-id",
            self.identity["candidate_id"],
            "--target-version",
            self.identity["target_version"],
            "--profile-id",
            self.identity["profile_id"],
            "--profile-digest",
            self.identity["profile_digest"],
            "--candidate-image-id",
            self.image_id,
            "--source-tree-sha256",
            self.identity["source_tree_sha256"],
            "--build-id",
            self.identity["build_id"],
            "--deployed-version",
            self.identity["deployed_version"],
            "--model",
            "gpt-5.6-luna",
            "--installation",
            facts["installation"].name,
            "--ingress",
            facts["ingress"].name,
            "--runtime-audit",
            facts["runtime_audit"].name,
            "--response-witness",
            facts["response_witness"].name,
            "--usage-audit",
            facts["usage_audit"].name,
        ]

    def test_restoration_derives_equal_and_database_subset_checks(self) -> None:
        self.assertEqual(self._run(self._restoration_argv()), 0)
        output = self.root / "restoration-report.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], finalizer.RESTORATION_SCHEMA)
        self.assertEqual(payload["status"], "restored")
        comparators = {
            check["id"]: check["comparator"] for check in payload["checks"]
        }
        self.assertEqual(
            comparators["database_state_preserved"], "before_subset"
        )
        self.assertEqual(
            set(comparators.values()), {"byte_equal", "before_subset"}
        )
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        producer = payload["producer"]
        core = {
            key: value
            for key, value in producer.items()
            if key != "command_sha256"
        }
        expected = hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(producer["command_sha256"], expected)
        self.assertEqual(len(producer["input_bindings"]), 10)

    def test_restoration_rejects_same_inode(self) -> None:
        arguments = self._restoration_argv()
        before = self.root / "service_before.json"
        after = self.root / "service_after.json"
        after.unlink()
        os.link(before, after)
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "restoration-report.json").exists())

    def test_restoration_rejects_missing_database_primary_key(self) -> None:
        arguments = self._restoration_argv()
        self._write_snapshot(
            "database_after.json",
            self._database_state(
                account_ids=(1,),
                usage_row_count=11,
                usage_max_id=11,
            ),
        )
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "restoration-report.json").exists())

    def test_restoration_rejects_decreasing_database_watermark(self) -> None:
        arguments = self._restoration_argv()
        self._write_snapshot(
            "database_after.json",
            self._database_state(
                account_ids=(1, 2, 3),
                usage_row_count=9,
                usage_max_id=9,
            ),
        )
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "restoration-report.json").exists())

    def test_restoration_rejects_decreasing_max_id_with_growing_rows(self) -> None:
        arguments = self._restoration_argv()
        self._write_snapshot(
            "database_after.json",
            self._database_state(
                account_ids=(1, 2, 3),
                usage_row_count=11,
                usage_max_id=9,
            ),
        )
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "restoration-report.json").exists())

    def test_restoration_rejects_changed_table_exists_state(self) -> None:
        arguments = self._restoration_argv()
        state = self._database_state(account_ids=(1, 2, 3))
        settings = next(
            table
            for table in state["protected_tables"]
            if table["name"] == "settings"
        )
        settings.update(
            {
                "exists": False,
                "primary_key_fingerprints": [],
                "row_count": None,
            }
        )
        self._write_snapshot("database_after.json", state)
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "restoration-report.json").exists())

    def test_restoration_rejects_missing_protected_table(self) -> None:
        arguments = self._restoration_argv()
        state = self._database_state(account_ids=(1, 2, 3))
        state["protected_tables"] = [
            table
            for table in state["protected_tables"]
            if table["name"] != "settings"
        ]
        self._write_snapshot("database_after.json", state)
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "restoration-report.json").exists())

    def test_restoration_rejects_unsorted_primary_key_fingerprints(self) -> None:
        arguments = self._restoration_argv()
        state = self._database_state(account_ids=(1, 2, 3))
        accounts = next(
            table
            for table in state["protected_tables"]
            if table["name"] == "accounts"
        )
        accounts["primary_key_fingerprints"] = list(
            reversed(accounts["primary_key_fingerprints"])
        )
        self._write_snapshot("database_after.json", state)
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "restoration-report.json").exists())

    def test_replay_restoration_is_read_only_and_rejects_tampered_check(
        self,
    ) -> None:
        self.assertEqual(self._run(self._restoration_argv()), 0)
        output = self.root / "restoration-report.json"
        original = output.read_bytes()
        replayed = finalizer.replay_receipt(
            output,
            self.root,
            expected_subcommand="restoration",
        )
        self.assertEqual(replayed["status"], "restored")
        self.assertEqual(output.read_bytes(), original)

        tampered = json.loads(output.read_text(encoding="utf-8"))
        tampered["checks"][0]["passed"] = False
        self._write_json(output.name, tampered)
        tampered_bytes = output.read_bytes()
        with self.assertRaises(finalizer.ReceiptFinalizerError):
            finalizer.replay_receipt(output, self.root, "restoration")
        self.assertEqual(output.read_bytes(), tampered_bytes)

    def test_replay_restoration_rejects_rebound_foreign_tool_path(self) -> None:
        self.assertEqual(self._run(self._restoration_argv()), 0)
        output = self.root / "restoration-report.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
        payload["producer"]["tool"]["path"] = "/tmp/foreign-finalizer.py"
        core = {
            key: value
            for key, value in payload["producer"].items()
            if key != "command_sha256"
        }
        payload["producer"]["command_sha256"] = hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._write_json(output.name, payload)
        with self.assertRaises(finalizer.ReceiptFinalizerError):
            finalizer.replay_receipt(output, self.root, "restoration")

    def test_observed_profile_binds_runtime_event_and_identity(self) -> None:
        self.assertEqual(self._run(self._observed_argv()), 0)
        payload = json.loads(
            (self.root / "observed-profile.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["schema_version"], finalizer.OBSERVED_PROFILE_SCHEMA)
        self.assertEqual(payload["profile_digest"], self.identity["profile_digest"])
        self.assertEqual(payload["runtime_event"]["path"], "profile-runtime.json")
        for field, value in {
            **self.attempt_binding,
            **self.time_window,
        }.items():
            self.assertEqual(payload[field], value)
            self.assertEqual(
                payload["producer"]["canonical_arguments"][field],
                value,
            )
        self.assertNotIn("status", self._observed_event())

    def test_observed_profile_rejects_cross_attempt_runtime_replay(self) -> None:
        arguments = self._observed_argv()
        self._replace_cli_value(arguments, "--attempt-id", "attempt-146-2")
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "observed-profile.json").exists())

    def test_observed_profile_rejects_invalid_run_nonce(self) -> None:
        arguments = self._observed_argv()
        self._replace_cli_value(arguments, "--run-nonce", "E" * 64)
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "observed-profile.json").exists())

    def test_observed_profile_rejects_event_outside_attempt_window(self) -> None:
        for observed_at in (
            "2026-07-30T23:59:59Z",
            "2026-07-31T00:00:11Z",
        ):
            with self.subTest(observed_at=observed_at):
                arguments = self._observed_argv()
                event = self._observed_event()
                event["observed_at_utc"] = observed_at
                self._write_json("profile-runtime.json", event)
                self.assertEqual(self._run(arguments), 2)
                self.assertFalse((self.root / "observed-profile.json").exists())

    def test_observed_profile_rejects_wrong_command_binding(self) -> None:
        event = self._observed_event()
        arguments = self._observed_argv()
        event["profile_digest"] = "e" * 64
        self._write_json("profile-runtime.json", event)
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "observed-profile.json").exists())

    def test_observed_profile_rejects_input_active_claim(self) -> None:
        arguments = self._observed_argv()
        event = self._observed_event()
        event["status"] = "active"
        self._write_json("profile-runtime.json", event)
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "observed-profile.json").exists())

    def test_observed_profile_rejects_non_rfc3339_time(self) -> None:
        arguments = self._observed_argv()
        event = self._observed_event()
        event["observed_at_utc"] = "2026-07-31 00:00:00"
        self._write_json("profile-runtime.json", event)
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "observed-profile.json").exists())

    def test_replay_observed_profile_rejects_tampered_producer(self) -> None:
        self.assertEqual(self._run(self._observed_argv()), 0)
        output = self.root / "observed-profile.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
        payload["producer"]["command_sha256"] = "0" * 64
        self._write_json(output.name, payload)
        tampered_bytes = output.read_bytes()
        with self.assertRaises(finalizer.ReceiptFinalizerError):
            finalizer.replay_receipt(
                output,
                self.root,
                expected_subcommand="observed-profile",
            )
        self.assertEqual(output.read_bytes(), tampered_bytes)

    def test_replay_observed_profile_rejects_rebound_output_argument(self) -> None:
        self.assertEqual(self._run(self._observed_argv()), 0)
        output = self.root / "observed-profile.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
        payload["producer"]["canonical_arguments"]["output"] = "other.json"
        core = {
            key: value
            for key, value in payload["producer"].items()
            if key != "command_sha256"
        }
        payload["producer"]["command_sha256"] = hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._write_json(output.name, payload)
        with self.assertRaises(finalizer.ReceiptFinalizerError):
            finalizer.replay_receipt(output, self.root, "observed-profile")

    def test_output_is_exclusive_and_preserves_handwritten_receipt(self) -> None:
        output = self._write_json(
            "observed-profile.json",
            {"schema_version": finalizer.OBSERVED_PROFILE_SCHEMA, "status": "active"},
        )
        before = output.read_bytes()
        self.assertEqual(self._run(self._observed_argv()), 2)
        self.assertEqual(output.read_bytes(), before)

    def test_kilo_binding_derives_success_from_five_correlated_facts(self) -> None:
        self.assertEqual(self._run(self._kilo_argv()), 0)
        payload = json.loads(
            (self.root / "kilo-compatible.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["schema_version"], finalizer.CLIENT_BINDING_SCHEMA)
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["response_proof"]["usage_recorded"])
        self.assertEqual(payload["response_proof"]["usage_account_id"], 90)
        for field, value in self.attempt_binding.items():
            self.assertEqual(payload[field], value)
            self.assertEqual(payload["request_proof"][field], value)
            self.assertEqual(payload["response_proof"][field], value)
            self.assertEqual(
                payload["producer"]["canonical_arguments"][field],
                value,
            )
        for field, value in self.time_window.items():
            self.assertEqual(payload[field], value)
            self.assertEqual(
                payload["producer"]["canonical_arguments"][field],
                value,
            )
        self.assertEqual(set(payload["raw_evidence"]), {
            "installation",
            "ingress",
            "response_witness",
            "runtime_audit",
            "usage_audit",
        })

    def test_kilo_binding_rejects_cross_attempt_raw_fact_replay(self) -> None:
        facts = self._kilo_facts()
        arguments = self._kilo_argv(facts)
        self._replace_cli_value(arguments, "--attempt-id", "attempt-146-2")
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "kilo-compatible.json").exists())

    def test_kilo_binding_requires_exact_binding_on_each_request_fact(self) -> None:
        for role in ("ingress", "runtime_audit", "response_witness", "usage_audit"):
            with self.subTest(role=role):
                facts = self._kilo_facts()
                payload = json.loads(facts[role].read_text(encoding="utf-8"))
                payload["run_nonce"] = "f" * 64
                self._write_json(facts[role].name, payload)
                self.assertEqual(self._run(self._kilo_argv(facts)), 2)
                self.assertFalse((self.root / "kilo-compatible.json").exists())

    def test_kilo_binding_rejects_request_fact_outside_attempt_window(self) -> None:
        cases = (
            ("ingress", "received_at_utc", "2026-07-30T23:59:59Z"),
            ("usage_audit", "recorded_at_utc", "2026-07-31T00:00:11Z"),
        )
        for role, field, observed_at in cases:
            with self.subTest(role=role):
                facts = self._kilo_facts()
                payload = json.loads(facts[role].read_text(encoding="utf-8"))
                payload[field] = observed_at
                self._write_json(facts[role].name, payload)
                self.assertEqual(self._run(self._kilo_argv(facts)), 2)
                self.assertFalse((self.root / "kilo-compatible.json").exists())

    def test_kilo_binding_rejects_inverted_attempt_window(self) -> None:
        arguments = self._kilo_argv()
        self._replace_cli_value(
            arguments,
            "--attempt-started-at-utc",
            "2026-07-31T00:00:11Z",
        )
        self.assertEqual(self._run(arguments), 2)
        self.assertFalse((self.root / "kilo-compatible.json").exists())

    def test_replay_kilo_binding_rejects_handwritten_receipt(self) -> None:
        self.assertEqual(self._run(self._kilo_argv()), 0)
        output = self.root / "kilo-compatible.json"
        valid_bytes = output.read_bytes()
        replayed = finalizer.replay_receipt(
            output,
            self.root,
            expected_subcommand="kilo-binding",
        )
        self.assertEqual(replayed["status"], "success")
        self.assertEqual(output.read_bytes(), valid_bytes)

        handwritten = {
            "schema_version": finalizer.CLIENT_BINDING_SCHEMA,
            "status": "success",
        }
        self._write_json(output.name, handwritten)
        handwritten_bytes = output.read_bytes()
        with self.assertRaises(finalizer.ReceiptFinalizerError):
            finalizer.replay_receipt(output, self.root, "kilo-binding")
        self.assertEqual(output.read_bytes(), handwritten_bytes)

    def test_replay_kilo_binding_rejects_rebound_input_sha(self) -> None:
        self.assertEqual(self._run(self._kilo_argv()), 0)
        output = self.root / "kilo-compatible.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
        payload["producer"]["input_bindings"][0]["sha256"] = "0" * 64
        core = {
            key: value
            for key, value in payload["producer"].items()
            if key != "command_sha256"
        }
        payload["producer"]["command_sha256"] = hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._write_json(output.name, payload)
        with self.assertRaises(finalizer.ReceiptFinalizerError):
            finalizer.replay_receipt(output, self.root, "kilo-binding")

    def test_kilo_responses_protocol_is_derived_from_approved_client_id(self) -> None:
        facts = self._kilo_facts()
        ingress = json.loads(facts["ingress"].read_text(encoding="utf-8"))
        ingress.update(
            {
                "client_id": "kilo-responses",
                "protocol": "openai-responses",
                "entrypoint": "/v1/responses",
            }
        )
        self._write_json("kilo-ingress.json", ingress)
        runtime = json.loads(facts["runtime_audit"].read_text(encoding="utf-8"))
        runtime.update(
            {
                "client_id": "kilo-responses",
                "protocol": "openai-responses",
                "entrypoint": "/v1/responses",
                "transport": "websocket",
                "affected_branches": ["websocket"],
            }
        )
        self._write_json("request-runtime.json", runtime)
        response = json.loads(
            facts["response_witness"].read_text(encoding="utf-8")
        )
        response["client_id"] = "kilo-responses"
        self._write_json("kilo-response.json", response)
        arguments = self._kilo_argv(
            facts,
            output="kilo-responses.json",
            client_id="kilo-responses",
        )
        self.assertEqual(self._run(arguments), 0)
        payload = json.loads(
            (self.root / "kilo-responses.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["protocol"], "openai-responses")
        self.assertEqual(payload["entrypoint"], "/v1/responses")

    def test_kilo_binding_rejects_wrong_correlation(self) -> None:
        facts = self._kilo_facts()
        usage = json.loads(facts["usage_audit"].read_text(encoding="utf-8"))
        usage["response_id"] = "response-other"
        self._write_json("usage-audit.json", usage)
        self.assertEqual(self._run(self._kilo_argv(facts)), 2)
        self.assertFalse((self.root / "kilo-compatible.json").exists())

    def test_kilo_binding_rejects_missing_usage(self) -> None:
        facts = self._kilo_facts()
        facts["usage_audit"].unlink()
        self.assertEqual(self._run(self._kilo_argv(facts)), 2)
        self.assertFalse((self.root / "kilo-compatible.json").exists())

    def test_kilo_binding_rejects_non_kilo_installation(self) -> None:
        facts = self._kilo_facts()
        installation = json.loads(
            facts["installation"].read_text(encoding="utf-8")
        )
        installation["product_id"] = "other-client"
        self._write_json("kilo-installation.json", installation)
        self.assertEqual(self._run(self._kilo_argv(facts)), 2)
        self.assertFalse((self.root / "kilo-compatible.json").exists())

    def test_kilo_binding_rejects_input_success_claim(self) -> None:
        facts = self._kilo_facts()
        response = json.loads(facts["response_witness"].read_text(encoding="utf-8"))
        response["status"] = "success"
        self._write_json("kilo-response.json", response)
        self.assertEqual(self._run(self._kilo_argv(facts)), 2)
        self.assertFalse((self.root / "kilo-compatible.json").exists())

    def test_kilo_binding_rejects_symlink_and_invalid_sha_inputs(self) -> None:
        facts = self._kilo_facts()
        usage_link = self.root / "usage-link.json"
        usage_link.symlink_to(facts["usage_audit"].name)
        linked_facts = {**facts, "usage_audit": usage_link}
        self.assertEqual(self._run(self._kilo_argv(linked_facts)), 2)

        installation = json.loads(
            facts["installation"].read_text(encoding="utf-8")
        )
        installation["executable_sha256"] = "NOT-A-SHA"
        self._write_json("kilo-installation.json", installation)
        self.assertEqual(self._run(self._kilo_argv(facts)), 2)
        self.assertFalse((self.root / "kilo-compatible.json").exists())


if __name__ == "__main__":
    unittest.main()
