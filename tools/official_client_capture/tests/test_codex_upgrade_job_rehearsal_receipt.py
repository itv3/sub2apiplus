"""Codex 完整 Job ARM64 离线演练收据的正反向测试。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as receipt
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture.tests.control_receipt_fixtures import (
    create_job_rehearsal_receipt,
)


class JobRehearsalReceiptTests(unittest.TestCase):
    def test_evaluator_manifests_do_not_invalidate_capture_jobs(self) -> None:
        """评估器和版本化清单变化不得被 shared 扩大成抓包 Job 重跑。"""

        with tempfile.TemporaryDirectory() as directory:
            contract = self._contract(Path(directory))
        old_entries = [
            {"path": "stable_helper.py", "sha256": "1" * 64},
            {"path": "candidate_rule_assertion.py", "sha256": "2" * 64},
            {
                "path": "candidate_rule_expectations_0_149_1.json",
                "sha256": "3" * 64,
            },
            {
                "path": "codex_upgrade_rules_0_151_0.json",
                "sha256": "4" * 64,
            },
            {
                "path": "codex_upgrade_scenarios_0_151_0.json",
                "sha256": "5" * 64,
            },
            {
                "path": "codex_upgrade_evidence_labels_0_151_0.json",
                "sha256": "6" * 64,
            },
        ]
        new_entries = copy.deepcopy(old_entries)
        for item in new_entries:
            if item["path"] != "stable_helper.py":
                item["sha256"] = "f" * 64

        job = codex_upgrade.Job(
            job_id="candidate-frozen-aux",
            phase="candidate",
            suites=("full",),
            description="candidate-frozen-aux",
            steps=({"argv": ["true"], "environment": {}, "timeout": 1},),
            evidence_roots=(),
            covers=(),
        )
        old_components = receipt._component_summary({"entries": old_entries})
        document = receipt._job_document(job)
        metadata = receipt._job_incremental_metadata(document, old_components)
        previous_facts = {
            "execution_contract": contract,
            "tool_components": old_components,
            "tool_trees": {"managed_host": {"entries": old_entries}},
            "jobs": [
                {
                    "id": job.job_id,
                    "status": "passed",
                    "job_contract_sha256": metadata["input_sha256"],
                    "incremental_result_key": metadata["result_key"],
                    "input_sha256": metadata["input_sha256"],
                    "environment_sha256": metadata["environment_sha256"],
                    "dependency_sha256": metadata["dependency_sha256"],
                    "tool_components": metadata["components"],
                    "tool_component_digests": metadata["component_digests"],
                }
            ],
        }

        current_contract = copy.deepcopy(contract)
        current_contract["evidence_label_declaration_sha256"] = "f" * 64
        plan = receipt._select_rehearsal_plan(
            [job],
            current_contract,
            receipt._component_summary({"entries": new_entries}),
            previous_facts,
        )

        self.assertEqual(plan["execute_job_ids"], [])
        self.assertEqual(plan["reused_job_ids"], ["candidate-frozen-aux"])
        for path in (
            "candidate_rule_assertion.py",
            "candidate_rule_expectations_0_149_1.json",
            "codex_upgrade_rules_0_151_0.json",
            "codex_upgrade_scenarios_0_151_0.json",
            "codex_upgrade_evidence_labels_0_151_0.json",
        ):
            self.assertEqual(receipt._job_component_for_path(path), "evaluator")

    def test_mitm_checkpoint_helper_only_invalidates_mitm_runner_jobs(self) -> None:
        """新增 MITM helper 不得误伤依赖 shared 的 frozen-aux。"""

        with tempfile.TemporaryDirectory() as directory:
            contract = self._contract(Path(directory))
        old_entries = [
            {"path": "stable_helper.py", "sha256": "1" * 64},
            {
                "path": "run_sub2api_openai_mitm_matrix.sh",
                "sha256": "2" * 64,
            },
        ]
        new_entries = [
            *old_entries,
            {"path": "mitm_scenario_checkpoint.py", "sha256": "3" * 64},
        ]

        def job(job_id: str, argv: list[str]) -> object:
            return codex_upgrade.Job(
                job_id=job_id,
                phase="candidate",
                suites=("full",),
                description=job_id,
                steps=(
                    {
                        "argv": argv,
                        "environment": {},
                        "timeout": 120,
                    },
                ),
                evidence_roots=(),
                covers=(),
            )

        jobs = [
            job(
                "candidate-core-mitm",
                [
                    "bash",
                    "/root/oauth-capture/tools/official_client_capture/"
                    "run_sub2api_openai_mitm_matrix.sh",
                ],
            ),
            job("candidate-frozen-aux", ["true"]),
        ]
        old_components = receipt._component_summary({"entries": old_entries})
        previous_jobs = []
        for current in jobs:
            document = receipt._job_document(current)
            metadata = receipt._job_incremental_metadata(
                document,
                old_components,
            )
            previous_jobs.append(
                {
                    "id": current.job_id,
                    "status": "passed",
                    "job_contract_sha256": metadata["input_sha256"],
                    "incremental_result_key": metadata["result_key"],
                    "input_sha256": metadata["input_sha256"],
                    "environment_sha256": metadata["environment_sha256"],
                    "dependency_sha256": metadata["dependency_sha256"],
                    "tool_components": metadata["components"],
                    "tool_component_digests": metadata["component_digests"],
                }
            )
        previous_facts = {
            "execution_contract": contract,
            "tool_components": old_components,
            "tool_trees": {"managed_host": {"entries": old_entries}},
            "jobs": previous_jobs,
        }

        plan = receipt._select_rehearsal_plan(
            jobs,
            contract,
            receipt._component_summary({"entries": new_entries}),
            previous_facts,
        )

        self.assertEqual(plan["execute_job_ids"], ["candidate-core-mitm"])
        self.assertEqual(plan["reused_job_ids"], ["candidate-frozen-aux"])
        self.assertEqual(
            receipt._job_component_for_path("mitm_scenario_checkpoint.py"),
            "runner.run_sub2api_openai_mitm_matrix",
        )

    def test_fingerprint_capture_files_only_map_to_mitm_runner(self) -> None:
        """指纹转发器、预热与专用启动接线不得落入 shared。"""

        for path in (
            "build_fingerprint_proxy.sh",
            "prewarm_codex_home.py",
            "runtime_scripts/run_fingerprint_mitm_pair.sh",
            "runtime_scripts/start_mitm.sh",
            "fingerprint_proxy/go.mod",
            "fingerprint_proxy/go.sum",
            "fingerprint_proxy/main.go",
        ):
            self.assertEqual(
                receipt._job_component_for_path(path),
                "runner.run_sub2api_openai_mitm_matrix",
            )

    def test_historical_component_remap_only_invalidates_changed_runner(self) -> None:
        """旧 relay/shared 粗分组不得把控制变化扩大为全部 Job。"""

        with tempfile.TemporaryDirectory() as directory:
            contract = self._contract(Path(directory))
        old_entries = [
            {"path": "codex_upgrade.py", "sha256": "1" * 64},
            {"path": "codex_upgrade_job_rehearsal_receipt.py", "sha256": "2" * 64},
            {"path": "codex_upgrade_supervisor.py", "sha256": "3" * 64},
            {"path": "run_sub2api_direct_matrix.sh", "sha256": "4" * 64},
            {"path": "run_sub2api_openai_mitm_matrix.sh", "sha256": "5" * 64},
        ]
        new_entries = copy.deepcopy(old_entries)
        for item in new_entries:
            if item["path"] in {
                "codex_upgrade.py",
                "codex_upgrade_job_rehearsal_receipt.py",
                "codex_upgrade_supervisor.py",
                "run_sub2api_openai_mitm_matrix.sh",
            }:
                item["sha256"] = "f" * 64
        current_components = receipt._component_summary({"entries": new_entries})
        broad_components = incremental_recovery.build_component_identities(
            old_entries,
            {item["path"]: "shared" for item in old_entries},
            default_component="shared",
        )

        def job(job_id: str, runner: str) -> object:
            return codex_upgrade.Job(
                job_id=job_id,
                phase="candidate",
                suites=("full",),
                description=job_id,
                steps=(
                    {
                        "argv": [
                            "bash",
                            "/root/oauth-capture/tools/official_client_capture/"
                            + runner,
                        ],
                        "environment": {},
                        "timeout": 120,
                    },
                ),
                evidence_roots=(),
                covers=(),
            )

        jobs = [
            job("candidate-core-direct", "run_sub2api_direct_matrix.sh"),
            job("candidate-core-mitm", "run_sub2api_openai_mitm_matrix.sh"),
        ]
        previous_jobs = []
        for current in jobs:
            document = receipt._job_document(current)
            previous_jobs.append(
                {
                    "id": current.job_id,
                    "status": "passed",
                    "job_contract_sha256": receipt._fingerprint(document),
                    "incremental_result_key": "a" * 64,
                }
            )
        previous_facts = {
            "execution_contract": contract,
            "tool_components": broad_components,
            "tool_trees": {"managed_host": {"entries": old_entries}},
            "jobs": previous_jobs,
        }
        plan = receipt._select_rehearsal_plan(
            jobs,
            contract,
            current_components,
            previous_facts,
        )
        self.assertEqual(plan["execute_job_ids"], ["candidate-core-mitm"])
        self.assertEqual(plan["reused_job_ids"], ["candidate-core-direct"])
        self.assertEqual(
            plan["reasons"]["candidate-core-direct"],
            "historical_component_map_reclassified",
        )

    def test_collect_forwards_original_previous_receipt_root(self) -> None:
        """跨 Campaign 复用必须保留前序收据的原始根坐标。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            source_root = root / "source"
            source_root.mkdir(mode=0o700)
            current_root = root / "current"
            current_root.mkdir(mode=0o700)
            campaign = root / "campaign"
            campaign.mkdir(mode=0o700)
            with mock.patch.object(
                receipt,
                "collect_facts",
                return_value={"status": "incremental-noop"},
            ) as collect_facts:
                receipt.collect(
                    current_root,
                    "facts.json",
                    campaign_dir=campaign,
                    previous_receipt="receipt.json",
                    previous_receipt_root=source_root,
                    rerun_failed=True,
                )
            self.assertEqual(
                collect_facts.call_args.kwargs["previous_receipt_root"],
                source_root,
            )

    def _contract(self, root: Path) -> dict[str, object]:
        scenario_path = Path(receipt.__file__).with_name(
            "codex_upgrade_scenarios_0_151_0.json"
        )
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        return receipt.build_execution_contract(
            target_version="0.151.0",
            target_sha256="1" * 64,
            target_package_sha256="2" * 64,
            target_code_mode_host_sha256="3" * 64,
            suite="full",
            tool_files_sha256=codex_upgrade._tool_identity()["files_sha256"],
            configuration={
                "runtime_image": f"capture-runtime@sha256:{'4' * 64}",
                "model": "gpt-5.5",
                "lite_model": "gpt-5.6-luna",
                "capture_root": "/root/oauth-capture",
                "capture_container": "capture-cli",
                "service_container": "sub2apiplus",
                "keeper_container": "sub2apiplus-keeper",
                "postgres_container": "sub2apiplus-postgres",
                "redis_container": "sub2apiplus-redis",
                "capture_codex_bin": "/opt/codex-0.151.0/bin/codex",
                "relay_codex_bin": "/opt/codex-0.151.0/bin/codex",
                "capture_code_mode_host_bin": (
                    "/opt/codex-0.151.0/bin/codex-code-mode-host"
                ),
                "relay_code_mode_host_bin": (
                    "/opt/codex-0.151.0/bin/codex-code-mode-host"
                ),
                "codex_account_id": 90,
                "api_key_id": 1,
                "live_attestation_compose_dir": str((root / "compose").resolve()),
                "live_attestation_compose_files": "-f compose.yml",
            },
            target_scenario=scenario,
            extra_jobs=None,
        )

    @staticmethod
    def _rewrite(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def test_0151_contract_expands_all_38_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = self._contract(Path(directory))
        self.assertEqual(contract["job_count"], 38)
        self.assertEqual(contract["phase_counts"], {"official": 29, "candidate": 9})
        declaration_path = Path(receipt.__file__).with_name(
            "codex_upgrade_evidence_labels_0_151_0.json"
        )
        self.assertEqual(
            contract["evidence_label_declaration_sha256"],
            receipt._sha256_file(declaration_path),
        )
        self.assertEqual(
            contract["c2pa_job_identities"],
            {
                "official-relay-file-upload-c2pa-negative": {
                    "scenario_job_id": "official-relay-file-upload-c2pa-negative",
                    "expectation": "negative",
                },
                "official-relay-file-upload-c2pa-positive": {
                    "scenario_job_id": "official-relay-file-upload-c2pa-positive",
                    "expectation": "positive",
                },
            },
        )

    def test_job_probe_accepts_empty_optional_environment_value(self) -> None:
        """可选环境变量的空字符串必须与场景 Schema 和计划器保持一致。"""

        job = codex_upgrade.Job(
            job_id="candidate-frozen-aux",
            phase="candidate",
            suites=("full",),
            description="candidate-frozen-aux",
            steps=(
                {
                    "argv": ["true"],
                    "environment": {
                        "CODEX_VERSION": "0.151.0",
                        "LIVE_ATTESTATION_COMPOSE_DIR": "",
                    },
                    "timeout": 1,
                },
            ),
            evidence_roots=("/root/oauth-capture/runs/candidate-frozen-aux",),
            covers=("SPEC-EP-002",),
        )
        with mock.patch.object(
            receipt,
            "_syntax_probe",
            return_value=("host", []),
        ):
            result = receipt._job_probe(job, "capture-cli")

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["step_count"], 1)

    def test_storage_probe_rejects_readonly_parents_without_writable_children(
        self,
    ) -> None:
        """只读父挂载不能再被语法探针错误放行为可运行环境。"""

        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data"
            data_root.mkdir(mode=0o700)
            (data_root / "runs").mkdir(mode=0o700)
            (data_root / "runtime").mkdir(mode=0o700)
            configuration = self._contract(Path(directory))["configuration"]
            aliases = sorted(
                {
                    str(receipt.CAPTURE_CONTAINER_ALIAS),
                    str(configuration["capture_root"]),
                }
            )
            inspected = [
                {
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": str(data_root),
                            "Destination": alias,
                            "RW": False,
                        }
                        for alias in aliases
                    ]
                }
            ]
            job = codex_upgrade.Job(
                job_id="official-core",
                phase="official",
                suites=("full",),
                description="official-core",
                steps=(
                    {"argv": ["true"], "environment": {}, "timeout": 1},
                ),
                evidence_roots=(
                    f"{configuration['capture_root']}/runs/official-core",
                ),
                covers=("SPEC-TLS-001",),
            )
            with (
                mock.patch.object(
                    receipt,
                    "EXPECTED_HOST_DATA_ROOT",
                    PurePosixPath(str(data_root)),
                ),
                mock.patch.object(
                    receipt,
                    "_run",
                    return_value=json.dumps(inspected).encode(),
                ) as run,
            ):
                with self.assertRaisesRegex(
                    receipt.JobRehearsalReceiptError,
                    "缺少同源可写运行挂载",
                ):
                    receipt._capture_storage_probe([job], configuration)
            run.assert_called_once()

    def test_storage_probe_accepts_readonly_parents_with_same_source_children(
        self,
    ) -> None:
        """runs/runtime 同源可写子挂载必须通过有界创建清理事实。"""

        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data"
            data_root.mkdir(mode=0o700)
            for namespace in receipt.WRITABLE_CAPTURE_NAMESPACES:
                (data_root / namespace).mkdir(mode=0o700)
            configuration = self._contract(Path(directory))["configuration"]
            aliases = sorted(
                {
                    str(receipt.CAPTURE_CONTAINER_ALIAS),
                    str(configuration["capture_root"]),
                }
            )
            mounts = [
                {
                    "Type": "bind",
                    "Source": str(data_root),
                    "Destination": alias,
                    "RW": False,
                }
                for alias in aliases
            ]
            namespaces = []
            for namespace in receipt.WRITABLE_CAPTURE_NAMESPACES:
                source = data_root / namespace
                metadata = source.stat()
                mounts.extend(
                    {
                        "Type": "bind",
                        "Source": str(source),
                        "Destination": f"{alias}/{namespace}",
                        "RW": True,
                    }
                    for alias in aliases
                )
                namespaces.append(
                    {
                        "name": namespace,
                        "destinations": [
                            {
                                "path": f"{alias}/{namespace}",
                                "device": metadata.st_dev,
                                "inode": metadata.st_ino,
                                "mode": metadata.st_mode & 0o777,
                                "uid": metadata.st_uid,
                                "gid": metadata.st_gid,
                            }
                            for alias in aliases
                        ],
                        "created_via": aliases,
                        "cleanup_verified": True,
                    }
                )
            job = codex_upgrade.Job(
                job_id="official-core",
                phase="official",
                suites=("full",),
                description="official-core",
                steps=(
                    {"argv": ["true"], "environment": {}, "timeout": 1},
                ),
                evidence_roots=(
                    f"{configuration['capture_root']}/runs/official-core",
                ),
                covers=("SPEC-TLS-001",),
            )
            inspect_raw = json.dumps([{"Mounts": mounts}]).encode()
            write_raw = json.dumps(
                {"status": "passed", "namespaces": namespaces}
            ).encode()
            archive_source_name = "codex-archive-route-unit"
            archive_name = (
                archive_source_name + receipt.FAILED_EVIDENCE_ARCHIVE_SUFFIX
            )
            archive_sources = [
                f"{alias}/runs/{archive_source_name}" for alias in aliases
            ]
            archive_targets = [
                f"{alias}/runs/{archive_name}" for alias in aliases
            ]
            host_source = data_root / "runs" / archive_source_name
            host_target = data_root / "runs" / archive_name
            archive_route = {
                "status": "passed",
                "namespace": "runs",
                "source_name": archive_source_name,
                "archive_name": archive_name,
                "host_source": str(host_source),
                "host_archive": str(host_target),
                "container_sources": archive_sources,
                "container_archives": archive_targets,
                "created_via": archive_sources[0],
                "archived_via": str(host_target),
                "read_via": archive_targets,
                "device": (data_root / "runs").stat().st_dev,
                "inode": (data_root / "runs").stat().st_ino,
                "cleanup_verified": True,
            }
            with (
                mock.patch.object(
                    receipt,
                    "EXPECTED_HOST_DATA_ROOT",
                    PurePosixPath(str(data_root)),
                ),
                mock.patch.object(
                    receipt,
                    "_run",
                    side_effect=[inspect_raw, write_raw],
                ) as run,
                mock.patch.object(
                    receipt,
                    "_capture_archive_route_probe",
                    return_value=archive_route,
                ) as archive_probe,
            ):
                result = receipt._capture_storage_probe([job], configuration)
                validated = receipt._validate_storage_probe(
                    result,
                    {
                        "configuration": configuration,
                        "job_ids": [job.job_id],
                    },
                )
            self.assertEqual(run.call_count, 2)
            archive_probe.assert_called_once()
            self.assertEqual(validated["status"], "passed")
            self.assertEqual(validated["job_count"], 1)
            self.assertEqual(validated["evidence_root_count"], 1)

    def test_archive_route_probe_closes_host_mapping_and_alias_read(self) -> None:
        """P0 必须真实走完容器创建、宿主归档、双别名读取和清理。"""

        with tempfile.TemporaryDirectory() as directory:
            host_runs = Path(directory) / "runs"
            host_runs.mkdir()
            aliases = [
                PurePosixPath("/capture"),
                PurePosixPath("/root/oauth-capture"),
            ]
            call_count = 0

            def fake_run(
                argv: list[str],
                label: str,
                timeout: int = 60,
                **_kwargs: object,
            ) -> bytes:
                nonlocal call_count
                call_count += 1
                self.assertEqual(timeout, 30)
                if call_count == 1:
                    self.assertIn("归档创建探针", label)
                    sources = json.loads(argv[-3])
                    marker_name = argv[-2]
                    payload = bytes.fromhex(argv[-1])
                    source = host_runs / PurePosixPath(sources[0]).name
                    source.mkdir(mode=0o700)
                    (source / marker_name).write_bytes(payload)
                    metadata = source.stat()
                    return json.dumps(
                        {
                            "status": "created",
                            "sources": sources,
                            "created_via": sources[0],
                            "device": metadata.st_dev,
                            "inode": metadata.st_ino,
                        }
                    ).encode()

                self.assertEqual(call_count, 2)
                self.assertIn("跨别名读取清理探针", label)
                sources = json.loads(argv[-4])
                archives = json.loads(argv[-3])
                marker_name = argv[-2]
                payload = bytes.fromhex(argv[-1])
                self.assertFalse(
                    (host_runs / PurePosixPath(sources[0]).name).exists()
                )
                archived = host_runs / PurePosixPath(archives[0]).name
                self.assertEqual((archived / marker_name).read_bytes(), payload)
                metadata = archived.stat()
                (archived / marker_name).unlink()
                archived.rmdir()
                return json.dumps(
                    {
                        "status": "passed",
                        "read_via": archives,
                        "device": metadata.st_dev,
                        "inode": metadata.st_ino,
                        "cleanup_verified": True,
                    }
                ).encode()

            with mock.patch.object(receipt, "_run", side_effect=fake_run):
                result = receipt._capture_archive_route_probe(
                    "capture-cli",
                    aliases,
                    host_runs,
                )

            self.assertEqual(call_count, 2)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["read_via"], result["container_archives"])
            self.assertTrue(result["cleanup_verified"])
            self.assertEqual(list(host_runs.iterdir()), [])

    def test_incremental_plan_reuses_exact_campaign_coordinate_relocation(
        self,
    ) -> None:
        """新 preflight 只改受管 Campaign 坐标时不得重跑已通过 Job。"""

        old_campaign = "c0151-p0-old"
        new_campaign = "c0151-p0-new"

        def job(campaign_id: str, *, description: str = "official-compact") -> object:
            return codex_upgrade.Job(
                job_id="official-compact",
                phase="official",
                suites=("full",),
                description=description,
                steps=(
                    {
                        "argv": [
                            "bash",
                            "/root/oauth-capture/tools/official_client_capture/"
                            "run_official_codex_compact_capture.sh",
                        ],
                        "environment": {"RUN_ID": f"{campaign_id}-official-compact"},
                        "timeout": 1200,
                    },
                ),
                evidence_roots=(
                    f"/root/oauth-capture/runs/{campaign_id}-official-compact",
                ),
                covers=("SPEC-EP-007",),
            )

        with tempfile.TemporaryDirectory() as directory:
            contract = self._contract(Path(directory))
        component_summary = receipt._component_summary(
            receipt._tool_tree_summary(Path(receipt.__file__).resolve().parent)
        )
        old_job = job(old_campaign)
        old_document = receipt._job_document(old_job)
        old_meta = receipt._job_incremental_metadata(
            old_document,
            component_summary,
        )
        previous_facts = {
            "execution_contract": contract,
            "preflight_campaign": {"campaign_id": old_campaign},
            "tool_components": component_summary,
            "jobs": [
                {
                    "id": old_job.job_id,
                    "status": "passed",
                    "job_contract_sha256": old_meta["input_sha256"],
                    "input_sha256": old_meta["input_sha256"],
                    "environment_sha256": old_meta["environment_sha256"],
                    "dependency_sha256": old_meta["dependency_sha256"],
                    "incremental_result_key": old_meta["result_key"],
                    "tool_components": old_meta["components"],
                    "tool_component_digests": old_meta["component_digests"],
                }
            ],
        }

        plan = receipt._select_rehearsal_plan(
            [job(new_campaign)],
            contract,
            component_summary,
            previous_facts,
            current_campaign_id=new_campaign,
        )
        self.assertEqual(plan["execute_job_ids"], [])
        self.assertEqual(plan["reused_job_ids"], ["official-compact"])
        self.assertEqual(
            plan["reasons"]["official-compact"],
            "campaign_coordinate_relocated",
        )

        changed = receipt._select_rehearsal_plan(
            [job(new_campaign, description="changed")],
            contract,
            component_summary,
            previous_facts,
            current_campaign_id=new_campaign,
        )
        self.assertEqual(changed["execute_job_ids"], ["official-compact"])

    def test_missing_0151_evidence_label_declaration_fails_closed(self) -> None:
        scenario_path = Path(receipt.__file__).with_name(
            "codex_upgrade_scenarios_0_151_0.json"
        )
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError,
                "缺少证据标签声明",
            ):
                receipt._target_evidence_label_declaration_sha256(
                    "0.151.0",
                    scenario,
                    tool_root=Path(directory),
                )

    def test_0151_evidence_label_declaration_must_cover_every_job(self) -> None:
        source = Path(receipt.__file__).with_name(
            "codex_upgrade_evidence_labels_0_151_0.json"
        )
        scenario_path = Path(receipt.__file__).with_name(
            "codex_upgrade_scenarios_0_151_0.json"
        )
        declaration = json.loads(source.read_text(encoding="utf-8"))
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        declaration["entries"] = declaration["entries"][:-1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / source.name
            self._rewrite(path, declaration)
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError,
                "未精确覆盖正式 Job 集",
            ):
                receipt._target_evidence_label_declaration_sha256(
                    "0.151.0",
                    scenario,
                    tool_root=root,
                )

    def test_finalize_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = create_job_rehearsal_receipt(
                root,
                contract=self._contract(root),
                preflight_campaign_id="preflight-0151",
            )
            replayed = receipt.replay(root, path.name)
        self.assertEqual(replayed["status"], "passed")
        self.assertEqual(replayed["job_count"], 38)
        self.assertRegex(
            replayed["storage_probe_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(
            replayed["failure_lifecycle_probe_sha256"], r"^[0-9a-f]{64}$"
        )

    def test_current_facts_missing_storage_probe_fail_closed(self) -> None:
        """新 P0 不能用缺少运行目录写探针的旧结构生成通过收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            contract = self._contract(root)
            create_job_rehearsal_receipt(
                root,
                contract=contract,
                preflight_campaign_id="preflight-0151",
            )
            facts_path = root / "facts.json"
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            facts["probes"].pop("storage")
            facts["runtime_identity_sha256"] = receipt._runtime_identity(facts)
            self._rewrite(facts_path, facts)
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError,
                "probes字段不闭合",
            ):
                receipt.build_receipt(root, "facts.json")

    def test_current_facts_require_complete_failure_lifecycle_probe(self) -> None:
        """新 P0 必须拒绝缺项、联网、残留或父监督器未闭合的联合事实。"""

        mutations = (
            (
                "missing",
                lambda probe: probe.pop("attempt_count"),
                "字段不闭合",
            ),
            (
                "live-request",
                lambda probe: probe.__setitem__("live_request_count", 1),
                "身份、重试或零网络事实非法",
            ),
            (
                "not-cleaned",
                lambda probe: probe.__setitem__("cleanup_verified", False),
                "身份、重试或零网络事实非法",
            ),
            (
                "parent-running",
                lambda probe: probe["parent_supervisor"].__setitem__(
                    "run_state", "running"
                ),
                "父监督器终态非法",
            ),
            (
                "marker-tampered",
                lambda probe: probe["archives"][0]["marker"].__setitem__(
                    "live_request_count", 1
                ),
                "第 1 次归档事实非法",
            ),
        )
        for label, mutate, expected_error in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                root.chmod(0o700)
                create_job_rehearsal_receipt(
                    root,
                    contract=self._contract(root),
                    preflight_campaign_id="preflight-0151",
                )
                facts_path = root / "facts.json"
                facts = json.loads(facts_path.read_text(encoding="utf-8"))
                mutate(facts["probes"]["failure_lifecycle"])
                facts["runtime_identity_sha256"] = receipt._runtime_identity(facts)
                self._rewrite(facts_path, facts)
                with self.assertRaisesRegex(
                    receipt.JobRehearsalReceiptError,
                    expected_error,
                ):
                    receipt.build_receipt(root, "facts.json")

    def test_formal_rejects_receipt_without_failure_lifecycle_digest(self) -> None:
        """历史结构可重放，但不能绕过当前 Formal 的联合 P0 门禁。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            contract = self._contract(root)
            path = create_job_rehearsal_receipt(
                root,
                contract=contract,
                preflight_campaign_id="preflight-0151",
            )
            replayed = receipt.replay(root, path.name)
            replayed.pop("failure_lifecycle_probe_sha256")
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError,
                "Formal 所需完整 Job 演练收据未通过",
            ):
                receipt.assert_formal_compatible(replayed, contract)

    def test_storage_only_legacy_receipt_replays_but_is_not_formal(self) -> None:
        """旧 storage-only 收据保持可审计，当前 Formal 必须拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            contract = self._contract(root)
            create_job_rehearsal_receipt(
                root,
                contract=contract,
                preflight_campaign_id="preflight-0151",
            )
            facts_path = root / "facts.json"
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            facts["probes"].pop("failure_lifecycle")
            facts["runtime_identity_sha256"] = receipt._runtime_identity(facts)
            self._rewrite(facts_path, facts)
            legacy = receipt.build_receipt(
                root,
                "facts.json",
                allow_collector_drift=True,
            )
            legacy_path = root / "storage-only-receipt.json"
            legacy_path.write_bytes(receipt._canonical(legacy))
            legacy_path.chmod(0o600)

            replayed = receipt.replay(root, legacy_path.name)
            self.assertRegex(replayed["storage_probe_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("failure_lifecycle_probe_sha256", replayed)
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError,
                "Formal 所需完整 Job 演练收据未通过",
            ):
                receipt.assert_formal_compatible(replayed, contract)

    def test_failed_job_duration_is_part_of_replayable_contract(self) -> None:
        """失败 Job 的耗时字段必须能被封存和独立重放。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            create_job_rehearsal_receipt(
                root,
                contract=self._contract(root),
                preflight_campaign_id="preflight-0151",
            )
            facts_path = root / "facts.json"
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            failed = facts["jobs"][0]
            failed.update(
                {
                    "status": "failed",
                    "steps": [],
                    "error": "synthetic failure",
                    "duration_seconds": 0.125,
                }
            )
            facts["summary"].update(
                {
                    "passed_job_count": facts["summary"]["job_count"] - 1,
                    "status": "failed",
                    "failed_job_ids": [failed["id"]],
                }
            )
            facts["runtime_identity_sha256"] = receipt._runtime_identity(facts)
            self._rewrite(facts_path, facts)
            failed_receipt = receipt.finalize(
                root,
                "facts.json",
                "failed-receipt.json",
            )
            replayed = receipt.replay(root, "failed-receipt.json")

        self.assertEqual(failed_receipt["status"], "failed")
        self.assertEqual(failed_receipt["failed_job_ids"], [failed["id"]])
        self.assertEqual(replayed["status"], "failed")

    def test_checkpoint_context_keeps_storage_schema(self) -> None:
        """运行上下文不得覆盖增量 checkpoint 存储器的 schema 字段。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            context = receipt._checkpoint_context(
                campaign_id="preflight-0151",
                contract=self._contract(root),
                component_summary={},
                plan={"plan_sha256": "a" * 64},
                previous_source=None,
            )
            self.assertEqual(
                context["context_schema_version"],
                receipt.CHECKPOINT_CONTEXT_SCHEMA,
            )
            store = incremental_recovery.CheckpointStore(root)
            record = store.append(
                {
                    **context,
                    "item_id": "job-a",
                    "status": "passed",
                    "previous_checkpoint_sha256": None,
                }
            )
        self.assertEqual(record["schema_version"], incremental_recovery.CHECKPOINT_SCHEMA)

    def test_missing_job_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            create_job_rehearsal_receipt(
                root,
                contract=self._contract(root),
                preflight_campaign_id="preflight-0151",
            )
            facts_path = root / "facts.json"
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            facts["jobs"].pop()
            self._rewrite(facts_path, facts)
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError, "数量不完整"
            ):
                receipt.build_receipt(root, "facts.json")

    def test_runtime_or_tool_contract_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            contract = self._contract(root)
            path = create_job_rehearsal_receipt(
                root,
                contract=contract,
                preflight_campaign_id="preflight-0151",
            )
            replayed = receipt.replay(root, path.name)
            for field, value in (
                ("runtime_image", f"capture-runtime@sha256:{'5' * 64}"),
                ("capture_container", "other-capture"),
                ("capture_codex_bin", "/opt/codex-0.151.0/bin/other"),
            ):
                with self.subTest(field=field):
                    changed = copy.deepcopy(contract)
                    changed["configuration"][field] = value
                    with self.assertRaisesRegex(
                        receipt.JobRehearsalReceiptError, "执行合同"
                    ):
                        receipt.assert_formal_compatible(replayed, changed)

    def test_c2pa_job_identity_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario_path = Path(receipt.__file__).with_name(
                "codex_upgrade_scenarios_0_151_0.json"
            )
            scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
            job = next(
                item
                for item in scenario["capture_jobs"]
                if item["id"] == "official-relay-file-upload-c2pa-positive"
            )
            job["steps"][0]["environment"]["SCENARIO_JOB_ID"] = (
                "official-relay-file-upload-c2pa-negative"
            )
            configuration = self._contract(root)["configuration"]
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError, "C2PA"
            ):
                receipt.build_execution_contract(
                    target_version="0.151.0",
                    target_sha256="1" * 64,
                    target_package_sha256="2" * 64,
                    target_code_mode_host_sha256="3" * 64,
                    suite="full",
                    tool_files_sha256=codex_upgrade._tool_identity()[
                        "files_sha256"
                    ],
                    configuration=configuration,
                    target_scenario=scenario,
                    extra_jobs=None,
                )

    def test_schema_matches_runtime_version(self) -> None:
        schema = json.loads(
            Path(receipt.__file__)
            .with_name("codex_upgrade_job_rehearsal_receipt.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            receipt.RECEIPT_SCHEMA,
        )
        self.assertEqual(
            schema["$defs"]["executionContract"]["properties"][
                "schema_version"
            ]["const"],
            receipt.EXECUTION_CONTRACT_SCHEMA,
        )

    def test_incremental_noop_skips_runtime_probes_and_is_not_formal_pass(self) -> None:
        """空执行计划在 Docker／二进制／环境探针前短路。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            source_root = root / "source"
            source_root.mkdir(mode=0o700)
            contract = self._contract(source_root)
            source_receipt = create_job_rehearsal_receipt(
                source_root,
                contract=contract,
                preflight_campaign_id="preflight-0151",
            )
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            (campaign_dir / "campaign.json").write_text("{}\n", encoding="utf-8")
            (campaign_dir / "campaign.json").chmod(0o600)
            scenario_name = "scenarios.json"
            scenario_source = Path(receipt.__file__).with_name(
                "codex_upgrade_scenarios_0_151_0.json"
            )
            (campaign_dir / scenario_name).write_bytes(
                scenario_source.read_bytes()
            )
            (campaign_dir / scenario_name).chmod(0o600)
            configuration = dict(contract["configuration"])
            manifest = {
                "campaign_id": "preflight-0151",
                "campaign_mode": "preflight_only",
                "target_version": "0.151.0",
                "target_sha256": "1" * 64,
                "suite": "full",
                "official_identity": {
                    "package": {
                        "asset_sha256": "2" * 64,
                        "code_mode_host_sha256": "3" * 64,
                    }
                },
                "inputs": {
                    "target_discovery_scenarios": {"path": scenario_name}
                },
                "configuration": configuration,
                "tool_identity": {
                    "files_sha256": contract["tool_files_sha256"]
                },
            }
            jobs = [
                codex_upgrade.Job(
                    job_id=job_id,
                    phase=contract["job_phases"][job_id],
                    suites=("full",),
                    description=job_id,
                    steps=(
                        {
                            "argv": ["true"],
                            "environment": {},
                            "timeout": 1,
                        },
                    ),
                    evidence_roots=(),
                    covers=(),
                )
                for job_id in contract["job_ids"]
            ]
            plan_core = {
                "contract_sha256": receipt.execution_contract_sha256(contract),
                "execute_job_ids": [],
                "reused_job_ids": list(contract["job_ids"]),
                "failed_job_ids": [],
                "changed_components": [],
                "reasons": {
                    job_id: "unchanged_dependency"
                    for job_id in contract["job_ids"]
                },
            }
            plan = {
                "schema_version": incremental_recovery.SCHEMA_VERSION,
                **plan_core,
                "plan_sha256": incremental_recovery.digest(plan_core),
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_jobs",
                    side_effect=[jobs[:29], jobs[29:]],
                ),
                mock.patch.object(
                    receipt.platform,
                    "machine",
                    return_value="aarch64",
                ),
                mock.patch.object(
                    receipt.platform,
                    "system",
                    return_value="linux",
                ),
                mock.patch.object(
                    receipt,
                    "_select_rehearsal_plan",
                    return_value=plan,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_execution_tree",
                    side_effect=AssertionError("_verify_execution_tree"),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_official_binaries",
                    side_effect=AssertionError("_verify_official_binaries"),
                ),
                mock.patch.object(
                    receipt,
                    "_container_tool_tree",
                    side_effect=AssertionError("_container_tool_tree"),
                ),
                mock.patch.object(
                    receipt,
                    "_container_facts",
                    side_effect=AssertionError("_container_facts"),
                ),
                mock.patch.object(
                    receipt,
                    "_host_dependencies",
                    side_effect=AssertionError("_host_dependencies"),
                ),
                mock.patch.object(
                    receipt,
                    "_container_dependencies",
                    side_effect=AssertionError("_container_dependencies"),
                ),
                mock.patch.object(
                    receipt,
                    "_bwrap_probe",
                    side_effect=AssertionError("_bwrap_probe"),
                ),
                mock.patch.object(
                    receipt,
                    "_zstd_probe",
                    side_effect=AssertionError("_zstd_probe"),
                ),
            ):
                facts = receipt.collect_facts(
                    campaign_dir,
                    previous_receipt=source_receipt,
                    previous_receipt_root=source_root,
                    rerun_failed=True,
                    checkpoint_root=root / "checkpoints",
                )
            self.assertEqual(facts["status"], receipt.INCREMENTAL_NOOP_STATUS)
            noop = facts["incremental_noop"]
            self.assertEqual(noop["execute_job_ids"], [])
            self.assertEqual(noop["reused_job_ids"], contract["job_ids"])
            self.assertEqual(noop["affected_job_ids"], [])
            self.assertEqual(noop["scanned_bytes"], 0)
            self.assertEqual(noop["live_request_count"], 0)
            self.assertFalse(noop["new_pass_fact"])
            receipt_path = root / "noop-facts.json"
            receipt_path.write_bytes(receipt._canonical(facts))
            receipt_path.chmod(0o600)
            built = receipt.build_receipt(root, receipt_path.name)
            self.assertEqual(built["status"], receipt.INCREMENTAL_NOOP_STATUS)
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError, "不是完整 Job 演练"
            ):
                receipt.assert_formal_compatible(built, contract)
            source = receipt.assert_recovery_compatible(built, contract)
            self.assertEqual(source["status"], "passed")
            self.assertRegex(source["runtime_identity_sha256"], r"^[0-9a-f]{64}$")
            self.assertFalse(
                codex_upgrade._manifest_allows_incremental_noop_rehearsal(
                    {"campaign_mode": "formal"}
                )
            )
            self.assertTrue(
                codex_upgrade._manifest_allows_incremental_noop_rehearsal(
                    {
                        "campaign_mode": "formal",
                        "predecessor": {
                            "reason": "sealed_stage_control_recovery"
                        },
                    }
                )
            )
            verified_source = codex_upgrade._assert_job_rehearsal_compatible(
                built,
                contract,
                allow_incremental_noop=True,
            )
            self.assertEqual(
                verified_source["runtime_identity_sha256"],
                source["runtime_identity_sha256"],
            )

            noop_receipt_path = root / "noop-receipt.json"
            noop_receipt_path.write_bytes(receipt._canonical(built))
            noop_receipt_path.chmod(0o600)
            control = codex_upgrade._job_rehearsal_control_from_receipt(
                root,
                noop_receipt_path,
                contract,
                allow_incremental_noop=True,
            )
            self.assertEqual(
                control["runtime_identity_sha256"],
                source["runtime_identity_sha256"],
            )
            self.assertEqual(
                control["preflight_campaign_id"],
                "preflight-0151",
            )

            source_receipt_path = source_root / source_receipt.name
            original = source_receipt_path.read_bytes()
            source_receipt_path.write_bytes(original + b"\n")
            with self.assertRaisesRegex(
                receipt.JobRehearsalReceiptError, "原始通过收据漂移"
            ):
                receipt.assert_recovery_compatible(built, contract)


if __name__ == "__main__":
    unittest.main()
