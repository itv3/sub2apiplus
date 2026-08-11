"""Codex CLI 升级编排器的离线门禁。"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import candidate_evidence_guard
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_receipt_finalizer
from tools.official_client_capture.codex_upgrade import (
    build_coverage,
    compare_inventory,
    compare_surfaces,
    load_rule_manifest,
    scan_evidence,
    scan_source_tree,
)
from tools.official_client_capture.codex_upgrade import Job


class CodexUpgradeTest(unittest.TestCase):
    def test_0145_to_0147_main_model_default_is_gpt_5_4(self) -> None:
        parser = codex_upgrade._build_parser()
        plan_parser = next(
            action.choices["plan"]
            for action in parser._actions
            if getattr(action, "choices", None) and "plan" in action.choices
        )
        model_action = next(
            action for action in plan_parser._actions if action.dest == "model"
        )
        self.assertEqual(model_action.default, "gpt-5.4")

    def test_checked_in_baseline_scenario_bindings_match_sources(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        repo_root = tool_root.parents[1]
        scenario_path = tool_root / "codex_upgrade_scenarios_0_145_0.json"
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))

        self.assertEqual(scenario["codex_version"], "0.145.0")

        source_spec = scenario["source_spec"]
        source_spec_path = repo_root / source_spec["path"]
        self.assertEqual(
            source_spec["sha256"],
            codex_upgrade.source_spec_section_sha256(
                source_spec_path,
                source_spec["fragment"],
            ),
        )

        rule_binding = scenario["rule_manifest"]
        rule_path = repo_root / rule_binding["path"]
        self.assertEqual(
            rule_binding["sha256"],
            codex_upgrade.file_sha256(rule_path),
        )
        rule_manifest = json.loads(rule_path.read_text(encoding="utf-8"))
        self.assertEqual(
            rule_binding["rule_count"],
            len(rule_manifest["required_rules"]),
        )

        candidate_jobs = [
            job for job in scenario["capture_jobs"] if job["phase"] == "candidate"
        ]
        self.assertTrue(candidate_jobs)
        for job in candidate_jobs:
            for step in job["steps"]:
                self.assertEqual(
                    step["environment"].get("CODEX_VERSION"),
                    "{target_version}",
                )

        evidence_owners: dict[tuple[str, str], str] = {}
        for job in scenario["capture_jobs"]:
            for root in job["evidence_roots"]:
                owner_key = (job["phase"], root)
                self.assertNotIn(owner_key, evidence_owners)
                evidence_owners[owner_key] = job["id"]

        serialized_scenario = json.dumps(scenario, ensure_ascii=False)
        self.assertNotIn(
            "/capture/tools/official_client_capture",
            serialized_scenario,
        )
        wham_job = next(
            job for job in scenario["capture_jobs"] if job["id"] == "official-wham-safe"
        )
        wham_command = wham_job["steps"][1]["argv"][2]
        self.assertIn("basicConstraints=critical,CA:TRUE", wham_command)
        self.assertIn("basicConstraints=critical,CA:FALSE", wham_command)
        self.assertIn(
            "SSL_CERT_FILE=/capture/runtime/{campaign_id}-official-wham-safe/ca.crt",
            wham_command,
        )
        self.assertNotIn(
            "SSL_CERT_FILE=/capture/runtime/{campaign_id}-official-wham-safe/server.crt",
            wham_command,
        )

        mutated = json.loads(json.dumps(scenario))
        candidate = next(
            job for job in mutated["capture_jobs"] if job["phase"] == "candidate"
        )
        candidate["steps"][0]["environment"]["CODEX_VERSION"] = "0.146.0"
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "Campaign target_version",
        ):
            codex_upgrade._validate_scenario_manifest_shape(mutated)

    def test_campaign_capture_scripts_bind_frozen_tool_root(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        scripts = (
            "run_official_codex_compact_capture.sh",
            "run_official_http_fallback_baseline.sh",
            "run_official_relay_scenario.sh",
            "run_sub2api_direct_matrix.sh",
            "run_sub2api_openai_mitm_matrix.sh",
            "run_h1_wire_probe.sh",
            "run_images_wire_probe.sh",
        )
        for name in scripts:
            content = (tool_root / name).read_text(encoding="utf-8")
            self.assertIn("capture_tool_root=", content, name)
            self.assertNotIn(
                "/capture/tools/official_client_capture",
                content,
                name,
            )

    def test_job_validation_rejects_shared_phase_evidence_root(self) -> None:
        jobs = [
            Job(
                job_id=job_id,
                phase="official",
                suites=("full",),
                description=job_id,
                steps=({"argv": ["true"], "environment": {}, "timeout": 60},),
                evidence_roots=("/tmp/shared-evidence",),
                covers=(),
            )
            for job_id in ("official-one", "official-two")
        ]
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "证据根必须由单一任务独占",
        ):
            codex_upgrade._validate_jobs(jobs, ())

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_state_snapshot(path: Path, state: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(candidate_evidence_guard.normalize_state(state))
        path.chmod(0o600)

    @staticmethod
    def _binding(path: Path, logical_path: str) -> dict[str, str]:
        return {
            "path": logical_path,
            "sha256": codex_upgrade.file_sha256(path),
        }

    @staticmethod
    def _make_private_tree(root: Path) -> None:
        for path in sorted(root.rglob("*")):
            path.chmod(0o700 if path.is_dir() else 0o600)
        root.chmod(0o700)

    @staticmethod
    def _database_state(*, after: bool) -> dict[str, object]:
        protected_tables = []
        for name, columns in sorted(
            codex_upgrade_receipt_finalizer.DATABASE_PROTECTED_TABLES.items()
        ):
            primary_key = hashlib.sha256(f"{name}:1".encode()).hexdigest()
            protected_tables.append(
                {
                    "exists": True,
                    "name": name,
                    "primary_key_columns": list(columns),
                    "primary_key_fingerprints": [primary_key],
                    "row_count": 1,
                }
            )
        append_only_watermarks = [
            {
                "exists": True,
                "max_id": 2 if after else 1,
                "name": name,
                "row_count": 2 if after else 1,
            }
            for name in sorted(
                codex_upgrade_receipt_finalizer.DATABASE_WATERMARK_TABLES
            )
        ]
        return {
            "append_only_watermarks": append_only_watermarks,
            "comparison_policy": (
                codex_upgrade_receipt_finalizer.DATABASE_COMPARISON_POLICY
            ),
            "probe_kind": "database",
            "protected_tables": protected_tables,
        }

    def test_attempt_reservation_is_created_below_phase_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir = Path(temporary) / "campaign"
            campaign_dir.mkdir(mode=0o700)
            self._write_json(campaign_dir / "campaign.json", {})
            manifest = {"campaign_id": "reservation-test"}
            job = Job(
                job_id="candidate-test",
                phase="candidate",
                suites=("full",),
                description="原子预约测试",
                steps=(),
                evidence_roots=(str(Path(temporary) / "evidence"),),
                covers=(),
            )

            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value=manifest,
            ):
                attempt_dir, reservation = codex_upgrade._reserve_capture_attempt(
                    campaign_dir,
                    phase="candidate",
                    candidate_id="candidate-a",
                    identity={"profile_id": "profile-a"},
                    jobs=[job],
                )

            self.assertTrue(attempt_dir.is_dir())
            self.assertTrue(
                attempt_dir.is_relative_to(
                    campaign_dir / "candidates" / "candidate-a" / "attempts"
                )
            )
            self.assertEqual(attempt_dir.stat().st_mode & 0o777, 0o700)
            self.assertTrue((attempt_dir / "reservation.json").is_file())
            self.assertRegex(reservation["run_nonce"], r"^[0-9a-f]{64}$")

    def _write_scenario_manifest(
        self,
        root: Path,
        rule_manifest: Path,
        rules: tuple[str, ...],
        *,
        version: str,
        name: str,
    ) -> Path:
        spec_path = Path(__file__).resolve().parents[3] / "docs" / (
            "CODEX_CLI_0145_EGRESS_SPEC.md"
        )
        scenario_manifest = root / name
        self._write_json(
            scenario_manifest,
            {
                "schema_version": codex_upgrade.SCENARIO_SCHEMA,
                "codex_version": version,
                "profile_id": (
                    "codex-0.146.0-test-v1"
                    if version == "0.146.0"
                    else "codex-0.145.0-upgrade-v1"
                ),
                "source_spec": {
                    "path": "docs/CODEX_CLI_0145_EGRESS_SPEC.md",
                    "fragment": "第二章",
                    "sha256": codex_upgrade.source_spec_section_sha256(
                        spec_path, "第二章"
                    ),
                },
                "rule_manifest": {
                    "path": str(
                        rule_manifest.resolve().relative_to(
                            Path(__file__).resolve().parents[3]
                        )
                    )
                    if rule_manifest.resolve().is_relative_to(
                        Path(__file__).resolve().parents[3]
                    )
                    else rule_manifest.name,
                    "sha256": codex_upgrade.file_sha256(rule_manifest),
                    "rule_count": len(rules),
                },
                "required_client_bindings": [
                    "kilo-compatible",
                    "kilo-responses",
                ],
                "variable_contract": [
                    {
                        "name": "campaign_dir",
                        "type": "absolute_path",
                        "required": True,
                        "sensitive": False,
                        "description": "测试 Campaign 根目录。",
                    },
                    {
                        "name": "target_version",
                        "type": "string",
                        "required": True,
                        "sensitive": False,
                        "description": "测试 Campaign 目标版本。",
                    }
                ],
                "evidence_scenarios": [
                    {
                        "scenario_id": "A01",
                        "description": "测试规则全集的双侧证据场景",
                        "trigger": "对官方 CLI 与候选服务执行同一组离线夹具",
                        "preconditions": ["测试证据根已创建"],
                        "required_artifact_kinds": ["process_trace"],
                        "covers": list(rules),
                    }
                ],
                "capture_jobs": [
                    {
                        "id": "official-test",
                        "phase": "official",
                        "suites": ["full"],
                        "description": "测试官方抓包阶段",
                        "required": True,
                        "steps": [
                            {
                                "argv": ["true"],
                                "environment": {},
                                "timeout_seconds": 60,
                            }
                        ],
                        "evidence_roots": [
                            "{campaign_dir}/official-evidence"
                        ],
                        "covers": list(rules),
                        "scenario_ids": ["A01"],
                        "required_scenario_receipts": [],
                    },
                    {
                        "id": "candidate-test",
                        "phase": "candidate",
                        "suites": ["full"],
                        "description": "测试候选抓包阶段",
                        "required": True,
                        "steps": [
                            {
                                "argv": ["true"],
                                "environment": {
                                    "CODEX_VERSION": "{target_version}",
                                },
                                "timeout_seconds": 60,
                            }
                        ],
                        "evidence_roots": [
                            "{campaign_dir}/candidate-evidence"
                        ],
                        "covers": list(rules),
                        "scenario_ids": ["A01"],
                        "required_scenario_receipts": [],
                    },
                ],
            },
        )
        return scenario_manifest

    def _campaign_arguments(
        self, root: Path, *, campaign_id: str = "upgrade-0146-test"
    ) -> argparse.Namespace:
        baseline_source = root / "baseline-source"
        target_source = root / "target-source"
        baseline_evidence = root / "baseline-evidence"
        for directory in (
            baseline_source,
            target_source,
            baseline_evidence,
        ):
            directory.mkdir(parents=True)
        for source in (baseline_source, target_source):
            (source / "Cargo.lock").write_text(
                '[[package]]\nname = "reqwest"\nversion = "0.12.28"\n',
                encoding="utf-8",
            )
        self._write_json(
            baseline_evidence / "surface.json",
            {
                "records": [
                    {
                        "request": {
                            "method": "POST",
                            "path": "/backend-api/codex/responses",
                            "http_version": "HTTP/1.1",
                            "headers": [
                                ["version", "0.145.0"],
                                ["host", "chatgpt.com"],
                            ],
                            "json_shape": {"model": "<string>", "input": []},
                        }
                    }
                ]
            },
        )
        baseline_rule_manifest = (
            Path(__file__).resolve().parents[1]
            / "codex_upgrade_rules_0_145_0.json"
        )
        required_rules = list(
            load_rule_manifest(baseline_rule_manifest, "0.145.0")
        )
        scenario_manifest = self._write_scenario_manifest(
            root,
            baseline_rule_manifest,
            tuple(required_rules),
            version="0.145.0",
            name="scenarios.json",
        )
        package_path = root / "codex-package-x86_64-unknown-linux-musl.tar.gz"
        binary_bytes = b"codex-cli-test-binary"
        code_mode_host_bytes = b"codex-code-mode-host-test-binary"
        package_metadata = json.dumps(
            {
                "layoutVersion": 1,
                "version": "0.146.0",
                "target": "x86_64-unknown-linux-musl",
                "variant": "codex",
                "entrypoint": "bin/codex",
                "resourcesDir": "codex-resources",
                "pathDir": "codex-path",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        with tarfile.open(package_path, mode="w:gz") as archive:
            for name, content, mode in (
                ("codex-package.json", package_metadata, 0o644),
                ("bin/codex", binary_bytes, 0o755),
                ("bin/codex-code-mode-host", code_mode_host_bytes, 0o755),
            ):
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = mode
                member.mtime = 0
                archive.addfile(member, io.BytesIO(content))
        return argparse.Namespace(
            command="plan",
            campaign_dir=root / "campaign",
            output=None,
            dry_run=False,
            execute=False,
            acknowledge_live_requests=False,
            baseline_version="0.145.0",
            target_version="0.146.0",
            baseline_source=baseline_source,
            target_source=target_source,
            baseline_evidence=baseline_evidence,
            target_sha256=hashlib.sha256(binary_bytes).hexdigest(),
            target_package=package_path,
            target_package_sha256=codex_upgrade.file_sha256(package_path),
            target_code_mode_host_sha256=hashlib.sha256(
                code_mode_host_bytes
            ).hexdigest(),
            runtime_image=f"capture-runtime@sha256:{'b' * 64}",
            rule_manifest=baseline_rule_manifest,
            scenario_manifest=scenario_manifest,
            extra_jobs=None,
            suite="full",
            campaign_id=campaign_id,
            model="gpt-5.6-luna",
            capture_root=Path("/root/oauth-capture"),
            capture_container="capture-cli",
            service_container="sub2apiplus",
            keeper_container="sub2apiplus-keeper",
            postgres_container="sub2apiplus-postgres",
            redis_container="sub2apiplus-redis",
            capture_codex_bin="/usr/local/bin/codex-capture",
            relay_codex_bin="/root/.local/bin/codex",
            capture_code_mode_host_bin="/usr/local/bin/codex-code-mode-host",
            relay_code_mode_host_bin="/root/.local/bin/codex-code-mode-host",
            codex_account_id=90,
            api_key_id=1,
            candidate_id=None,
            profile_id=None,
            profile_digest=None,
            target_rule_manifest=None,
            migration_manifest=None,
            assertion_profile_manifest=None,
            approve_manifest_sha256=None,
            assertions=None,
        )

    def _create_campaign(self, root: Path) -> tuple[Path, dict[str, object]]:
        arguments = self._campaign_arguments(root)
        codex_upgrade.create_campaign(arguments)
        campaign_dir = arguments.campaign_dir
        manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
        return campaign_dir, manifest

    def _seal_official_stage(
        self,
        root: Path,
        campaign_dir: Path,
        manifest: dict[str, object],
        *,
        include_new_surface: bool = False,
    ) -> Path:
        evidence_root = root / "official-evidence"
        self._write_capture_stage(
            campaign_dir,
            evidence_root,
            phase="official",
            identity=manifest["official_identity"],
            include_new_surface=include_new_surface,
        )
        return evidence_root

    def _write_capture_stage(
        self,
        campaign_dir: Path,
        evidence_root: Path,
        *,
        phase: str,
        identity: dict[str, object],
        candidate_id: str | None = None,
        include_new_surface: bool = False,
        restoration_passed: bool = True,
    ) -> None:
        evidence_root.mkdir(parents=True, exist_ok=True)
        campaign_manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
        attempt_id = (
            "20260731T000000Z-1111111111111111"
            if phase == "official"
            else "20260731T000000Z-2222222222222222"
        )
        run_nonce = ("1" if phase == "official" else "2") * 64
        window_base = datetime.now(timezone.utc) - timedelta(minutes=1)

        def timestamp(offset_seconds: int) -> str:
            return (
                (window_base + timedelta(seconds=offset_seconds))
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )

        attempt_started_at_utc = timestamp(0)
        client_checkpoint_at_utc = timestamp(360)
        prefix = evidence_root.name
        records = [
            {
                "request": {
                    "method": "POST",
                    "path": "/backend-api/codex/responses",
                    "http_version": "HTTP/1.1",
                    "headers": [
                        ["version", "0.146.0"],
                        ["host", "chatgpt.com"],
                    ],
                    "json_shape": {"model": "<string>", "input": []},
                }
            }
        ]
        if include_new_surface:
            records.append(
                {
                    "request": {
                        "method": "GET",
                        "path": "/backend-api/codex/new-egress?token=redacted",
                        "http_version": "HTTP/1.1",
                        "headers": [["host", "chatgpt.com"]],
                    }
                }
            )
        self._write_json(
            evidence_root / "surface.json",
            {"records": records},
        )

        restoration_report = evidence_root / "restoration-report.json"
        restoration_arguments: dict[str, object] = {
            "evidence_root": evidence_root,
            "output": Path(restoration_report.name),
            "phase": phase,
            "candidate_id": candidate_id,
        }
        for check_id, before_name, after_name, comparator in (
            codex_upgrade_receipt_finalizer.RESTORATION_INPUTS
        ):
            before_path = evidence_root / f"{before_name}.json"
            after_path = evidence_root / f"{after_name}.json"
            if comparator == "before_subset":
                before_state = self._database_state(after=False)
                after_state = self._database_state(after=True)
            else:
                before_state = {
                    "probe_kind": check_id,
                    "stable_value": "restored",
                }
                after_state = dict(before_state)
            self._write_state_snapshot(before_path, before_state)
            self._write_state_snapshot(after_path, after_state)
            restoration_arguments[before_name] = Path(before_path.name)
            restoration_arguments[after_name] = Path(after_path.name)
        self._make_private_tree(evidence_root)
        codex_upgrade_receipt_finalizer.finalize_restoration(
            argparse.Namespace(**restoration_arguments)
        )

        capture_manifest = evidence_root / "capture-manifest.json"
        self._write_json(
            capture_manifest,
            {
                "schema_version": (
                    "codex-candidate-capture-manifest/v1"
                ),
                "codex_version": "0.146.0",
                "capture_id": f"{phase}-{candidate_id or 'official'}",
                "status": "complete",
                "artifacts": [
                    {
                        "path": "surface.json",
                        "sha256": codex_upgrade.file_sha256(
                            evidence_root / "surface.json"
                        ),
                        "kind": "process_trace",
                        "parser": "observation_json",
                        "scenario_ids": ["A01"],
                        "labels": {"side": phase},
                    }
                ],
            },
        )

        client_bindings: list[dict[str, object]] = []
        observed_profile: dict[str, str] | None = None
        client_receipts: list[str] = []
        post_client_restoration_report: Path | None = None
        if phase == "candidate":
            self.assertIsNotNone(candidate_id)
            observed_runtime_path = (
                evidence_root / "observed-profile-runtime-audit.json"
            )
            self._write_json(
                observed_runtime_path,
                {
                    "schema_version": (
                        codex_upgrade_receipt_finalizer.RUNTIME_AUDIT_SCHEMA
                    ),
                    "source": "sub2api-runtime",
                    "event_type": "profile_activated",
                    "event_id": "profile-event-1",
                    "campaign_id": campaign_manifest["campaign_id"],
                    "attempt_id": attempt_id,
                    "run_nonce": run_nonce,
                    "candidate_id": candidate_id,
                    "target_version": "0.146.0",
                    "profile_id": identity["profile_id"],
                    "profile_digest": identity["profile_digest"],
                    "image_id": identity["image_id"],
                    "image_reference": identity["image_reference"],
                    "source_tree_sha256": identity["source_tree_sha256"],
                    "build_id": identity["build_id"],
                    "deployed_version": identity["deployed_version"],
                    "observed_at_utc": timestamp(10),
                },
            )
            observed_profile_path = evidence_root / "observed-profile.json"
            kilo_arguments: list[argparse.Namespace] = []
            clients = (
                (
                    "kilo-compatible",
                    "openai-compatible",
                    "/v1/chat/completions",
                ),
                (
                    "kilo-responses",
                    "openai-responses",
                    "/v1/responses",
                ),
            )
            for client, protocol, entrypoint in clients:
                installation_id = f"installation-{client}"
                request_id = f"request-{client}"
                response_id = f"response-{client}"
                ingress_witness_id = f"ingress-{client}"
                transport = (
                    "http" if client == "kilo-compatible" else "websocket"
                )
                installation_path = (
                    evidence_root / f"{client}-installation.json"
                )
                ingress_path = evidence_root / f"{client}-ingress.json"
                runtime_path = evidence_root / f"{client}-runtime-audit.json"
                response_path = (
                    evidence_root / f"{client}-response-witness.json"
                )
                usage_path = evidence_root / f"{client}-usage-audit.json"
                self._write_json(
                    installation_path,
                    {
                        "schema_version": (
                            codex_upgrade_receipt_finalizer.KILO_INSTALLATION_SCHEMA
                        ),
                        "source": "kilo-installation",
                        "installation_id": installation_id,
                        "product_id": "kilo",
                        "display_name": "Kilo Code",
                        "client_version": "kilo-test-1",
                        "executable_path": (
                            "/Applications/Kilo Code.app/Contents/MacOS/Kilo"
                        ),
                        "executable_sha256": hashlib.sha256(
                            client.encode()
                        ).hexdigest(),
                        "observed_at_utc": timestamp(-3600),
                    },
                )
                self._write_json(
                    ingress_path,
                    {
                        "schema_version": (
                            codex_upgrade_receipt_finalizer.KILO_INGRESS_SCHEMA
                        ),
                        "source": "kilo-ingress",
                        "witness_id": ingress_witness_id,
                        "request_id": request_id,
                        "campaign_id": campaign_manifest["campaign_id"],
                        "attempt_id": attempt_id,
                        "run_nonce": run_nonce,
                        "installation_id": installation_id,
                        "client_id": client,
                        "client_version": "kilo-test-1",
                        "protocol": protocol,
                        "entrypoint": entrypoint,
                        "model": "gpt-5.6-luna",
                        "candidate_id": candidate_id,
                        "target_version": "0.146.0",
                        "received_at_utc": timestamp(20),
                    },
                )
                self._write_json(
                    runtime_path,
                    {
                        "schema_version": (
                            codex_upgrade_receipt_finalizer.RUNTIME_AUDIT_SCHEMA
                        ),
                        "source": "sub2api-runtime",
                        "event_type": "oauth_request_forwarded",
                        "event_id": f"runtime-{client}",
                        "request_id": request_id,
                        "campaign_id": campaign_manifest["campaign_id"],
                        "attempt_id": attempt_id,
                        "run_nonce": run_nonce,
                        "ingress_witness_id": ingress_witness_id,
                        "installation_id": installation_id,
                        "client_id": client,
                        "protocol": protocol,
                        "entrypoint": entrypoint,
                        "model": "gpt-5.6-luna",
                        "candidate_id": candidate_id,
                        "target_version": "0.146.0",
                        "profile_id": identity["profile_id"],
                        "profile_digest": identity["profile_digest"],
                        "image_id": identity["image_id"],
                        "source_tree_sha256": identity["source_tree_sha256"],
                        "build_id": identity["build_id"],
                        "deployed_version": identity["deployed_version"],
                        "auth_mode": "oauth",
                        "oauth_account_id": 90,
                        "upstream_endpoint": "/backend-api/codex/responses",
                        "transport": transport,
                        "affected_branches": [transport],
                        "observed_at_utc": timestamp(30),
                    },
                )
                self._write_json(
                    response_path,
                    {
                        "schema_version": (
                            codex_upgrade_receipt_finalizer.KILO_RESPONSE_SCHEMA
                        ),
                        "source": "kilo-response",
                        "witness_id": f"response-witness-{client}",
                        "request_id": request_id,
                        "campaign_id": campaign_manifest["campaign_id"],
                        "attempt_id": attempt_id,
                        "run_nonce": run_nonce,
                        "installation_id": installation_id,
                        "client_id": client,
                        "candidate_id": candidate_id,
                        "http_status": 200,
                        "response_id": response_id,
                        "completed_at_utc": timestamp(40),
                    },
                )
                self._write_json(
                    usage_path,
                    {
                        "schema_version": (
                            codex_upgrade_receipt_finalizer.USAGE_AUDIT_SCHEMA
                        ),
                        "source": "sub2api-usage",
                        "event_id": f"usage-event-{client}",
                        "request_id": request_id,
                        "campaign_id": campaign_manifest["campaign_id"],
                        "attempt_id": attempt_id,
                        "run_nonce": run_nonce,
                        "response_id": response_id,
                        "candidate_id": candidate_id,
                        "usage_id": f"usage-{client}",
                        "oauth_account_id": 90,
                        "recorded_at_utc": timestamp(50),
                    },
                )
                receipt_path = evidence_root / f"{client}.json"
                kilo_arguments.append(
                    argparse.Namespace(
                        evidence_root=evidence_root,
                        output=Path(receipt_path.name),
                        campaign_id=campaign_manifest["campaign_id"],
                        attempt_id=attempt_id,
                        run_nonce=run_nonce,
                        attempt_started_at_utc=attempt_started_at_utc,
                        client_checkpoint_at_utc=client_checkpoint_at_utc,
                        client_id=client,
                        candidate_id=candidate_id,
                        target_version="0.146.0",
                        profile_id=identity["profile_id"],
                        profile_digest=identity["profile_digest"],
                        candidate_image_id=identity["image_id"],
                        source_tree_sha256=identity["source_tree_sha256"],
                        build_id=identity["build_id"],
                        deployed_version=identity["deployed_version"],
                        model="gpt-5.6-luna",
                        installation=Path(installation_path.name),
                        ingress=Path(ingress_path.name),
                        runtime_audit=Path(runtime_path.name),
                        response_witness=Path(response_path.name),
                        usage_audit=Path(usage_path.name),
                    )
                )
                client_receipts.append(f"{client}={receipt_path}")

            self._make_private_tree(evidence_root)
            codex_upgrade_receipt_finalizer.finalize_observed_profile(
                argparse.Namespace(
                    evidence_root=evidence_root,
                    output=Path(observed_profile_path.name),
                    campaign_id=campaign_manifest["campaign_id"],
                    attempt_id=attempt_id,
                    run_nonce=run_nonce,
                    attempt_started_at_utc=attempt_started_at_utc,
                    client_checkpoint_at_utc=client_checkpoint_at_utc,
                    candidate_id=candidate_id,
                    target_version="0.146.0",
                    profile_id=identity["profile_id"],
                    profile_digest=identity["profile_digest"],
                    image_id=identity["image_id"],
                    image_reference=identity["image_reference"],
                    source_tree_sha256=identity["source_tree_sha256"],
                    build_id=identity["build_id"],
                    deployed_version=identity["deployed_version"],
                    runtime_audit=Path(observed_runtime_path.name),
                )
            )
            for arguments in kilo_arguments:
                codex_upgrade_receipt_finalizer.finalize_kilo_binding(arguments)
            post_client_restoration_report = (
                evidence_root / "client-restoration-report.json"
            )
            post_client_arguments: dict[str, object] = {
                "evidence_root": evidence_root,
                "output": Path(post_client_restoration_report.name),
                "phase": "candidate",
                "candidate_id": candidate_id,
            }
            for check_id, before_name, after_name, comparator in (
                codex_upgrade_receipt_finalizer.RESTORATION_INPUTS
            ):
                before_path = evidence_root / f"client-{before_name}.json"
                after_path = evidence_root / f"client-{after_name}.json"
                if comparator == "before_subset":
                    before_state = self._database_state(after=True)
                    after_state = self._database_state(after=True)
                else:
                    before_state = {
                        "probe_kind": f"post_client_{check_id}",
                        "stable_value": "restored",
                    }
                    after_state = dict(before_state)
                self._write_state_snapshot(before_path, before_state)
                self._write_state_snapshot(after_path, after_state)
                post_client_arguments[before_name] = Path(before_path.name)
                post_client_arguments[after_name] = Path(after_path.name)
            codex_upgrade_receipt_finalizer.finalize_restoration(
                argparse.Namespace(**post_client_arguments)
            )
            self._write_json(
                evidence_root
                / "environment"
                / "client-after"
                / "probe-manifest.json",
                {
                    "schema_version": "codex-upgrade-environment-probe/v1",
                    "phase": "after",
                    "observed_at_utc": client_checkpoint_at_utc,
                },
            )

        self._make_private_tree(evidence_root)
        restoration = codex_upgrade._validate_restoration_report(
            restoration_report,
            [evidence_root],
            phase=phase,
            candidate_id=candidate_id,
        )
        if phase == "candidate":
            self.assertIsNotNone(post_client_restoration_report)
            restoration["post_client"] = (
                codex_upgrade._validate_restoration_report(
                    post_client_restoration_report,
                    [evidence_root],
                    phase="candidate",
                    candidate_id=str(candidate_id),
                )
            )
            observed_profile, _ = (
                codex_upgrade._validate_observed_profile_receipt(
                    observed_profile_path,
                    [evidence_root],
                    campaign_id=str(campaign_manifest["campaign_id"]),
                    attempt_id=attempt_id,
                    run_nonce=run_nonce,
                    attempt_started_at_utc=attempt_started_at_utc,
                    client_checkpoint_at_utc=client_checkpoint_at_utc,
                    candidate_id=str(candidate_id),
                    target_version="0.146.0",
                    expected_profile_id=str(identity["profile_id"]),
                    expected_profile_digest=str(identity["profile_digest"]),
                    image_id=str(identity["image_id"]),
                    image_reference=str(identity["image_reference"]),
                    source_tree_sha256=str(identity["source_tree_sha256"]),
                    build_id=str(identity["build_id"]),
                    deployed_version=str(identity["deployed_version"]),
                )
            )
            client_bindings = codex_upgrade._parse_client_evidence(
                client_receipts,
                [evidence_root],
                campaign_id=str(campaign_manifest["campaign_id"]),
                attempt_id=attempt_id,
                run_nonce=run_nonce,
                attempt_started_at_utc=attempt_started_at_utc,
                client_checkpoint_at_utc=client_checkpoint_at_utc,
                candidate_id=str(candidate_id),
                target_version="0.146.0",
                model="gpt-5.6-luna",
                identity=identity,
            )
        normalized_surface = codex_upgrade.scan_evidence(
            [evidence_root], f"target-{phase}"
        )
        stage_relative = (
            Path("official")
            if phase == "official"
            else Path("candidates") / str(candidate_id)
        )
        surface_path = campaign_dir / stage_relative / "surface.json"
        self._write_json(surface_path, normalized_surface)
        capture_binding = self._binding(
            capture_manifest, f"{prefix}/{capture_manifest.name}"
        )
        try:
            planned_job = next(
                job
                for job in codex_upgrade._campaign_jobs(
                    campaign_dir,
                    campaign_manifest,
                    phase,
                    candidate_id=candidate_id,
                    runtime_image=str(identity.get("image_reference", "")),
                    profile_id=str(identity.get("profile_id", "")),
                    profile_digest=str(identity.get("profile_digest", "")),
                )
                if job.job_id == f"{phase}-test"
            )
            execution_sha256 = codex_upgrade._job_execution_sha256(planned_job)
        except codex_upgrade.ConfigurationError:
            execution_sha256 = "0" * 64
        result_item = {
            "id": f"{phase}-test",
            "phase": phase,
            "required": True,
            "execution_sha256": execution_sha256,
            "status": "complete" if restoration_passed else "failed",
            "description": "合成抓包阶段",
            "duration_seconds": 0.0,
            "steps": [],
            "evidence_roots": [str(evidence_root)],
            "missing_evidence_patterns": [],
            "empty_evidence_patterns": [],
            "covers": [],
            "scenario_ids": ["A01"],
            "scenario_receipts": [],
            "scenario_receipt_failures": [],
            "track": "main",
            "model_id": "gpt-5.6-luna",
            "expected_use_responses_lite": False,
            "required_model_receipt": False,
            "model_condition_receipt": None,
            "model_condition_receipt_failure": None,
        }
        binary_verification: dict[str, object] | None = None
        if phase == "official":
            package_identity = campaign_manifest["official_identity"]["package"]
            binary_verification = {
                "passed": True,
                "expected_version": "0.146.0",
                "expected_sha256": campaign_manifest["target_sha256"],
                "runtime_image_reference": f"capture-runtime@sha256:{'b' * 64}",
                "runtime_image_id": f"sha256:{'c' * 64}",
                "identities": [
                    {
                        "label": label,
                        "path": path,
                        "version": "0.146.0",
                        "version_output": "codex-cli 0.146.0",
                        "sha256": campaign_manifest["target_sha256"],
                    }
                    for label, path in (
                        ("container:capture_codex_bin", "/usr/local/bin/codex-capture"),
                        ("container:relay_codex_bin", "/root/.local/bin/codex"),
                        ("host:relay_codex_bin", "/root/.local/bin/codex"),
                    )
                ],
                "package": package_identity,
                "helpers": [
                    {
                        "label": label,
                        "path": path,
                        "sha256": package_identity["code_mode_host_sha256"],
                    }
                    for label, path in (
                        (
                            "container:capture_code_mode_host_bin",
                            "/usr/local/bin/codex-code-mode-host",
                        ),
                        (
                            "container:relay_code_mode_host_bin",
                            "/root/.local/bin/codex-code-mode-host",
                        ),
                        (
                            "host:relay_code_mode_host_bin",
                            "/root/.local/bin/codex-code-mode-host",
                        ),
                    )
                ],
            }
        attempt_root = (
            campaign_dir / stage_relative / "attempts" / attempt_id
        )
        attempt_root.mkdir(parents=True, mode=0o700)
        reservation: dict[str, object] = {
            "schema_version": codex_upgrade.CAPTURE_RESERVATION_SCHEMA,
            "campaign_id": campaign_manifest["campaign_id"],
            "campaign_manifest_sha256": codex_upgrade.file_sha256(
                campaign_dir / "campaign.json"
            ),
            "phase": phase,
            "candidate_id": candidate_id,
            "attempt_id": attempt_id,
            "run_nonce": run_nonce,
            "started_at_utc": attempt_started_at_utc,
            "identity_sha256": codex_upgrade._fingerprint(identity),
            "planned_jobs": [
                {
                    "id": result_item["id"],
                    "required": True,
                    "execution_sha256": execution_sha256,
                }
            ],
        }
        reservation["reservation_digest"] = codex_upgrade._fingerprint(reservation)
        codex_upgrade._secure_write_json_once(
            attempt_root / "reservation.json", reservation
        )
        attempt = codex_upgrade._write_capture_attempt(
            campaign_dir,
            attempt_root,
            {
                "campaign_id": campaign_manifest["campaign_id"],
                "phase": phase,
                "candidate_id": candidate_id,
                "status": "awaiting_receipts",
                "identity": identity,
                "results": [result_item],
                "evidence_roots": [str(evidence_root)],
                "environment": {
                    "evidence_root": str(evidence_root),
                    "before_probe": None,
                    "after_probe": None,
                    "restoration_report": None,
                },
                "binary_verification": binary_verification,
                "execution_error": None,
                "restoration_error": None,
                "next_gate": "生成机器收据后 seal",
            },
        )
        attempt_path = attempt_root / "attempt.json"
        payload: dict[str, object] = {
            "status": "complete" if restoration_passed else "failed",
            "attempt": self._binding(
                attempt_path, attempt_path.relative_to(campaign_dir).as_posix()
            ),
            "evidence_roots": [str(evidence_root)],
            "identity": identity,
            "results": [result_item],
            "surface": self._binding(
                surface_path, surface_path.relative_to(campaign_dir).as_posix()
            ),
            "client_bindings": client_bindings,
            "assertion_context": {
                "capture_manifest": capture_binding,
                "capture_manifest_path": str(capture_manifest.resolve()),
                "evidence_root": str(evidence_root.resolve()),
                "evidence_prefix": prefix,
            },
            "assertion_gate": {
                "side": "official" if phase == "official" else "candidate",
                "bundle_dir_name": "assertion-bundle",
                "bundle_provenance_sha256": "1" * 64,
                "bundle_entry_count": 1,
                "derived_provenance_sha256": None,
                "capture_manifest": {
                    "path": "capture-manifest.json",
                    "sha256": capture_binding["sha256"],
                },
                "acceptance_contract_sha256": "2" * 64,
                "artifact_count": 1,
                "observation_count": 1,
                "checked_rule_count": 1,
                "checked_check_count": 1,
            },
            "restoration": restoration,
            "security": {
                "raw_evidence_private": True,
                **codex_upgrade._evidence_security([evidence_root]),
            },
        }
        if observed_profile is not None:
            payload["observed_profile"] = observed_profile
        if phase == "official":
            payload["binary_verification"] = binary_verification
        payload["evidence_inventory"] = codex_upgrade._evidence_inventory(
            [evidence_root]
        )
        codex_upgrade._seal_preview(
            campaign_dir,
            attempt_root,
            phase=phase,
            candidate_id=candidate_id,
            attempt=attempt,
            stage_payload=payload,
            approve_sha256=None,
        )
        preview_path = attempt_root / "seal-preview.json"
        payload["seal_preview"] = self._binding(
            preview_path,
            preview_path.relative_to(campaign_dir).as_posix(),
        )
        codex_upgrade.save_stage_result(
            campaign_dir,
            "capture-official" if phase == "official" else "capture-candidate",
            payload,
            candidate_id=candidate_id,
        )

    def _write_classification_manifests(
        self,
        root: Path,
        *,
        omit_last: bool = False,
        blocked_rule: str | None = None,
    ) -> tuple[Path, Path, Path, Path, Path, tuple[str, ...]]:
        baseline_manifest = (
            Path(__file__).resolve().parents[1]
            / "codex_upgrade_rules_0_145_0.json"
        )
        rules = load_rule_manifest(baseline_manifest, "0.145.0")
        target_manifest = root / "target-rules.json"
        migration_manifest = root / "rule-migration.json"
        scenario_manifest = root / "target-scenarios.json"
        profile_manifest = root / "profile.json"
        assertion_profile_manifest = root / "assertion-profile.json"
        self._write_json(
            target_manifest,
            {
                "schema_version": codex_upgrade.RULE_SCHEMA,
                "codex_version": "0.146.0",
                "required_rules": list(rules),
            },
        )
        entries = []
        for rule in rules[:-1] if omit_last else rules:
            classification = "blocked" if rule == blocked_rule else "inherit"
            entries.append(
                {
                    "baseline_rule": rule,
                    "target_rule": rule,
                    "classification": classification,
                    "rationale": "测试迁移闭环",
                    "evidence_refs": (
                        [] if classification == "blocked" else ["official-diff.json"]
                    ),
                }
            )
        self._write_json(
            migration_manifest,
            {
                "schema_version": codex_upgrade.MIGRATION_SCHEMA,
                "baseline_version": "0.145.0",
                "target_version": "0.146.0",
                "status": "approved",
                "entries": entries,
                "discovery_classifications": [],
            },
        )
        self._write_scenario_manifest(
            root,
            target_manifest,
            rules,
            version="0.146.0",
            name=scenario_manifest.name,
        )
        profile_payload = {
            "transport": "codex-official-egress",
            "rule_count": len(rules),
        }
        self._write_json(
            profile_manifest,
            {
                "schema_version": codex_upgrade.PROFILE_SCHEMA,
                "codex_version": "0.146.0",
                "profile_id": "codex-0.146.0-test-v1",
                "profile_digest": "c" * 64,
                "profile_payload": profile_payload,
                "profile_payload_sha256": codex_upgrade._fingerprint(
                    profile_payload
                ),
                "status": "approved",
            },
        )
        self._write_assertion_profile(
            assertion_profile_manifest,
            rules,
            version="0.146.0",
        )
        return (
            target_manifest,
            migration_manifest,
            scenario_manifest,
            profile_manifest,
            assertion_profile_manifest,
            rules,
        )

    def _write_assertion_profile(
        self,
        path: Path,
        rules: tuple[str, ...],
        *,
        version: str,
    ) -> None:
        frozen_path = (
            Path(__file__).resolve().parents[1]
            / "candidate_rule_expectations_0_145_0.json"
        )
        payload = json.loads(frozen_path.read_text(encoding="utf-8"))
        payload = json.loads(
            json.dumps(payload, ensure_ascii=False).replace("0.145.0", version)
        )
        spec_path = Path(__file__).resolve().parents[3] / (
            "docs/CODEX_CLI_0145_EGRESS_SPEC.md"
        )
        payload["source_spec_sha256"] = (
            codex_upgrade.source_spec_section_sha256(spec_path, "第二章")
        )
        rows = {
            row["rule_id"]: row
            for row in payload["rules"]
        }
        template = json.loads(json.dumps(payload["rules"][0]))
        selected: list[dict[str, object]] = []
        for rule in rules:
            row = rows.get(rule)
            if row is None:
                row = json.loads(json.dumps(template))
                row["rule_id"] = rule
                row["description"] = "测试新增规则断言"
            selected.append(row)
        payload["rules"] = selected
        self._write_json(path, payload)

    def _classification_arguments(
        self,
        campaign_dir: Path,
        manifests: tuple[Path, Path, Path, Path, Path],
    ) -> list[str]:
        target, migration, scenario, profile, assertion_profile = manifests
        return [
            "classify",
            "--campaign-dir",
            str(campaign_dir),
            "--target-rule-manifest",
            str(target),
            "--migration-manifest",
            str(migration),
            "--scenario-manifest",
            str(scenario),
            "--profile-manifest",
            str(profile),
            "--assertion-profile-manifest",
            str(assertion_profile),
        ]

    def _approve_classification(
        self,
        campaign_dir: Path,
        manifests: tuple[Path, Path, Path, Path, Path],
    ) -> tuple[int, dict[str, object], str]:
        arguments = self._classification_arguments(campaign_dir, manifests)
        request_code, request_stdout, request_stderr = self._run_main(arguments)
        self.assertEqual(request_code, 2, request_stderr)
        request = json.loads(request_stdout)
        self.assertEqual(request["status"], "approval_required")
        return_code, stdout, stderr = self._run_main(
            [
                *arguments,
                "--approve-manifest-sha256",
                request["joint_manifest_sha256"],
            ]
        )
        return return_code, json.loads(stdout) if stdout else {}, stderr

    def _run_main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return_code = codex_upgrade.main(arguments)
        return return_code, stdout.getvalue(), stderr.getvalue()

    def _create_classified_campaign(
        self, root: Path
    ) -> tuple[Path, dict[str, object], tuple[str, ...]]:
        campaign_dir, manifest = self._create_campaign(root)
        self._seal_official_stage(root, campaign_dir, manifest)
        target, migration, scenario, profile, assertion_profile, rules = (
            self._write_classification_manifests(root)
        )
        return_code, _, stderr = self._approve_classification(
            campaign_dir,
            (target, migration, scenario, profile, assertion_profile),
        )
        self.assertEqual(return_code, 0, stderr)
        return campaign_dir, manifest, rules

    def _seal_candidate_stage(
        self,
        root: Path,
        campaign_dir: Path,
        *,
        candidate_id: str = "candidate-a",
        profile_digest: str | None = None,
        restoration_passed: bool = True,
    ) -> tuple[Path, dict[str, str]]:
        profile_digest = profile_digest or "c" * 64
        evidence_root = root / f"{candidate_id}-evidence"
        identity = {
            "git_commit": "f" * 40,
            "source_tree_sha256": "d" * 64,
            "image_reference": f"sub2apiplus@sha256:{'9' * 64}",
            "image_digest": f"sha256:{'9' * 64}",
            "image_id": f"sha256:{'e' * 64}",
            "build_id": "build-0146-test",
            "deployed_version": "0.1.999-test",
            "profile_id": "codex-0.146.0-test-v1",
            "profile_digest": profile_digest,
        }
        self._write_capture_stage(
            campaign_dir,
            evidence_root,
            phase="candidate",
            identity=identity,
            candidate_id=candidate_id,
            restoration_passed=restoration_passed,
        )
        return evidence_root, identity

    def _write_assertions(
        self,
        root: Path,
        rules: tuple[str, ...],
        identity: dict[str, str],
        *,
        candidate_id: str = "candidate-a",
        omit_last: bool = False,
        profile_digest: str | None = None,
        first_rule_status: str = "pass",
        first_rule_evidence_level: str = "full",
    ) -> Path:
        assertions_path = root / f"{candidate_id}-assertions.json"
        selected_rules = rules[:-1] if omit_last else rules
        campaign_dir = root / "campaign"
        manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
        official = codex_upgrade._load_stage_result(
            campaign_dir, "capture-official"
        )
        classification = codex_upgrade._load_stage_result(
            campaign_dir, "classify"
        )
        candidate = codex_upgrade._load_stage_result(
            campaign_dir, "capture-candidate", candidate_id
        )
        comparison = codex_upgrade._load_stage_result(
            campaign_dir, "compare", candidate_id
        )
        official_evidence = root / "official-evidence" / "surface.json"
        candidate_evidence = root / f"{candidate_id}-evidence" / "surface.json"
        official_relative = "official-evidence/surface.json"
        candidate_relative = f"{candidate_id}-evidence/surface.json"
        machine_root = campaign_dir / "assertions" / candidate_id / "machine"
        rows = []
        checker_sha256 = next(
            item["sha256"]
            for item in manifest["tool_identity"]["entries"]
            if item["path"] == "candidate_rule_assertion.py"
        )
        profile_reference = classification["assertion_profile_manifest"]
        rule_reference = classification["target_rule_manifest"]
        approved_profile = (
            campaign_dir / profile_reference["path"]
        ).resolve(strict=True)
        approved_rules = (
            campaign_dir / rule_reference["path"]
        ).resolve(strict=True)
        validation_modes = codex_upgrade._acceptance_validation_modes(
            campaign_dir, classification, tuple(rules)
        )
        official_authority = codex_upgrade._classification_official_authority(
            classification
        )
        for index, rule in enumerate(selected_rules):
            machine_bindings: dict[str, dict[str, str]] = {}
            commands: dict[str, list[str]] = {}
            expected_check_ids = codex_upgrade._acceptance_expected_check_ids(
                campaign_dir, classification, rule
            )
            sides = (
                (("official", official), ("candidate", candidate))
                if validation_modes[rule] == "dual_wire"
                else (("candidate", candidate),)
            )
            for side, stage in sides:
                machine_path = machine_root / side / f"{rule}.json"
                context = stage["assertion_context"]
                command = codex_upgrade.build_machine_assertion_command(
                    rule_id=rule,
                    capture_manifest=context["capture_manifest_path"],
                    evidence_root=context["evidence_root"],
                    profile=str(approved_profile),
                    rule_manifest=str(approved_rules),
                    expected_codex_version="0.146.0",
                    expected_profile_sha256=profile_reference["sha256"],
                    output=str(machine_path.resolve()),
                )
                machine_result = {
                    "schema_version": codex_upgrade.MACHINE_ASSERTION_SCHEMA,
                    "rule_id": rule,
                    "status": "pass",
                    "started_at": "2026-07-31T00:00:00Z",
                    "finished_at": "2026-07-31T00:00:01Z",
                    "exit_code": 0,
                    "checker_sha256": checker_sha256,
                    "command_sha256": (
                        codex_upgrade.machine_command_sha256(command)
                    ),
                    "checks": [
                        {
                            "id": check_id,
                            "description": f"{check_id} 判据",
                            "passed": True,
                            "expected": {"present": True},
                            "actual": {"present": True},
                            "evidence_paths": ["surface.json"],
                        }
                        for check_id in expected_check_ids
                    ],
                }
                self._write_json(machine_path, machine_result)
                machine_bindings[side] = self._binding(
                    machine_path,
                    machine_path.relative_to(campaign_dir).as_posix(),
                )
                commands[side] = command
            row: dict[str, object] = {
                "rule": rule,
                "validation_mode": validation_modes[rule],
                "status": first_rule_status if index == 0 else "pass",
                "candidate_evidence_refs": [
                    {
                        "path": candidate_relative,
                        "sha256": hashlib.sha256(
                            candidate_evidence.read_bytes()
                        ).hexdigest(),
                    }
                ],
                "candidate_machine_result": machine_bindings["candidate"],
                "candidate_command": commands["candidate"],
                "evidence_level": (
                    first_rule_evidence_level if index == 0 else "full"
                ),
                "rationale": "离线机器断言逐规则通过",
            }
            if validation_modes[rule] == "dual_wire":
                row["official_evidence_refs"] = [
                    {
                        "path": official_relative,
                        "sha256": hashlib.sha256(
                            official_evidence.read_bytes()
                        ).hexdigest(),
                    }
                ]
                row["official_machine_result"] = machine_bindings["official"]
                row["official_command"] = commands["official"]
            else:
                row["official_authority"] = dict(official_authority)
            rows.append(row)
        self._write_json(
            assertions_path,
            {
                "schema_version": codex_upgrade.RESULTS_SCHEMA_V2,
                "document_kind": "results",
                "candidate_id": candidate_id,
                "target_version": "0.146.0",
                "profile_id": identity["profile_id"],
                "profile_digest": profile_digest or identity["profile_digest"],
                "official_package_digest": official["package_digest"],
                "candidate_package_digest": candidate["package_digest"],
                "comparison_package_digest": comparison["package_digest"],
                "acceptance_contract_sha256": (
                    codex_upgrade._acceptance_contract_sha256(
                        campaign_dir, classification
                    )
                ),
                "rules": rows,
            },
        )
        return assertions_path

    def test_campaign_cli_exposes_all_staged_commands(self) -> None:
        parser = codex_upgrade._build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers), 1)
        self.assertEqual(
            set(subparsers[0].choices),
            {
                "plan",
                "capture-official",
                "classify",
                "prepare-profile",
                "stage-profile",
                "capture-candidate",
                "compare",
                "accept",
                "all",
                "status",
                "resume",
            },
        )

    def test_campaign_manifest_is_immutable_and_stage_is_write_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            manifest_path = campaign_dir / "campaign.json"
            before = manifest_path.read_bytes()
            self._seal_official_stage(root, campaign_dir, manifest)
            self.assertEqual(manifest_path.read_bytes(), before)
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.save_stage_result(
                    campaign_dir,
                    "capture-official",
                    {"status": "complete"},
                )

    def test_stage_envelope_preserves_result_schema_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, _ = self._create_campaign(Path(directory))
            path = codex_upgrade.save_stage_result(
                campaign_dir,
                "capture-official",
                {
                    "schema_version": "codex-test-official-result/v1",
                    "status": "failed",
                },
            )
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], codex_upgrade.STAGE_SCHEMA)
            self.assertEqual(
                receipt["result_schema_version"],
                "codex-test-official-result/v1",
            )

    def test_capture_stage_cannot_drift_from_approved_seal_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            stage_path = campaign_dir / "official" / "result.json"
            stage = json.loads(stage_path.read_text(encoding="utf-8"))
            stage["identity"]["version"] = "0.146.1"
            stage.pop("package_digest")
            stage["package_digest"] = codex_upgrade._fingerprint(stage)
            self._write_json(stage_path, stage)

            with self.assertRaises(codex_upgrade.ConfigurationError) as caught:
                codex_upgrade._load_stage_result(
                    campaign_dir,
                    "capture-official",
                )
            self.assertIn("seal 预览不一致", str(caught.exception))

    def test_plan_and_status_cli_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            plan_code, plan_stdout, plan_stderr = self._run_main(
                [
                    "plan",
                    "--campaign-dir",
                    str(arguments.campaign_dir),
                    "--baseline-version",
                    arguments.baseline_version,
                    "--target-version",
                    arguments.target_version,
                    "--baseline-source",
                    str(arguments.baseline_source),
                    "--target-source",
                    str(arguments.target_source),
                    "--baseline-evidence",
                    str(arguments.baseline_evidence),
                    "--target-sha256",
                    arguments.target_sha256,
                    "--target-package",
                    str(arguments.target_package),
                    "--target-package-sha256",
                    arguments.target_package_sha256,
                    "--target-code-mode-host-sha256",
                    arguments.target_code_mode_host_sha256,
                    "--runtime-image",
                    arguments.runtime_image,
                    "--rule-manifest",
                    str(arguments.rule_manifest),
                    "--scenario-manifest",
                    str(arguments.scenario_manifest),
                    "--campaign-id",
                    arguments.campaign_id,
                ]
            )
            self.assertEqual(plan_code, 0, plan_stderr)
            self.assertEqual(json.loads(plan_stdout)["status"], "planned")
            status_code, status_stdout, status_stderr = self._run_main(
                ["status", "--campaign-dir", str(arguments.campaign_dir)]
            )
            self.assertEqual(status_code, 0, status_stderr)
            self.assertEqual(json.loads(status_stdout)["status"], "planned")

    def test_plan_rejects_package_helper_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            arguments.target_code_mode_host_sha256 = "f" * 64
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "codex-code-mode-host 摘要不一致",
            ):
                codex_upgrade.create_campaign(arguments)

    def test_candidate_stage_never_overwrites_an_existing_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _ = self._create_campaign(root)
            self._seal_candidate_stage(
                root, campaign_dir, candidate_id="candidate-a"
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.save_stage_result(
                    campaign_dir,
                    "capture-candidate",
                    {"status": "complete"},
                    candidate_id="candidate-a",
                )
            self._seal_candidate_stage(
                root, campaign_dir, candidate_id="candidate-b"
            )
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(
                set(status["candidates"]), {"candidate-a", "candidate-b"}
            )

    def test_campaign_manifest_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, _ = self._create_campaign(Path(directory))
            manifest_path = campaign_dir / "campaign.json"
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["target_sha256"] = "f" * 64
            self._write_json(manifest_path, payload)
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.load_campaign_manifest(campaign_dir)

    def test_classify_requires_baseline_and_target_rule_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root, omit_last=True)
            )
            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("闭环", stderr)

    def test_classify_seals_complete_migration_without_rewriting_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            campaign_bytes = (campaign_dir / "campaign.json").read_bytes()
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            return_code, _, stderr = self._approve_classification(
                campaign_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 0, stderr)
            self.assertEqual(
                (campaign_dir / "campaign.json").read_bytes(), campaign_bytes
            )
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(status["stages"]["classify"], "complete")

    def test_classify_requires_exact_dynamic_discovery_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(
                root,
                campaign_dir,
                manifest,
                include_new_surface=True,
            )
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("未唯一分类", stderr)

    def test_classify_rejects_old_0145_assertion_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            (
                target,
                migration,
                scenario,
                profile,
                assertion_profile,
                _,
            ) = self._write_classification_manifests(root)
            payload = json.loads(
                assertion_profile.read_text(encoding="utf-8")
            )
            payload["codex_version"] = "0.145.0"
            self._write_json(assertion_profile, payload)
            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (
                        target,
                        migration,
                        scenario,
                        profile,
                        assertion_profile,
                    ),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("断言画像 codex_version 不一致", stderr)

    def test_classify_draft_rewrites_every_nested_version_coordinate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)

            receipt = codex_upgrade.classify_campaign(campaign_dir)
            self.assertEqual(receipt["status"], "draft")
            replacement = receipt["assertion_version_replacements"]
            self.assertEqual(replacement["baseline_version"], "0.145.0")
            self.assertEqual(replacement["target_version"], "0.146.0")
            self.assertEqual(replacement["count"], 11)
            self.assertEqual(len(replacement["paths"]), 11)

            assertion_profile = json.loads(
                (Path(receipt["path"]) / "assertion-profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn(
                "0.145.0",
                json.dumps(assertion_profile, ensure_ascii=False),
            )
            coordinates = codex_upgrade._assertion_profile_version_coordinates(
                assertion_profile
            )
            self.assertTrue(coordinates)
            self.assertEqual({version for _, version in coordinates}, {"0.146.0"})

    def test_0147_没有期望覆盖时画像逐字不变(self) -> None:
        """R9 复核后撤销了唯一一条 override，0.147 现在不应有任何期望变更。

        原 override 把 `wham-get-paths` 的 `wham/usage` 改成 `wham/settings/user`，
        依据是「0.147 用后者替代了前者」。这条判定已被双重证伪：0.147 的
        `backend-client/src/client/rate_limit_resets.rs:83` 仍然构造 `{}/wham/usage`，
        而 `settings/user` 在 `client.rs:640` 是另一个独立调用点；relay 面实测
        A12（配额查询）发的正是 usage ＋ rate-limit-reset-credits，与 0.145 一致。

        原判定来自 mitm 面证据，那个采集面恰好只捕到 `settings/user`——是证据面
        选错，不是版本行为变化。
        """

        base_path = (
            Path(__file__).resolve().parents[1]
            / "candidate_rule_expectations_0_145_0.json"
        )
        profile = json.loads(base_path.read_text(encoding="utf-8"))
        updated, count = codex_upgrade._apply_assertion_profile_overrides(
            profile,
            target_version="0.147.0",
        )
        self.assertEqual(count, 0)
        self.assertEqual(updated, profile)

    def test_wham_get_paths_保持_0145_原期望(self) -> None:
        """防回归：不得再把 usage 换成 settings/user。"""

        base_path = (
            Path(__file__).resolve().parents[1]
            / "candidate_rule_expectations_0_145_0.json"
        )
        profile = json.loads(base_path.read_text(encoding="utf-8"))
        check = next(
            check
            for rule in profile["rules"]
            if rule["rule_id"] == "SPEC-EP-019"
            for check in rule["checks"]
            if check["id"] == "wham-get-paths"
        )
        self.assertEqual(
            check["assertion"]["value"],
            [
                "/backend-api/wham/usage",
                "/backend-api/wham/rate-limit-reset-credits",
            ],
        )
        # settings/user 属另一个调用点，不进 A12 的路径集合；画像别处已按
        # not_equal 显式排除它，这里一并锁住，避免两端再次各说各话。
        excluded = [
            condition
            for rule in profile["rules"]
            for check_item in rule["checks"]
            for condition in check_item["select"].get("where", [])
            if condition.get("value") == "/backend-api/wham/settings/user"
        ]
        self.assertTrue(excluded)
        for condition in excluded:
            self.assertEqual(condition["operator"], "not_equal")

    def test_classify_rejects_nested_baseline_version_after_top_level_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            payload = json.loads(assertion_profile.read_text(encoding="utf-8"))
            identity = next(
                check["assertion"]["value"]
                for rule in payload["rules"]
                for check in rule["checks"]
                if isinstance(check["assertion"].get("value"), dict)
                and "user_agent_prefix" in check["assertion"]["value"]
            )
            identity["user_agent_prefix"] = "codex_exec/0.145.0"
            self._write_json(assertion_profile, payload)

            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("仍残留 baseline 版本坐标", stderr)

    def test_classify_rejects_non_target_behavior_version_coordinate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            payload = json.loads(assertion_profile.read_text(encoding="utf-8"))
            query_pair = next(
                pair
                for rule in payload["rules"]
                for check in rule["checks"]
                for pair in (
                    check["assertion"].get("value", {}).get("query_pairs", [])
                    if isinstance(check["assertion"].get("value"), dict)
                    else []
                )
                if pair[0] == "client_version"
            )
            query_pair[1] = "0.144.0"
            self._write_json(assertion_profile, payload)

            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("行为版本坐标与 target_version 不一致", stderr)

    def test_stage_profile_requires_profile_approved_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _ = self._create_campaign(root)
            return_code, _, stderr = self._run_main(
                [
                    "stage-profile",
                    "--campaign-dir",
                    str(campaign_dir),
                    "--output",
                    str(root / "catalog-stage"),
                ]
            )
            self.assertEqual(return_code, 1)
            self.assertIn("只允许从 profile_approved 状态执行", stderr)

    def test_prepare_profile_binds_official_sealed_target_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            snapshot = root / "snapshot.json"
            snapshot.write_text("{}\n", encoding="utf-8")
            output = root / "profile-draft.json"
            profile = {
                "schema_version": codex_upgrade.PROFILE_SCHEMA,
                "codex_version": manifest["target_version"],
                "profile_id": "codex-0.146.0-prepared",
                "profile_digest": "d" * 64,
                "profile_payload": {"prepared": True},
                "profile_payload_sha256": codex_upgrade._fingerprint(
                    {"prepared": True}
                ),
                "status": "draft",
            }

            def prepare(*_args: object, **_kwargs: object) -> argparse.Namespace:
                output.write_text(json.dumps(profile), encoding="utf-8")
                return argparse.Namespace(
                    returncode=0,
                    stdout=json.dumps(profile),
                    stderr="",
                )

            with mock.patch.object(
                codex_upgrade.subprocess,
                "run",
                side_effect=prepare,
            ) as run:
                return_code, stdout, stderr = self._run_main(
                    [
                        "prepare-profile",
                        "--campaign-dir",
                        str(campaign_dir),
                        "--snapshot",
                        str(snapshot),
                        "--profile-id",
                        profile["profile_id"],
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(return_code, 0, stderr)
            self.assertIn('"status": "draft"', stdout)
            self.assertIn("-prepare-snapshot", run.call_args.args[0])

    def test_stage_profile_binds_approved_identity_without_changing_active(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest, _ = self._create_classified_campaign(root)
            classification = codex_upgrade._load_stage_result(
                campaign_dir,
                "classify",
            )
            receipt = {
                "schema_version": "official-egress-catalog-stage/v1",
                "campaign_id": manifest["campaign_id"],
                "classification_sha256": classification[
                    "joint_manifest_sha256"
                ],
                "target_version": manifest["target_version"],
                "target_profile_digest": "c" * 64,
                "candidate_release_mode": "previous",
                "active_unchanged": True,
                "production_selector_changed": False,
            }
            output = root / "catalog-stage"
            asset = b"{}\n"
            inventory = [
                {
                    "path": "catalogdata/runtime/release-catalog.json",
                    "sha256": hashlib.sha256(asset).hexdigest(),
                    "size": len(asset),
                }
            ]
            receipt["inventory"] = inventory
            receipt["inventory_sha256"] = codex_upgrade._fingerprint(inventory)

            def run_stage(*_args: object, **_kwargs: object) -> argparse.Namespace:
                target = output / "catalogdata/runtime/release-catalog.json"
                target.parent.mkdir(parents=True)
                target.write_bytes(asset)
                (output / "catalog-stage-receipt.json").write_text(
                    json.dumps(receipt),
                    encoding="utf-8",
                )
                return argparse.Namespace(
                    returncode=0,
                    stdout=json.dumps(receipt),
                    stderr="",
                )

            with mock.patch.object(
                codex_upgrade.subprocess,
                "run",
                side_effect=run_stage,
            ) as run:
                return_code, stdout, stderr = self._run_main(
                    [
                        "stage-profile",
                        "--campaign-dir",
                        str(campaign_dir),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(return_code, 0, stderr)
            self.assertIn('"active_unchanged": true', stdout)
            command = run.call_args.args[0]
            self.assertIn("./cmd/egresscatalogstage", command)
            self.assertIn(str(output), command)

    def test_classify_supports_explicit_rule_add_delete_and_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            baseline_manifest = (
                Path(__file__).resolve().parents[1]
                / "codex_upgrade_rules_0_145_0.json"
            )
            baseline_rules = list(
                load_rule_manifest(baseline_manifest, "0.145.0")
            )
            added_rule = "SPEC-NEW-001"
            target_rules = [
                rule for rule in baseline_rules if rule != baseline_rules[2]
            ] + [added_rule]
            target = root / "target-rules-mixed.json"
            migration = root / "rule-migration-mixed.json"
            profile = root / "profile-mixed.json"
            assertion_profile = root / "assertion-profile-mixed.json"
            self._write_json(
                target,
                {
                    "schema_version": codex_upgrade.RULE_SCHEMA,
                    "codex_version": "0.146.0",
                    "required_rules": target_rules,
                },
            )
            entries = []
            for index, rule in enumerate(baseline_rules):
                classification = {
                    0: "change",
                    1: "condition_change",
                    2: "delete",
                }.get(index, "inherit")
                entries.append(
                    {
                        "baseline_rule": rule,
                        "target_rule": (
                            None if classification == "delete" else rule
                        ),
                        "classification": classification,
                        "rationale": "显式迁移分类",
                        "evidence_refs": ["official-diff.json"],
                    }
                )
            entries.append(
                {
                    "baseline_rule": None,
                    "target_rule": added_rule,
                    "classification": "add",
                    "rationale": "目标版本新增规则",
                    "evidence_refs": ["official-diff.json"],
                }
            )
            self._write_json(
                migration,
                {
                    "schema_version": codex_upgrade.MIGRATION_SCHEMA,
                    "baseline_version": "0.145.0",
                    "target_version": "0.146.0",
                    "status": "approved",
                    "entries": entries,
                    "discovery_classifications": [],
                },
            )
            profile_payload = {
                "transport": "codex-official-egress",
                "rule_count": len(target_rules),
            }
            self._write_json(
                profile,
                {
                    "schema_version": codex_upgrade.PROFILE_SCHEMA,
                    "codex_version": "0.146.0",
                    "profile_id": "codex-0.146.0-test-v1",
                    "profile_digest": "c" * 64,
                    "profile_payload": profile_payload,
                    "profile_payload_sha256": codex_upgrade._fingerprint(
                        profile_payload
                    ),
                    "status": "approved",
                },
            )
            scenario = self._write_scenario_manifest(
                root,
                target,
                tuple(target_rules),
                version="0.146.0",
                name="scenarios-mixed.json",
            )
            self._write_assertion_profile(
                assertion_profile,
                tuple(target_rules),
                version="0.146.0",
            )
            return_code, _, stderr = self._approve_classification(
                campaign_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 0, stderr)
            self._seal_candidate_stage(root, campaign_dir)
            comparison = codex_upgrade.compare_campaign(
                campaign_dir, "candidate-a"
            )
            self.assertTrue(comparison["coverage"]["complete"])
            self.assertEqual(
                comparison["coverage"]["required_rule_count"],
                len(target_rules),
            )

    def test_blocked_migration_keeps_campaign_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            baseline_manifest = (
                Path(__file__).resolve().parents[1]
                / "codex_upgrade_rules_0_145_0.json"
            )
            rules = load_rule_manifest(baseline_manifest, "0.145.0")
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(
                    root, blocked_rule=rules[0]
                )
            )
            return_code, _, _ = self._approve_classification(
                campaign_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 2)
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(status["status"], "blocked")
            self.assertEqual(status["stages"]["classify"], "blocked")

    def test_compare_campaign_is_offline_and_keeps_candidate_evidence_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, _ = self._create_classified_campaign(root)
            evidence_root, _ = self._seal_candidate_stage(root, campaign_dir)
            evidence_sha = hashlib.sha256(
                (evidence_root / "surface.json").read_bytes()
            ).hexdigest()
            with (
                mock.patch.object(
                    codex_upgrade,
                    "run_job",
                    side_effect=AssertionError("compare 不得运行抓包任务"),
                ),
                mock.patch.object(
                    codex_upgrade.subprocess,
                    "run",
                    side_effect=AssertionError("compare 不得启动外部进程"),
                ),
            ):
                result = codex_upgrade.compare_campaign(
                    campaign_dir, "candidate-a"
                )
            self.assertTrue(result["equal"])
            self.assertEqual(
                hashlib.sha256(
                    (evidence_root / "surface.json").read_bytes()
                ).hexdigest(),
                evidence_sha,
            )

    def test_recovery_failure_blocks_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, _ = self._create_classified_campaign(root)
            self._seal_candidate_stage(
                root, campaign_dir, restoration_passed=False
            )
            failed_stage = codex_upgrade._load_stage_result(
                campaign_dir,
                "capture-candidate",
                "candidate-a",
            )
            self.assertEqual(failed_stage["status"], "failed")
            self.assertTrue(failed_stage["restoration"]["passed"])
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.compare_campaign(campaign_dir, "candidate-a")

    def test_accept_requires_one_passing_assertion_for_every_target_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(
                root, rules, identity, omit_last=True
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.accept_campaign(
                    campaign_dir, "candidate-a", assertions
                )

    def test_accept_rejects_profile_identity_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(
                root,
                rules,
                identity,
                profile_digest="f" * 64,
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.accept_campaign(
                    campaign_dir, "candidate-a", assertions
                )

    def test_accept_rejects_not_applicable_without_full_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(
                root,
                rules,
                identity,
                first_rule_status="not_applicable",
                first_rule_evidence_level="partial",
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.accept_campaign(
                    campaign_dir, "candidate-a", assertions
                )

    def test_accept_succeeds_only_after_compare_and_all_rule_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            comparison = codex_upgrade.compare_campaign(
                campaign_dir, "candidate-a"
            )
            self.assertTrue(comparison["equal"])
            assertions = self._write_assertions(root, rules, identity)
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                acceptance = codex_upgrade.accept_campaign(
                    campaign_dir, "candidate-a", assertions
                )
            self.assertTrue(acceptance["accepted"])
            candidate = codex_upgrade._load_stage_result(
                campaign_dir, "capture-candidate", "candidate-a"
            )
            self.assertEqual(
                {
                    (item["client_id"], item["protocol"], item["entrypoint"])
                    for item in candidate["client_bindings"]
                },
                {
                    (
                        "kilo-compatible",
                        "openai-compatible",
                        "/v1/chat/completions",
                    ),
                    (
                        "kilo-responses",
                        "openai-responses",
                        "/v1/responses",
                    ),
                },
            )
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(status["status"], "ready")

    def test_accept_rejects_handwritten_machine_pass_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(root, rules, identity)

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "离线重放",
            ):
                codex_upgrade.accept_campaign(
                    campaign_dir, "candidate-a", assertions
                )

    def test_accept_rejects_evidence_tampering_after_compare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            evidence_root, identity = self._seal_candidate_stage(
                root, campaign_dir
            )
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(root, rules, identity)
            (evidence_root / "surface.json").write_text(
                '{"records":[]}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "原始证据摘要",
            ):
                codex_upgrade.accept_campaign(
                    campaign_dir, "candidate-a", assertions
                )

    def _accept_with_mutated_results(
        self,
        root: Path,
        mutate,
    ):
        """构造合法 v2 results 后按需变异，返回 accept 调用结果或异常。"""

        campaign_dir, _, rules = self._create_classified_campaign(root)
        _, identity = self._seal_candidate_stage(root, campaign_dir)
        codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
        assertions = self._write_assertions(root, rules, identity)
        document = json.loads(assertions.read_text(encoding="utf-8"))
        mutate(document)
        self._write_json(assertions, document)
        with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
            return codex_upgrade.accept_campaign(
                campaign_dir, "candidate-a", assertions
            )

    def test_accept_rejects_legacy_v1_results_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                document["schema_version"] = "codex-egress-rule-assertions/v1"

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "旧 schema 已废除"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_wrong_validation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                for row in document["rules"]:
                    if row["validation_mode"] == "dual_wire":
                        row["validation_mode"] = "candidate_profile"
                        break

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "validation_mode 与验收契约不一致"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_candidate_profile_row_carrying_official_side(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                for row in document["rules"]:
                    if row["validation_mode"] == "candidate_profile":
                        row["official_command"] = ["python3", "fake.py"]
                        break

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "字段不闭合"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_forged_official_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                for row in document["rules"]:
                    if row["validation_mode"] == "candidate_profile":
                        row["official_authority"]["review_sha256"] = "f" * 64
                        break

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "官方权威"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_contract_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                document["acceptance_contract_sha256"] = "0" * 64

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "冻结验收契约摘要"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_missing_positive_negative_leftovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                document["rules"][0]["positive_assertions"] = ["x"]

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "字段不闭合"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_candidate_specific_status_does_not_leak_between_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity_a = self._seal_candidate_stage(
                root, campaign_dir, candidate_id="candidate-a"
            )
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(
                root,
                rules,
                identity_a,
                candidate_id="candidate-a",
            )
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                accepted = codex_upgrade.accept_campaign(
                    campaign_dir, "candidate-a", assertions
                )
            self.assertTrue(accepted["accepted"])
            self._seal_candidate_stage(
                root, campaign_dir, candidate_id="candidate-b"
            )

            status_a = codex_upgrade.campaign_status(
                campaign_dir, "candidate-a"
            )
            status_b = codex_upgrade.campaign_status(
                campaign_dir, "candidate-b"
            )
            self.assertEqual(status_a["status"], "ready")
            self.assertEqual(status_b["status"], "candidate_sealed")
            self.assertEqual(
                status_b["candidate_states"]["candidate-a"], "ready"
            )
            self.assertEqual(
                status_b["candidate_states"]["candidate-b"],
                "candidate_sealed",
            )

    def test_0145_rule_manifest_contains_exact_required_scope(self) -> None:
        manifest = (
            Path(__file__).resolve().parents[1]
            / "codex_upgrade_rules_0_145_0.json"
        )
        rules = load_rule_manifest(manifest, "0.145.0")
        self.assertEqual(len(rules), 42)
        self.assertIn("SPEC-EP-023", rules)
        self.assertNotIn("SPEC-H2-001", rules)
        self.assertNotIn("SPEC-WS-003", rules)
        self.assertNotIn("SPEC-BODY-007", rules)

    def test_source_inventory_detects_endpoint_and_dependency_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            target = root / "target"
            for source in (baseline, target):
                (source / "src").mkdir(parents=True)
            (baseline / "Cargo.lock").write_text(
                '[[package]]\nname = "reqwest"\nversion = "0.12.28"\n',
                encoding="utf-8",
            )
            (target / "Cargo.lock").write_text(
                '[[package]]\nname = "reqwest"\nversion = "0.13.0"\n',
                encoding="utf-8",
            )
            (baseline / "src/client.rs").write_text(
                'client.post("/backend-api/codex/responses").send();\n',
                encoding="utf-8",
            )
            (target / "src/client.rs").write_text(
                (
                    'client.post("/backend-api/codex/responses").send();\n'
                    'client.post("/backend-api/codex/new-egress").send();\n'
                ),
                encoding="utf-8",
            )
            baseline_inventory = scan_source_tree(baseline, "0.145.0")
            target_inventory = scan_source_tree(target, "0.146.0")
            difference = compare_inventory(
                baseline_inventory, target_inventory
            )
            added_values = {
                item["value"] for item in difference["added"]
            }
            self.assertIn(
                "/backend-api/codex/new-egress", added_values
            )
            self.assertTrue(
                any("reqwest|0.13.0" in value for value in added_values)
            )

    def test_dynamic_surface_detects_new_route_and_body_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            target = root / "target"
            baseline.mkdir()
            target.mkdir()
            common = {
                "method": "POST",
                "path": "/backend-api/codex/responses",
                "http_version": "HTTP/1.1",
                "headers": [["version", "0.145.0"], ["host", "chatgpt.com"]],
                "json_shape": {"model": "<string>", "input": []},
            }
            (baseline / "surface.json").write_text(
                json.dumps({"records": [{"request": common}]}),
                encoding="utf-8",
            )
            new_request = {
                **common,
                "path": "/backend-api/codex/new-egress?token=secret",
                "json_shape": {"model": "<string>", "new_field": True},
            }
            (target / "surface.json").write_text(
                json.dumps(
                    {"records": [{"request": common}, {"request": new_request}]}
                ),
                encoding="utf-8",
            )
            baseline_surface = scan_evidence([baseline], "baseline")
            target_surface = scan_evidence([target], "target")
            difference = compare_surfaces(baseline_surface, target_surface)
            self.assertEqual(difference["added_count"], 1)
            self.assertEqual(
                difference["added"][0]["path"],
                "/backend-api/codex/new-egress?token",
            )
            serialized = json.dumps(difference, ensure_ascii=False)
            self.assertNotIn("secret", serialized)

    def test_raw_mitm_body_is_reduced_to_shape_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "request": {
                    "method": "POST",
                    "path": "/backend-api/codex/responses",
                    "http_version": "HTTP/1.1",
                    "headers": [["authorization", "SECRET-TOKEN"]],
                    "body": {
                        "text": json.dumps(
                            {
                                "model": "gpt-test",
                                "input": [{"role": "user", "content": "SECRET-TEXT"}],
                            }
                        )
                    },
                }
            }
            (root / "capture.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )
            result = scan_evidence([root], "candidate")
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("SECRET-TOKEN", serialized)
            self.assertNotIn("SECRET-TEXT", serialized)
            self.assertEqual(result["surface_count"], 1)
            self.assertIsNotNone(
                result["surfaces"][0]["body_shape_sha256"]
            )

    def test_h2_request_signature_includes_host_and_query_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = {
                "connections": [
                    {
                        "protocol": "h2",
                        "frames": [
                            {
                                "type": "HEADERS",
                                "header_names_in_order": [
                                    ":method",
                                    ":scheme",
                                    ":authority",
                                    ":path",
                                    "content-type",
                                ],
                                "headers": [
                                    {"name": ":method", "value": "POST"},
                                    {"name": ":scheme", "value": "https"},
                                    {
                                        "name": ":authority",
                                        "value": "api.openai.com",
                                    },
                                    {
                                        "name": ":path",
                                        "value": "/v1/new?token=<redacted>",
                                    },
                                    {
                                        "name": "content-type",
                                        "value": "application/json",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
            (root / "relay.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
            result = scan_evidence([root], "h2")
            self.assertEqual(result["surface_count"], 1)
            surface = result["surfaces"][0]
            self.assertEqual(surface["host"], "api.openai.com")
            self.assertEqual(surface["path"], "/v1/new?token")
            self.assertEqual(surface["protocol"], "h2")
            self.assertEqual(
                surface["header_names"], summary["connections"][0]["frames"][0]["header_names_in_order"]
            )

    def test_rule_coverage_requires_official_and_candidate_evidence(self) -> None:
        official = Job(
            job_id="official",
            phase="official",
            suites=("full",),
            description="official",
            steps=(),
            evidence_roots=(),
            covers=("SPEC-H1-001",),
        )
        candidate = Job(
            job_id="candidate",
            phase="candidate",
            suites=("full",),
            description="candidate",
            steps=(),
            evidence_roots=(),
            covers=("SPEC-H1-001",),
        )
        incomplete = build_coverage(
            ("SPEC-H1-001",),
            [official, candidate],
            [
                {"id": "official", "status": "complete"},
                {"id": "candidate", "status": "failed"},
            ],
        )
        self.assertFalse(incomplete["complete"])
        complete = build_coverage(
            ("SPEC-H1-001",),
            [official, candidate],
            [
                {"id": "official", "status": "complete"},
                {"id": "candidate", "status": "complete"},
            ],
        )
        self.assertTrue(complete["complete"])

    def test_run_job_fails_when_any_evidence_root_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            present_root = root / "present"
            present_root.mkdir()
            (present_root / "evidence.json").write_text(
                '{"status":"complete"}\n',
                encoding="utf-8",
            )
            self._make_private_tree(present_root)
            missing_root = root / "missing"
            log_root = root / "logs"
            log_root.mkdir()
            job = Job(
                job_id="multi-root",
                phase="official",
                suites=("full",),
                description="任一证据根缺失都必须失败",
                steps=({"argv": ["true"], "timeout": 30},),
                evidence_roots=(str(present_root), str(missing_root)),
                covers=("SPEC-H1-001",),
                scenario_ids=("A01",),
            )
            result = codex_upgrade.run_job(job, log_root)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                result["missing_evidence_patterns"], [str(missing_root)]
            )


if __name__ == "__main__":
    unittest.main()
