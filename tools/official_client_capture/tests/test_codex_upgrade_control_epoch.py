"""原 Campaign control epoch 的离线正反门禁。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade


class CodexUpgradeControlEpochTest(unittest.TestCase):
    def test_official_epoch_allows_only_deterministic_discovery_cache(self) -> None:
        """唯一发现缓存不是草案；其他分类输出仍必须失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            classification = campaign / "classification"
            discovery = classification / "discovery"
            discovery.mkdir(parents=True)
            (discovery / "baseline-to-target-official.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            manifest = {"campaign_mode": "formal"}
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_successor_reason_ancestor",
                    return_value=None,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "祖先尚未使用",
                ),
            ):
                codex_upgrade._official_sealed_control_epoch_source_context(
                    campaign,
                    manifest,
                )

            draft = classification / "draft/revision"
            draft.mkdir(parents=True)
            (draft / "draft.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "分类草案或结果",
            ):
                codex_upgrade._official_sealed_control_epoch_source_context(
                    campaign,
                    manifest,
                )

    def test_official_epoch_replay_allows_postpublication_outputs(self) -> None:
        """已发布 epoch 的历史重放不得再次套用发布前为空门禁。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            official_path = campaign / "official" / "result.json"
            official_path.parent.mkdir(parents=True)
            official_path.write_text("{}\n", encoding="utf-8")
            (campaign / "classification" / "draft" / "revision").mkdir(
                parents=True
            )
            (campaign / "candidates" / "candidate-a").mkdir(parents=True)
            manifest = {
                "campaign_mode": "formal",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                        "sha256": "5" * 64,
                    }
                },
            }
            previous_controls = {
                "upgrade_timing": {"ledger_dir": "/ledger/recovery"},
                "arm64_environment": {"evidence_root": "/p0/recovery"},
                "job_rehearsal": {"evidence_root": "/noop/recovery"},
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_successor_reason_ancestor",
                    return_value={"reason": "sealed_stage_control_recovery"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_stage_result",
                    return_value={
                        "status": "complete",
                        "package_digest": "7" * 64,
                    },
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_sealed_stage_recovery_context",
                    return_value={"effective_controls": previous_controls},
                ),
            ):
                source = (
                    codex_upgrade._official_sealed_control_epoch_source_context(
                        campaign,
                        manifest,
                        _enforce_prepublication_boundary=False,
                    )
                )
            self.assertEqual(source["mode"], "official_sealed")
            self.assertEqual(source["previous_controls"], previous_controls)

    def _offline_draft_approval_fixture(
        self,
        root: Path,
        *,
        revision: str = "20260905T013527Z-610719308",
    ) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object]]:
        """构造一份在 VC-2 截止前已写入的唯一分类草案。"""

        campaign = root / "campaign"
        draft = campaign / "classification" / "draft" / revision
        draft.mkdir(parents=True)
        for name in (
            "target-rules.json",
            "rule-migration.json",
            "scenarios.json",
            "profile.json",
            "assertion-profile.json",
        ):
            (draft / name).write_text("{}\n", encoding="utf-8")
        (draft / "draft.json").write_text(
            json.dumps(
                {
                    "status": "draft",
                    "revision": revision,
                    "path": str(draft),
                }
            ),
            encoding="utf-8",
        )
        manifest = {
            "campaign_id": "formal-official-classification",
            "baseline_version": "0.149.1",
            "target_version": "0.151.0",
            "campaign_purpose": "production_replacement",
        }
        epoch = {
            "source": {"mode": "official_sealed"},
            "runtime_repair": {"path": "control-epochs/runtime-repair.json"},
            "successor_controls": {"upgrade_timing": {"marker": "timing"}},
            "boundary": codex_upgrade._control_epoch_zero_boundary(),
        }
        frozen_summary = {
            "status": "active",
            "baseline_version": "0.149.1",
            "target_version": "0.151.0",
            "campaign_purpose": "production_replacement",
            "active_phase": "VC-2",
            "head_sequence": 3,
            "head_sha256": "a" * 64,
            "total_deadline_at_utc": "2099-09-05T04:35:48+00:00",
            "stage_deadline_at_utc": "2026-09-05T01:51:10+00:00",
            "total_live_request_count": 0,
        }
        current_summary = {
            **frozen_summary,
            "status": "stop_required",
        }
        return campaign, manifest, epoch, {
            "frozen": frozen_summary,
            "current": current_summary,
        }

    def test_offline_approval_accepts_draft_created_before_vc2_deadline(
        self,
    ) -> None:
        """阶段截止前已生成的草案可在总截止内离线批准。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, epoch, timing = self._offline_draft_approval_fixture(
                Path(directory)
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_sealed_stage_timing_checkpoint",
                    return_value=(Path("/ledger"), {"summary": timing["frozen"]}),
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "inspect_ledger",
                    return_value=timing["current"],
                ),
            ):
                authorization = (
                    codex_upgrade._verify_stopped_classification_draft_approval(
                        campaign,
                        manifest,
                        epoch,
                    )
                )
            self.assertEqual(
                authorization["draft_revision"],
                "20260905T013527Z-610719308",
            )

    def test_offline_approval_rejects_draft_created_after_vc2_deadline(
        self,
    ) -> None:
        """阶段截止后才生成的草案不能借离线批准放行。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, epoch, timing = self._offline_draft_approval_fixture(
                Path(directory),
                revision="20260905T015200Z-000000001",
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_sealed_stage_timing_checkpoint",
                    return_value=(Path("/ledger"), {"summary": timing["frozen"]}),
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "inspect_ledger",
                    return_value=timing["current"],
                ),
                self.assertRaisesRegex(codex_upgrade.ConfigurationError, "截止前"),
            ):
                codex_upgrade._verify_stopped_classification_draft_approval(
                    campaign,
                    manifest,
                    epoch,
                )

    def test_offline_approval_rejects_expired_total_deadline_or_live_request(
        self,
    ) -> None:
        """总截止已过或 live 请求非零时必须失败关闭。"""

        for field, value, message in (
            ("total_deadline_at_utc", "2020-01-01T00:00:00+00:00", "总截止"),
            ("total_live_request_count", 1, "live"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                campaign, manifest, epoch, timing = (
                    self._offline_draft_approval_fixture(Path(directory))
                )
                timing["current"][field] = value
                if field == "total_deadline_at_utc":
                    timing["frozen"][field] = value
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_sealed_stage_timing_checkpoint",
                        return_value=(
                            Path("/ledger"),
                            {"summary": timing["frozen"]},
                        ),
                    ),
                    mock.patch.object(
                        codex_upgrade.codex_upgrade_timing_ledger,
                        "inspect_ledger",
                        return_value=timing["current"],
                    ),
                    self.assertRaisesRegex(codex_upgrade.ConfigurationError, message),
                ):
                    codex_upgrade._verify_stopped_classification_draft_approval(
                        campaign,
                        manifest,
                        epoch,
                    )

    def test_offline_approval_rejects_existing_result_or_candidate(self) -> None:
        """已有批准结果或 Candidate 时不得重放草案批准。"""

        for output, message in (
            ("result", "分类结果"),
            ("approved", "批准目录"),
            ("candidate", "Candidate"),
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                campaign, manifest, epoch, timing = (
                    self._offline_draft_approval_fixture(Path(directory))
                )
                if output == "result":
                    (campaign / "classification" / "result.json").write_text(
                        "{}\n",
                        encoding="utf-8",
                    )
                elif output == "approved":
                    (campaign / "classification" / "approved").mkdir()
                else:
                    (campaign / "candidates" / "candidate-a").mkdir(parents=True)
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_sealed_stage_timing_checkpoint",
                        return_value=(
                            Path("/ledger"),
                            {"summary": timing["frozen"]},
                        ),
                    ),
                    mock.patch.object(
                        codex_upgrade.codex_upgrade_timing_ledger,
                        "inspect_ledger",
                        return_value=timing["current"],
                    ),
                    self.assertRaisesRegex(codex_upgrade.ConfigurationError, message),
                ):
                    codex_upgrade._verify_stopped_classification_draft_approval(
                        campaign,
                        manifest,
                        epoch,
                    )

    def _fixture(
        self,
        root: Path,
        *,
        final_scope: bool = False,
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        campaign = root / "campaign"
        epoch_root = campaign / codex_upgrade.CONTROL_EPOCH_DIRECTORY
        epoch_root.mkdir(parents=True)
        (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
        old_timing = {
            "ledger_dir": "/control/old",
            "upgrade_id": "old-ledger",
        }
        previous_controls = {
            "upgrade_timing": old_timing,
            "arm64_environment": {"marker": "old-arm"},
            "job_rehearsal": {"marker": "old-rehearsal"},
        }
        successor_controls = {
            "upgrade_timing": {
                "ledger_dir": "/control/new",
                "upgrade_id": "new-ledger",
            },
            "arm64_environment": {"marker": "new-arm"},
            "job_rehearsal": {"marker": "new-rehearsal"},
        }
        manifest: dict[str, object] = {
            "campaign_id": "formal-control-refresh",
            "campaign_mode": "formal",
            "campaign_purpose": "production_replacement",
            "baseline_version": "0.149.1",
            "target_version": "0.151.0",
            "control_receipts": previous_controls,
        }
        execute_job_ids = ["candidate-compact-mitm", "candidate-core-mitm"]
        reused_job_ids = (
            [
                "candidate-compact-direct",
                "candidate-core-direct",
                "candidate-lite-direct",
                "candidate-lite-mitm",
                "official-compact",
                "official-core",
                "official-lite",
            ]
            if final_scope
            else []
        )
        source = {
            "candidate_id": "candidate-a",
            "attempt_id": "attempt-a",
            "attempt_digest": "1" * 64,
            "candidate_identity_sha256": "2" * 64,
            "source_transition": {"path": "transition.json", "sha256": "3" * 64},
            "recovery_scope_sha256": "4" * 64,
            "planned_job_ids": sorted(execute_job_ids + reused_job_ids),
            "execute_job_ids": execute_job_ids,
            "reused_job_ids": sorted(reused_job_ids),
            "failed_job_ids": execute_job_ids,
            "pending_job_ids": [],
            "production_paths": ["run_sub2api_openai_mitm_matrix.sh"],
            "target_scenario": {"path": "inputs/target.json", "sha256": "5" * 64},
        }
        invariants = {
            "tool_files_sha256": "6" * 64,
            "tool_production_sha256": "7" * 64,
            "compose_sha256": "8" * 64,
            "required_active_phase": "VC-4",
        }
        stop = {
            "ledger_dir": "/control/old",
            "receipt": {"path": "receipts/stop.json", "sha256": "9" * 64, "bytes": 1},
            "upgrade_id": "old-ledger",
            "active_phase": "VC-4",
            "head_sequence": 10,
            "head_sha256": "a" * 64,
            "total_elapsed_seconds": 100,
            "total_live_request_count": 0,
            "total_deadline_at_utc": "2026-09-03T17:59:41+00:00",
        }
        stop_summary = {
            **{key: stop[key] for key in (
                "upgrade_id",
                "active_phase",
                "head_sequence",
                "head_sha256",
                "total_elapsed_seconds",
                "total_live_request_count",
                "total_deadline_at_utc",
            )},
            "status": "stopped",
        }
        successor_summary = {
            "status": "active",
            "active_phase": "VC-4",
            "total_elapsed_seconds": 20,
            "total_live_request_count": 0,
            "total_deadline_at_utc": "2026-09-03T17:50:00+00:00",
        }
        budget = {
            "predecessor_total_deadline_at_utc": stop_summary["total_deadline_at_utc"],
            "successor_total_deadline_at_utc": successor_summary["total_deadline_at_utc"],
            "predecessor_elapsed_seconds": 100,
            "successor_elapsed_seconds": 20,
            "cumulative_elapsed_seconds": 120,
            "predecessor_live_request_count": 0,
            "successor_live_request_count": 0,
            "cumulative_live_request_count": 0,
        }
        core = {
            "schema_version": codex_upgrade.CONTROL_EPOCH_SCHEMA,
            "status": "active",
            "epoch_index": 1,
            "created_at_utc": "2026-09-03T15:00:00Z",
            "campaign_id": manifest["campaign_id"],
            "campaign_manifest_sha256": codex_upgrade.file_sha256(campaign / "campaign.json"),
            "previous_epoch_sha256": None,
            "previous_controls": previous_controls,
            "stop_checkpoint": stop,
            "successor_controls": successor_controls,
            "source": source,
            "invariants": invariants,
            "boundary": {
                "reservation_count": 0,
                "attempt_count": 0,
                "checkpoint_count": 0,
                "live_request_count": 0,
                "scanned_bytes": 0,
            },
            "budget": budget,
        }
        payload = {**core, "receipt_sha256": codex_upgrade._fingerprint(core)}
        path = codex_upgrade._control_epoch_path(campaign)
        path.write_text(json.dumps(payload), encoding="utf-8")
        context = {
            "source": source,
            "invariants": invariants,
            "stop": stop,
            "stop_summary": stop_summary,
            "successor_summary": successor_summary,
            "successor_controls": successor_controls,
        }
        return campaign, manifest, context

    def _tool_identity_fixture(
        self,
        *,
        orchestrator_sha256: str,
        producer_sha256: str = "c" * 64,
    ) -> dict[str, object]:
        """构造同时包含混合编排器和真实 producer 的工具身份。"""

        entries = [
            {
                "path": "capture.py",
                "sha256": producer_sha256,
            },
            {
                "path": "codex_upgrade.py",
                "sha256": orchestrator_sha256,
            },
        ]
        components = codex_upgrade._tool_component_identities(entries)
        return {
            "git_commit": None,
            "entry_count": len(entries),
            "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
            "entries": entries,
            "components": components["components"],
            "component_identity_sha256": codex_upgrade._fingerprint(components),
            **codex_upgrade._tool_identity_sides(entries),
        }

    def _load(
        self,
        campaign: Path,
        manifest: dict[str, object],
        context: dict[str, object],
        *,
        current_invariants: dict[str, object] | None = None,
        allow_unrepaired_tool_drift: bool = False,
        allowed_production_paths: set[str] | None = None,
    ) -> dict[str, object] | None:
        active_invariants = current_invariants or context["invariants"]
        with (
            mock.patch.object(
                codex_upgrade,
                "_control_epoch_source_context",
                return_value=context["source"],
            ) as source_context,
            mock.patch.object(
                codex_upgrade,
                "_tool_identity",
                return_value={"files_sha256": active_invariants["tool_files_sha256"]},
            ),
            mock.patch.object(
                codex_upgrade,
                "_control_epoch_invariants",
                return_value=active_invariants,
            ),
            mock.patch.object(
                codex_upgrade,
                "_control_epoch_failed_scope_production_paths",
                return_value=allowed_production_paths or set(),
            ),
            mock.patch.object(
                codex_upgrade,
                "_control_epoch_stop_checkpoint",
                return_value=(context["stop"], context["stop_summary"]),
            ),
            mock.patch.object(codex_upgrade, "_verify_control_receipts"),
            mock.patch.object(
                codex_upgrade,
                "_sealed_stage_timing_checkpoint",
                return_value=(Path("/control/new"), {"summary": context["successor_summary"]}),
            ),
        ):
            result = codex_upgrade._load_control_epoch_receipt(
                campaign,
                manifest,
                _allow_unrepaired_tool_drift=allow_unrepaired_tool_drift,
            )
        self.assertFalse(
            source_context.call_args.kwargs["_enforce_prepublication_boundary"]
        )
        return result

    def test_historical_control_replay_enables_historical_ledger_heads(self) -> None:
        """历史清单重放必须独立启用历史 Ledger-head 模式。"""

        campaign = Path("/control/campaign")
        manifest: dict[str, object] = {}
        with (
            mock.patch.object(
                codex_upgrade,
                "_load_control_epoch_receipt",
                side_effect=codex_upgrade.ConfigurationError("stop-after-load"),
            ) as loader,
            self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "stop-after-load",
            ),
        ):
            codex_upgrade._verify_control_receipts(
                campaign,
                manifest,
                require_active=False,
                _historical_manifest_controls=True,
            )
        loader.assert_called_once_with(
            campaign,
            manifest,
            _allow_unrepaired_tool_drift=True,
            _historical_ledger_heads=True,
        )

    def test_historical_replacement_loader_preserves_vc0_stop_phase(self) -> None:
        """历史 replacement 首个 epoch 不得被后继 VC-4 规则追溯改判。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            manifest["predecessor"] = {
                "reason": "candidate_recovery_control_replacement",
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_source_context",
                    return_value=context["source"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value={
                        "files_sha256": context["invariants"][
                            "tool_files_sha256"
                        ]
                    },
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_invariants",
                    return_value=context["invariants"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_failed_scope_production_paths",
                    return_value=set(),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_stop_checkpoint",
                    side_effect=codex_upgrade.ConfigurationError(
                        "stop-after-phase-selection"
                    ),
                ) as stop_checkpoint,
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "stop-after-phase-selection",
                ),
            ):
                codex_upgrade._load_control_epoch_receipt(
                    campaign,
                    manifest,
                    _allow_unrepaired_tool_drift=True,
                    _historical_ledger_heads=True,
                )
            self.assertIsNone(
                stop_checkpoint.call_args.kwargs["required_phase"]
            )
            self.assertTrue(
                stop_checkpoint.call_args.kwargs["_historical_ledger_heads"]
            )

    def _add_runtime_repair(
        self,
        campaign: Path,
        manifest: dict[str, object],
        context: dict[str, object],
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        epoch_path = codex_upgrade._control_epoch_path(campaign)
        current_invariants = {
            **context["invariants"],
            "tool_files_sha256": "b" * 64,
        }
        controls = json.loads(json.dumps(context["successor_controls"]))
        controls["job_rehearsal"] = {"marker": "current-rehearsal"}
        core = {
            "schema_version": codex_upgrade.CONTROL_EPOCH_RUNTIME_REPAIR_SCHEMA,
            "status": "active",
            "repair_index": 1,
            "created_at_utc": "2026-09-03T16:00:00Z",
            "campaign_id": manifest["campaign_id"],
            "campaign_manifest_sha256": codex_upgrade.file_sha256(
                campaign / "campaign.json"
            ),
            "control_epoch": {
                "path": epoch_path.relative_to(campaign).as_posix(),
                "sha256": codex_upgrade.file_sha256(epoch_path),
                "bytes": epoch_path.stat().st_size,
                "receipt_sha256": json.loads(
                    epoch_path.read_text(encoding="utf-8")
                )["receipt_sha256"],
            },
            "previous_tool_files_sha256": context["invariants"][
                "tool_files_sha256"
            ],
            "successor_controls": controls,
            "source": context["source"],
            "invariants": current_invariants,
            "boundary": codex_upgrade._control_epoch_zero_boundary(),
            "budget": json.loads(
                epoch_path.read_text(encoding="utf-8")
            )["budget"],
        }
        payload = {**core, "receipt_sha256": codex_upgrade._fingerprint(core)}
        path = codex_upgrade._control_epoch_runtime_repair_path(campaign)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path, payload, current_invariants

    def _add_runtime_repair_amendment(
        self,
        campaign: Path,
        manifest: dict[str, object],
        context: dict[str, object],
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        first_path, first, first_invariants = self._add_runtime_repair(
            campaign,
            manifest,
            context,
        )
        current_invariants = {
            **first_invariants,
            "tool_files_sha256": "c" * 64,
        }
        controls = json.loads(json.dumps(first["successor_controls"]))
        controls["job_rehearsal"] = {"marker": "amended-rehearsal"}
        core = {
            "schema_version": codex_upgrade.CONTROL_EPOCH_RUNTIME_REPAIR_SCHEMA,
            "status": "active",
            "repair_index": 2,
            "created_at_utc": "2026-09-03T16:10:00Z",
            "campaign_id": manifest["campaign_id"],
            "campaign_manifest_sha256": codex_upgrade.file_sha256(
                campaign / "campaign.json"
            ),
            "control_epoch": first["control_epoch"],
            "previous_runtime_repair": {
                "path": first_path.relative_to(campaign).as_posix(),
                "sha256": codex_upgrade.file_sha256(first_path),
                "bytes": first_path.stat().st_size,
                "receipt_sha256": first["receipt_sha256"],
            },
            "previous_tool_files_sha256": first_invariants[
                "tool_files_sha256"
            ],
            "successor_controls": controls,
            "source": context["source"],
            "invariants": current_invariants,
            "boundary": codex_upgrade._control_epoch_zero_boundary(),
            "budget": first["budget"],
        }
        payload = {**core, "receipt_sha256": codex_upgrade._fingerprint(core)}
        path = codex_upgrade._control_epoch_runtime_repair_amendment_path(
            campaign
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path, payload, current_invariants

    def test_loads_one_signed_epoch_and_rejects_later_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            self.assertEqual(self._load(campaign, manifest, context)["epoch_index"], 1)

            path = codex_upgrade._control_epoch_path(campaign)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["budget"]["successor_total_deadline_at_utc"] = "2026-09-03T18:10:00+00:00"
            core = dict(payload)
            core.pop("receipt_sha256")
            payload["receipt_sha256"] = codex_upgrade._fingerprint(core)
            path.write_text(json.dumps(payload), encoding="utf-8")
            context["successor_summary"]["total_deadline_at_utc"] = "2026-09-03T18:10:00+00:00"
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "预算、阶段"):
                self._load(campaign, manifest, context)

    def test_loads_single_official_epoch_at_vc2_with_bounded_new_deadline(self) -> None:
        """已用过 sealed successor 后只允许一份 VC-2 原地控制 epoch。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            source = {
                "mode": "official_sealed",
                "official_stage": {
                    "path": "official/result.json",
                    "sha256": "1" * 64,
                    "package_digest": "2" * 64,
                },
                "previous_sealed_stage_recovery": {
                    "campaign_dir": "/campaign/prior",
                    "campaign_id": "prior-recovery",
                    "campaign_manifest_sha256": "3" * 64,
                    "reason": "sealed_stage_control_recovery",
                },
                "previous_controls": manifest["control_receipts"],
                "target_scenario": {
                    "path": "inputs/target.json",
                    "sha256": "4" * 64,
                },
            }
            context["source"] = source
            context["invariants"]["required_active_phase"] = "VC-2"
            context["stop"]["active_phase"] = "VC-2"
            context["stop_summary"]["active_phase"] = "VC-2"
            context["successor_summary"]["active_phase"] = "VC-2"
            context["successor_summary"]["total_deadline_at_utc"] = (
                "2026-09-03T19:00:00+00:00"
            )
            path = codex_upgrade._control_epoch_path(campaign)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["source"] = source
            payload["invariants"] = context["invariants"]
            payload["stop_checkpoint"] = context["stop"]
            payload["budget"]["successor_total_deadline_at_utc"] = (
                context["successor_summary"]["total_deadline_at_utc"]
            )
            core = dict(payload)
            core.pop("receipt_sha256")
            payload["receipt_sha256"] = codex_upgrade._fingerprint(core)
            path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(
                self._load(campaign, manifest, context)["source"]["mode"],
                "official_sealed",
            )
            current_invariants = {
                **context["invariants"],
                "tool_files_sha256": "b" * 64,
            }
            self.assertEqual(
                self._load(
                    campaign,
                    manifest,
                    context,
                    current_invariants=current_invariants,
                )["source"]["mode"],
                "official_sealed",
            )
            codex_upgrade._control_epoch_path(campaign, 2).write_text(
                "{}", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "只允许一份",
            ):
                self._load(campaign, manifest, context)

    def test_classification_accepts_only_registered_drift_after_official_epoch(
        self,
    ) -> None:
        """分类只可承接 official epoch 后已登记的混合评估文件变化。"""

        expected = self._tool_identity_fixture(orchestrator_sha256="a" * 64)
        current = self._tool_identity_fixture(orchestrator_sha256="b" * 64)
        configuration = {
            "live_attestation_compose_dir": "/compose",
            "live_attestation_compose_files": "-f compose.yml",
        }
        manifest = {
            "tool_identity": expected,
            "configuration": configuration,
        }
        epoch = {
            "source": {"mode": "official_sealed"},
            "invariants": {
                "tool_files_sha256": expected["files_sha256"],
                "tool_production_sha256": (
                    codex_upgrade._tool_identity_side_digest_excluding(
                        expected,
                        "production",
                        codex_upgrade._PHASE_EVALUATION_HYBRID_FILES,
                    )
                ),
                "compose_sha256": codex_upgrade._fingerprint(configuration),
                "required_active_phase": "VC-2",
            },
        }
        self.assertTrue(
            codex_upgrade._official_epoch_allows_classification_tool_drift(
                manifest,
                current,
                expected,
                epoch,
                operation="classify",
            )
        )
        producer_changed = self._tool_identity_fixture(
            orchestrator_sha256="b" * 64,
            producer_sha256="d" * 64,
        )
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "超出已批准",
        ):
            codex_upgrade._official_epoch_allows_classification_tool_drift(
                manifest,
                producer_changed,
                expected,
                epoch,
                operation="classify",
            )

    def test_official_epoch_accepts_one_classification_control_repair(self) -> None:
        """VC-2 到期后只可绑定一次新 Ledger/P0/no-op，且不创建第二 epoch。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            source = {
                "mode": "official_sealed",
                "official_stage": {
                    "path": "official/result.json",
                    "sha256": "1" * 64,
                    "package_digest": "2" * 64,
                },
                "previous_sealed_stage_recovery": {
                    "campaign_dir": "/campaign/prior",
                    "campaign_id": "prior-recovery",
                    "campaign_manifest_sha256": "3" * 64,
                    "reason": "sealed_stage_control_recovery",
                },
                "previous_controls": manifest["control_receipts"],
                "target_scenario": {
                    "path": "inputs/target.json",
                    "sha256": "4" * 64,
                },
            }
            context["source"] = source
            context["invariants"]["required_active_phase"] = "VC-2"
            context["stop"]["active_phase"] = "VC-2"
            context["stop_summary"]["active_phase"] = "VC-2"
            context["successor_summary"]["active_phase"] = "VC-2"
            path = codex_upgrade._control_epoch_path(campaign)
            epoch = json.loads(path.read_text(encoding="utf-8"))
            epoch["source"] = source
            epoch["invariants"] = context["invariants"]
            epoch["stop_checkpoint"] = context["stop"]
            core = dict(epoch)
            core.pop("receipt_sha256")
            epoch["receipt_sha256"] = codex_upgrade._fingerprint(core)
            path.write_text(json.dumps(epoch), encoding="utf-8")

            current_invariants = {
                **context["invariants"],
                "tool_files_sha256": "b" * 64,
            }
            repaired_controls = {
                "upgrade_timing": {"marker": "classification-ledger"},
                "arm64_environment": {"marker": "classification-p0"},
                "job_rehearsal": {"marker": "classification-noop"},
            }
            repair_core = {
                "schema_version": (
                    codex_upgrade.CONTROL_EPOCH_RUNTIME_REPAIR_SCHEMA
                ),
                "status": "active",
                "repair_index": 1,
                "created_at_utc": "2026-09-03T16:00:00Z",
                "campaign_id": manifest["campaign_id"],
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "control_epoch": {
                    "path": path.relative_to(campaign).as_posix(),
                    "sha256": codex_upgrade.file_sha256(path),
                    "bytes": path.stat().st_size,
                    "receipt_sha256": epoch["receipt_sha256"],
                },
                "previous_tool_files_sha256": context["invariants"][
                    "tool_files_sha256"
                ],
                "stop_checkpoint": context["stop"],
                "successor_controls": repaired_controls,
                "source": source,
                "invariants": current_invariants,
                "boundary": codex_upgrade._control_epoch_zero_boundary(),
                "budget": epoch["budget"],
            }
            repair = {
                **repair_core,
                "receipt_sha256": codex_upgrade._fingerprint(repair_core),
            }
            repair_path = codex_upgrade._control_epoch_runtime_repair_path(
                campaign
            )
            repair_path.write_text(json.dumps(repair), encoding="utf-8")
            effective = self._load(
                campaign,
                manifest,
                context,
                current_invariants=current_invariants,
            )
            self.assertEqual(effective["successor_controls"], repaired_controls)
            self.assertEqual(
                effective["runtime_repair"]["receipt_sha256"],
                repair["receipt_sha256"],
            )

            codex_upgrade._control_epoch_runtime_repair_amendment_path(
                campaign
            ).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "至多一份分类控制修复",
            ):
                self._load(
                    campaign,
                    manifest,
                    context,
                    current_invariants=current_invariants,
                )

    def test_plan_identity_uses_replayed_official_epoch_for_classification(
        self,
    ) -> None:
        """classify 在唯一 official epoch 重放后不得再被混合文件误拦截。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            source = campaign / "source"
            package = campaign / "package.tar.gz"
            source.mkdir(parents=True)
            package.write_bytes(b"package")
            epoch_path = codex_upgrade._control_epoch_path(campaign)
            epoch_path.parent.mkdir(parents=True, exist_ok=True)
            epoch_path.write_text("{}\n", encoding="utf-8")
            expected = self._tool_identity_fixture(
                orchestrator_sha256="a" * 64
            )
            current = self._tool_identity_fixture(
                orchestrator_sha256="b" * 64
            )
            configuration = {
                "target_source": str(source),
                "target_package": str(package),
                "live_attestation_compose_dir": "/compose",
                "live_attestation_compose_files": "-f compose.yml",
            }
            package_identity = {
                "asset_sha256": "1" * 64,
                "code_mode_host_sha256": "2" * 64,
            }
            manifest = {
                "campaign_id": "formal-official-epoch",
                "configuration": configuration,
                "target_version": "0.151.0",
                "target_sha256": "3" * 64,
                "official_identity": {
                    "source_tree_sha256": "4" * 64,
                    "cargo_lock_sha256": None,
                    "package": package_identity,
                },
                "tool_identity": expected,
            }
            epoch = {
                "source": {"mode": "official_sealed"},
                "invariants": {
                    "tool_files_sha256": expected["files_sha256"],
                    "tool_production_sha256": (
                        codex_upgrade._tool_identity_side_digest_excluding(
                            expected,
                            "production",
                            codex_upgrade._PHASE_EVALUATION_HYBRID_FILES,
                        )
                    ),
                    "compose_sha256": codex_upgrade._fingerprint(
                        {
                            "live_attestation_compose_dir": "/compose",
                            "live_attestation_compose_files": "-f compose.yml",
                        }
                    ),
                    "required_active_phase": "VC-2",
                },
                "receipt_sha256": "5" * 64,
            }
            with (
                mock.patch.object(codex_upgrade, "_verify_control_receipts"),
                mock.patch.object(
                    codex_upgrade,
                    "_directory_tree_digest",
                    return_value="4" * 64,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_codex_package",
                    return_value=package_identity,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=current,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_control_epoch_receipt",
                    return_value=epoch,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_record_evaluation_side_drift",
                ) as drift_recorder,
            ):
                result = codex_upgrade._verify_plan_identity(
                    campaign,
                    manifest,
                    operation="classify",
                )
            self.assertEqual(result["kind"], "control_epoch")
            self.assertEqual(
                result["control_epoch"]["sha256"],
                codex_upgrade.file_sha256(epoch_path),
            )
            drift_recorder.assert_called_once()

    def test_bootstrap_allows_descendant_authorized_production_drift(self) -> None:
        """祖先 bootstrap 不得用后代已授权的 production 身份否定旧 epoch。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            current = {
                **context["invariants"],
                "tool_production_sha256": "c" * 64,
            }
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "场景、compose 或工具身份漂移",
            ):
                self._load(
                    campaign,
                    manifest,
                    context,
                    current_invariants=current,
                )
            self.assertEqual(
                self._load(
                    campaign,
                    manifest,
                    context,
                    current_invariants=current,
                    allow_unrepaired_tool_drift=True,
                )["epoch_index"],
                1,
            )

    def test_control_replacement_allows_only_registered_production_drift(self) -> None:
        """最终 transition 登记的失败闭集产出变化不得击穿历史 epoch。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(
                Path(directory),
                final_scope=True,
            )
            manifest["predecessor"] = {
                "reason": "candidate_recovery_control_replacement"
            }
            current = {
                **context["invariants"],
                "tool_files_sha256": "b" * 64,
                "tool_production_sha256": "c" * 64,
            }
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "场景、compose 或工具身份漂移",
            ):
                self._load(
                    campaign,
                    manifest,
                    context,
                    current_invariants=current,
                )
            self.assertEqual(
                self._load(
                    campaign,
                    manifest,
                    context,
                    current_invariants=current,
                    allowed_production_paths={
                        "run_sub2api_openai_mitm_matrix.sh"
                    },
                )["epoch_index"],
                1,
            )

    def test_loads_two_epoch_chain_with_continuous_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            first_path = codex_upgrade._control_epoch_path(campaign)
            first = json.loads(first_path.read_text(encoding="utf-8"))
            stop = {
                **context["stop"],
                "ledger_dir": "/control/new",
                "upgrade_id": "new-ledger",
                "head_sequence": 20,
                "head_sha256": "b" * 64,
                "total_elapsed_seconds": 30,
                "total_deadline_at_utc": "2026-09-03T17:50:00+00:00",
            }
            stop_summary = {**stop, "status": "stopped"}
            successor_controls = {
                "upgrade_timing": {
                    "ledger_dir": "/control/new-2",
                    "upgrade_id": "new-ledger-2",
                },
                "arm64_environment": {"marker": "new-arm-2"},
                "job_rehearsal": {"marker": "new-rehearsal-2"},
            }
            successor_summary = {
                "status": "active",
                "active_phase": "VC-4",
                "total_elapsed_seconds": 5,
                "total_live_request_count": 0,
                "total_deadline_at_utc": "2026-09-03T17:49:00+00:00",
            }
            core = {
                "schema_version": codex_upgrade.CONTROL_EPOCH_SCHEMA,
                "status": "active",
                "epoch_index": 2,
                "created_at_utc": "2026-09-03T16:30:00Z",
                "campaign_id": manifest["campaign_id"],
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "previous_epoch_sha256": first["receipt_sha256"],
                "previous_controls": context["successor_controls"],
                "stop_checkpoint": stop,
                "successor_controls": successor_controls,
                "source": context["source"],
                "invariants": context["invariants"],
                "boundary": codex_upgrade._control_epoch_zero_boundary(),
                "budget": {
                    "predecessor_total_deadline_at_utc": stop[
                        "total_deadline_at_utc"
                    ],
                    "successor_total_deadline_at_utc": successor_summary[
                        "total_deadline_at_utc"
                    ],
                    "predecessor_elapsed_seconds": 30,
                    "successor_elapsed_seconds": 5,
                    "cumulative_elapsed_seconds": 135,
                    "predecessor_live_request_count": 0,
                    "successor_live_request_count": 0,
                    "cumulative_live_request_count": 0,
                },
            }
            second = {**core, "receipt_sha256": codex_upgrade._fingerprint(core)}
            codex_upgrade._control_epoch_path(campaign, 2).write_text(
                json.dumps(second),
                encoding="utf-8",
            )

            def stop_checkpoint(
                _manifest: object,
                ledger_root: Path,
                _receipt: Path,
                **_kwargs: object,
            ) -> tuple[dict[str, object], dict[str, object]]:
                if str(ledger_root) == "/control/new":
                    return stop, stop_summary
                return context["stop"], context["stop_summary"]

            def timing_checkpoint(
                timing: dict[str, object],
                **_kwargs: object,
            ) -> tuple[Path, dict[str, object]]:
                summary = (
                    successor_summary
                    if timing.get("upgrade_id") == "new-ledger-2"
                    else context["successor_summary"]
                )
                return Path(str(timing["ledger_dir"])), {"summary": summary}

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_source_context",
                    return_value=context["source"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value={
                        "files_sha256": context["invariants"][
                            "tool_files_sha256"
                        ]
                    },
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_invariants",
                    return_value=context["invariants"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_stop_checkpoint",
                    side_effect=stop_checkpoint,
                ),
                mock.patch.object(codex_upgrade, "_verify_control_receipts"),
                mock.patch.object(
                    codex_upgrade,
                    "_sealed_stage_timing_checkpoint",
                    side_effect=timing_checkpoint,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_failed_scope_production_paths",
                    return_value=set(),
                ),
            ):
                loaded = codex_upgrade._load_control_epoch_receipt(
                    campaign,
                    manifest,
                )
            self.assertEqual(loaded, second)

    def test_rejects_nonzero_boundary_and_third_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            path = codex_upgrade._control_epoch_path(campaign)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["boundary"]["reservation_count"] = 1
            core = dict(payload)
            core.pop("receipt_sha256")
            payload["receipt_sha256"] = codex_upgrade._fingerprint(core)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "发布边界"):
                self._load(campaign, manifest, context)

            (path.parent / "control-epoch-02.json").write_text("{}", encoding="utf-8")
            self.assertEqual(len(codex_upgrade._control_epoch_files(campaign)), 2)
            (path.parent / "control-epoch-03.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "最多两份"):
                codex_upgrade._control_epoch_files(campaign)

    def test_empty_boundary_rejects_existing_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            reservation = campaign / "candidates" / "c1" / "attempts" / "a1" / "reservation.json"
            reservation.parent.mkdir(parents=True)
            reservation.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "必须全为 0"):
                codex_upgrade._control_epoch_empty_boundary(campaign)

    def test_control_replacement_epoch_accepts_vc0_stop(self) -> None:
        """控制替代后继的唯一 epoch 可从零执行 VC-0 停线承接。"""

        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger"
            ledger.mkdir()
            receipt = ledger / "stop.json"
            receipt.write_text("{}\n", encoding="utf-8")
            timing = {
                "ledger_dir": str(ledger.resolve()),
                "upgrade_id": "replacement-ledger",
                "evidence_decision": "reuse",
            }
            manifest = {
                "predecessor": {
                    "reason": "candidate_recovery_control_replacement",
                },
                "control_receipts": {"upgrade_timing": timing},
            }
            summary = {
                "status": "stopped",
                "active_phase": "VC-0",
                "upgrade_id": "replacement-ledger",
                "evidence_decision": "reuse",
                "head_sequence": 2,
                "head_sha256": "a" * 64,
                "total_elapsed_seconds": 60,
                "total_live_request_count": 0,
                "total_deadline_at_utc": "2026-09-03T23:13:50+00:00",
            }
            with (
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "replay",
                    return_value={"summary": summary},
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "inspect_ledger",
                    return_value=summary,
                ),
            ):
                binding, observed = codex_upgrade._control_epoch_stop_checkpoint(
                    manifest,
                    ledger,
                    receipt,
                    expected_timing=timing,
                )
            self.assertEqual(binding["active_phase"], "VC-0")
            self.assertEqual(observed, summary)

    def test_historical_stop_checkpoint_allows_advanced_current_head(self) -> None:
        """历史重放只校验冻结 checkpoint，不绑定 Ledger 今天的 head。"""

        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger"
            ledger.mkdir()
            receipt = ledger / "stop.json"
            receipt.write_text("{}\n", encoding="utf-8")
            timing = {
                "ledger_dir": str(ledger.resolve()),
                "upgrade_id": "historical-ledger",
                "evidence_decision": "reuse",
            }
            manifest = {"control_receipts": {"upgrade_timing": timing}}
            frozen = {
                "status": "stopped",
                "active_phase": "VC-4",
                "upgrade_id": "historical-ledger",
                "evidence_decision": "reuse",
                "head_sequence": 2,
                "head_sha256": "a" * 64,
                "total_elapsed_seconds": 60,
                "total_live_request_count": 0,
                "total_deadline_at_utc": "2026-09-03T23:13:50+00:00",
            }
            with (
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "replay",
                    return_value={"summary": frozen},
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "inspect_ledger",
                    return_value={
                        **frozen,
                        "head_sequence": 3,
                        "head_sha256": "b" * 64,
                    },
                ) as inspect_ledger,
            ):
                binding, observed = codex_upgrade._control_epoch_stop_checkpoint(
                    manifest,
                    ledger,
                    receipt,
                    expected_timing=timing,
                    _historical_ledger_heads=True,
                )
            inspect_ledger.assert_not_called()
            self.assertEqual(binding["head_sha256"], frozen["head_sha256"])
            self.assertEqual(observed, frozen)

    def test_current_stop_checkpoint_rejects_advanced_current_head(self) -> None:
        """当前正式操作仍必须绑定 Ledger 当前终态 head。"""

        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger"
            ledger.mkdir()
            receipt = ledger / "stop.json"
            receipt.write_text("{}\n", encoding="utf-8")
            timing = {
                "ledger_dir": str(ledger.resolve()),
                "upgrade_id": "current-ledger",
                "evidence_decision": "reuse",
            }
            manifest = {"control_receipts": {"upgrade_timing": timing}}
            frozen = {
                "status": "stopped",
                "active_phase": "VC-4",
                "upgrade_id": "current-ledger",
                "evidence_decision": "reuse",
                "head_sequence": 2,
                "head_sha256": "a" * 64,
                "total_elapsed_seconds": 60,
                "total_live_request_count": 0,
                "total_deadline_at_utc": "2026-09-03T23:13:50+00:00",
            }
            with (
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "replay",
                    return_value={"summary": frozen},
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "inspect_ledger",
                    return_value={
                        **frozen,
                        "head_sequence": 3,
                        "head_sha256": "b" * 64,
                    },
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "当前 VC-4 停线终态 head",
                ),
            ):
                codex_upgrade._control_epoch_stop_checkpoint(
                    manifest,
                    ledger,
                    receipt,
                    expected_timing=timing,
                )

    def test_control_replacement_epoch_replays_frozen_source(self) -> None:
        """控制替代 epoch 必须从签名导入收据重放原失败闭集。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "successor"
            predecessor = root / "predecessor"
            campaign.mkdir()
            predecessor.mkdir()
            (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
            (predecessor / "campaign.json").write_text("{}\n", encoding="utf-8")
            scenario = {"path": "inputs/target.json", "sha256": "1" * 64}
            source = {
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "target_scenario": scenario,
            }
            predecessor_controls = {
                "upgrade_timing": {"marker": "old-timing"},
                "arm64_environment": {"marker": "old-arm"},
                "job_rehearsal": {"marker": "old-rehearsal"},
            }
            current_controls = {
                "upgrade_timing": {"marker": "new-timing"},
                "arm64_environment": {"marker": "new-arm"},
                "job_rehearsal": {"marker": "new-rehearsal"},
            }
            stored = {
                "effective_epoch": {"path": "control-epochs/control-epoch-01.json"},
                "runtime_repair": None,
                "unpublished_stop_checkpoint": {
                    "ledger_dir": "/control/unpublished",
                    "receipt": {"path": "stop.json"},
                },
                "supervisor_audit": {"run_dir": "/control/audit"},
                "source_attempt": {
                    "candidate_id": "candidate-a",
                    "attempt_id": "attempt-a",
                },
                "effective_controls": predecessor_controls,
                "source": source,
            }
            predecessor_manifest = {
                "campaign_id": "predecessor-campaign",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
            }
            manifest = {
                "campaign_id": "replacement-campaign",
                "campaign_mode": "formal",
                "predecessor": {
                    "campaign_dir": str(predecessor),
                    "campaign_id": "predecessor-campaign",
                    "campaign_manifest_sha256": codex_upgrade.file_sha256(
                        predecessor / "campaign.json"
                    ),
                    "reason": "candidate_recovery_control_replacement",
                },
                "inputs": {"target_discovery_scenarios": scenario},
                "control_receipts": current_controls,
            }
            core = {
                "schema_version": (
                    codex_upgrade.PREDECESSOR_CONTROL_REPLACEMENT_IMPORT_SCHEMA
                ),
                "reason": "candidate_recovery_control_replacement",
                "successor_campaign_id": "replacement-campaign",
                "successor_campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "control_replacement": stored,
                "recovery_control_transition": {
                    "predecessor": {
                        "upgrade_timing": predecessor_controls["upgrade_timing"],
                        "arm64_environment": predecessor_controls[
                            "arm64_environment"
                        ],
                    },
                    "successor": {
                        "upgrade_timing": current_controls["upgrade_timing"],
                        "arm64_environment": current_controls[
                            "arm64_environment"
                        ],
                    },
                },
                "job_rehearsal_transition": {
                    "predecessor": predecessor_controls["job_rehearsal"],
                    "successor": current_controls["job_rehearsal"],
                },
            }
            receipt = {**core, "receipt_digest": codex_upgrade._fingerprint(core)}
            (campaign / "predecessor-import.json").write_text(
                json.dumps(receipt),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=predecessor_manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_replacement_context",
                    return_value=stored,
                ),
            ):
                observed = codex_upgrade._control_epoch_source_context(
                    campaign,
                    manifest,
                    candidate_id="candidate-a",
                    attempt_id="attempt-a",
                )
            self.assertEqual(observed, source)

    def test_control_replacement_context_accepts_legacy_missing_continuity(self) -> None:
        """旧监督器缺少 continuity 时只允许兼容为当前 null。"""

        stored = {
            "supervisor_audit": {
                "summary": {
                    "schema_version": "codex-upgrade-supervisor/v1",
                    "audit_incomplete": False,
                    "integrity_errors": [],
                }
            },
            "source": {"candidate_id": "candidate-a"},
        }
        replayed = {
            "supervisor_audit": {
                "summary": {
                    "schema_version": "codex-upgrade-supervisor/v1",
                    "audit_incomplete": False,
                    "integrity_errors": [],
                    "continuity": None,
                }
            },
            "source": {"candidate_id": "candidate-a"},
        }
        self.assertTrue(
            codex_upgrade._control_replacement_context_matches(stored, replayed)
        )

        replayed["supervisor_audit"]["summary"]["continuity"] = {
            "gap_seconds": 0.0,
        }
        self.assertFalse(
            codex_upgrade._control_replacement_context_matches(stored, replayed)
        )

    def test_control_replacement_rejects_second_epoch_without_repair_02(self) -> None:
        """控制替代后继缺少 repair-02 时不得创建最终 epoch。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            manifest["predecessor"] = {
                "reason": "candidate_recovery_control_replacement"
            }
            (
                campaign
                / codex_upgrade.CONTROL_EPOCH_DIRECTORY
                / "control-epoch-02.json"
            ).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "必须是 repair-02 后",
            ):
                self._load(campaign, manifest, context)

    def test_control_replacement_loads_one_bounded_final_execution_epoch(self) -> None:
        """repair-02 后只允许绑定完整审计的 2/7 最终执行 epoch。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(
                Path(directory),
                final_scope=True,
            )
            manifest["predecessor"] = {
                "reason": "candidate_recovery_control_replacement"
            }
            first_path = codex_upgrade._control_epoch_path(campaign)
            first = json.loads(first_path.read_text(encoding="utf-8"))
            repair_root = campaign / codex_upgrade.CONTROL_EPOCH_DIRECTORY
            first_repair = repair_root / codex_upgrade.CONTROL_EPOCH_RUNTIME_REPAIR_FILENAME
            repair_02 = (
                repair_root
                / codex_upgrade.CONTROL_EPOCH_RUNTIME_REPAIR_AMENDMENT_FILENAME
            )
            first_repair.write_text("{}\n", encoding="utf-8")
            repair_02.write_text("{}\n", encoding="utf-8")
            effective_first = json.loads(json.dumps(first))
            effective_first["runtime_repair"] = {
                "path": repair_02.relative_to(campaign).as_posix(),
                "sha256": codex_upgrade.file_sha256(repair_02),
                "bytes": repair_02.stat().st_size,
                "receipt_sha256": "d" * 64,
            }
            authorization = {
                "schema_version": codex_upgrade.CONTROL_EPOCH_FINAL_EXECUTION_SCHEMA,
                "mode": codex_upgrade.CONTROL_EPOCH_FINAL_EXECUTION_MODE,
                "supervisor_audit": {"run_dir": "/control/audit"},
                "marker": "complete-audit",
            }
            stop = {
                **context["stop"],
                "ledger_dir": "/control/new",
                "upgrade_id": "new-ledger",
                "active_phase": "VC-4",
                "head_sequence": 20,
                "head_sha256": "b" * 64,
                "total_elapsed_seconds": 30,
                "total_deadline_at_utc": "2026-09-03T17:50:00+00:00",
            }
            stop_summary = {**stop, "status": "stopped"}
            successor_controls = {
                "upgrade_timing": {
                    "ledger_dir": "/control/final",
                    "upgrade_id": "final-ledger",
                    "evidence_decision": context["successor_controls"][
                        "upgrade_timing"
                    ].get("evidence_decision"),
                },
                "arm64_environment": {"marker": "final-arm"},
                "job_rehearsal": {"marker": "final-rehearsal"},
            }
            successor_summary = {
                "status": "active",
                "active_phase": "VC-4",
                "total_elapsed_seconds": 5,
                "total_live_request_count": 0,
                "total_deadline_at_utc": "2026-09-03T18:30:00+00:00",
            }
            core = {
                "schema_version": codex_upgrade.CONTROL_EPOCH_SCHEMA,
                "status": "active",
                "epoch_index": 2,
                "created_at_utc": "2026-09-03T16:30:00Z",
                "campaign_id": manifest["campaign_id"],
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "previous_epoch_sha256": first["receipt_sha256"],
                "previous_controls": context["successor_controls"],
                "stop_checkpoint": stop,
                "successor_controls": successor_controls,
                "source": context["source"],
                "invariants": context["invariants"],
                "boundary": codex_upgrade._control_epoch_zero_boundary(),
                "budget": {
                    "predecessor_total_deadline_at_utc": stop[
                        "total_deadline_at_utc"
                    ],
                    "successor_total_deadline_at_utc": successor_summary[
                        "total_deadline_at_utc"
                    ],
                    "predecessor_elapsed_seconds": 30,
                    "successor_elapsed_seconds": 5,
                    "cumulative_elapsed_seconds": 135,
                    "predecessor_live_request_count": 0,
                    "successor_live_request_count": 0,
                    "cumulative_live_request_count": 0,
                },
                "execution_authorization": authorization,
            }
            second = {**core, "receipt_sha256": codex_upgrade._fingerprint(core)}
            second_path = codex_upgrade._control_epoch_path(campaign, 2)
            second_path.write_text(json.dumps(second), encoding="utf-8")

            def stop_checkpoint(
                _manifest: object,
                ledger_root: Path,
                _receipt: Path,
                **_kwargs: object,
            ) -> tuple[dict[str, object], dict[str, object]]:
                if str(ledger_root) == "/control/new":
                    return stop, stop_summary
                return context["stop"], context["stop_summary"]

            def timing_checkpoint(
                timing: dict[str, object],
                **_kwargs: object,
            ) -> tuple[Path, dict[str, object]]:
                summary = (
                    successor_summary
                    if timing.get("upgrade_id") == "final-ledger"
                    else context["successor_summary"]
                )
                return Path(str(timing["ledger_dir"])), {"summary": summary}

            patches = (
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_source_context",
                    return_value=context["source"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value={
                        "files_sha256": context["invariants"][
                            "tool_files_sha256"
                        ]
                    },
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_invariants",
                    return_value=context["invariants"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_stop_checkpoint",
                    side_effect=stop_checkpoint,
                ),
                mock.patch.object(codex_upgrade, "_verify_control_receipts"),
                mock.patch.object(
                    codex_upgrade,
                    "_sealed_stage_timing_checkpoint",
                    side_effect=timing_checkpoint,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_control_epoch_runtime_repair",
                    return_value=effective_first,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_final_execution_authorization",
                    return_value=authorization,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_failed_scope_production_paths",
                    return_value=set(),
                ),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[
                5
            ], patches[6], patches[7], patches[8]:
                loaded = codex_upgrade._load_control_epoch_receipt(campaign, manifest)
            self.assertEqual(loaded, second)

            second["budget"]["successor_total_deadline_at_utc"] = (
                "2026-09-03T23:31:00+00:00"
            )
            successor_summary["total_deadline_at_utc"] = (
                second["budget"]["successor_total_deadline_at_utc"]
            )
            unsigned = dict(second)
            unsigned.pop("receipt_sha256")
            second["receipt_sha256"] = codex_upgrade._fingerprint(unsigned)
            second_path.write_text(json.dumps(second), encoding="utf-8")
            patches = (
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_source_context",
                    return_value=context["source"],
                ),
                mock.patch.object(codex_upgrade, "_tool_identity", return_value={}),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_invariants",
                    return_value=context["invariants"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_stop_checkpoint",
                    side_effect=stop_checkpoint,
                ),
                mock.patch.object(codex_upgrade, "_verify_control_receipts"),
                mock.patch.object(
                    codex_upgrade,
                    "_sealed_stage_timing_checkpoint",
                    side_effect=timing_checkpoint,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_control_epoch_runtime_repair",
                    return_value=effective_first,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_final_execution_authorization",
                    return_value=authorization,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_failed_scope_production_paths",
                    return_value=set(),
                ),
            )
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                patches[6],
                patches[7],
                patches[8],
                self.assertRaisesRegex(codex_upgrade.ConfigurationError, "预算、阶段"),
            ):
                codex_upgrade._load_control_epoch_receipt(campaign, manifest)

    def test_final_execution_scope_is_exactly_two_execute_seven_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _campaign, _manifest, context = self._fixture(
                Path(directory),
                final_scope=True,
            )
            scope = codex_upgrade._control_epoch_final_execution_scope(
                context["source"]
            )
            self.assertEqual(scope["execute_count"], 2)
            self.assertEqual(scope["reused_count"], 7)
            context["source"]["reused_job_ids"] = context["source"][
                "reused_job_ids"
            ][:-1]
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "2 执行／7 复用"):
                codex_upgrade._control_epoch_final_execution_scope(context["source"])

    def test_control_replacement_allows_single_runtime_repair(self) -> None:
        """控制替代的唯一 epoch 可追加一次当前工具 runtime-repair。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            manifest["predecessor"] = {
                "reason": "candidate_recovery_control_replacement"
            }
            _path, payload, current_invariants = self._add_runtime_repair(
                campaign,
                manifest,
                context,
            )
            loaded = self._load(
                campaign,
                manifest,
                context,
                current_invariants=current_invariants,
            )
            self.assertEqual(
                loaded["runtime_repair"]["receipt_sha256"],
                payload["receipt_sha256"],
            )

    def test_runtime_scenario_walk_replays_replacement_predecessor_historically(self) -> None:
        """运行时场景前序遍历必须按 replacement 边界启用历史控制重放。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "successor"
            predecessor = root / "predecessor"
            campaign.mkdir()
            predecessor.mkdir()
            (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
            predecessor_manifest_path = predecessor / "campaign.json"
            predecessor_manifest_path.write_text("{}\n", encoding="utf-8")
            scenario = {"path": "inputs/target.json", "sha256": "a" * 64}
            manifest = {
                "inputs": {"target_discovery_scenarios": scenario},
                "predecessor": {
                    "campaign_dir": str(predecessor.resolve()),
                    "campaign_id": "predecessor-campaign",
                    "campaign_manifest_sha256": codex_upgrade.file_sha256(
                        predecessor_manifest_path
                    ),
                    "reason": "candidate_recovery_control_replacement",
                },
            }
            predecessor_manifest = {
                "campaign_id": "predecessor-campaign",
                "inputs": {"target_discovery_scenarios": scenario},
            }
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value=predecessor_manifest,
            ) as loader:
                changed = codex_upgrade._bound_runtime_scenario_transition_job_ids(
                    campaign,
                    manifest,
                )
            self.assertEqual(changed, frozenset())
            self.assertTrue(
                loader.call_args.kwargs["_control_epoch_bootstrap"]
            )

    def test_seal_preview_attempt_replay_preserves_historical_context(self) -> None:
        """前序 seal-preview 重放 attempt 时不得丢失历史控制上下文。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            attempt_path = campaign / "attempt.json"
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_file",
                    return_value=attempt_path,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_read_json",
                    return_value={"attempt_id": "attempt-a"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    side_effect=codex_upgrade.ConfigurationError(
                        "stop-after-context-check"
                    ),
                ) as loader,
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "stop-after-context-check",
                ),
            ):
                codex_upgrade._verify_capture_seal_preview(
                    campaign,
                    {"attempt": {"path": "attempt.json"}},
                    "capture-official",
                    _historical_manifest_controls=True,
                )
            loader.assert_called_once_with(
                campaign,
                "official",
                None,
                "attempt-a",
                _historical_manifest_controls=True,
            )

    def test_direct_predecessor_attempt_preserves_historical_context(self) -> None:
        """直接前序 official attempt 重放必须承接历史控制上下文。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            attempt_path = campaign / "attempts" / "attempt-a" / "attempt.json"
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_file",
                    return_value=attempt_path,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    side_effect=codex_upgrade.ConfigurationError(
                        "stop-after-context-check"
                    ),
                ) as loader,
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "stop-after-context-check",
                ),
            ):
                codex_upgrade._validate_direct_predecessor_official_attempt(
                    campaign,
                    {
                        "attempt": {
                            "path": "attempts/attempt-a/attempt.json",
                            "sha256": "a" * 64,
                        }
                    },
                    _historical_manifest_controls=True,
                )
            loader.assert_called_once_with(
                campaign,
                "official",
                None,
                "attempt-a",
                _historical_manifest_controls=True,
            )

    def test_direct_predecessor_reservation_preserves_historical_context(self) -> None:
        """直接前序 reservation 二次重放也必须承接历史控制上下文。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            attempt_root = campaign / "attempts" / "attempt-a"
            attempt_path = attempt_root / "attempt.json"
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_file",
                    return_value=attempt_path,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, {"results": []}),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    side_effect=codex_upgrade.ConfigurationError(
                        "stop-after-reservation-context-check"
                    ),
                ) as loader,
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "stop-after-reservation-context-check",
                ),
            ):
                codex_upgrade._validate_direct_predecessor_official_attempt(
                    campaign,
                    {
                        "attempt": {
                            "path": "attempts/attempt-a/attempt.json",
                            "sha256": "a" * 64,
                        }
                    },
                    _historical_manifest_controls=True,
                )
            loader.assert_called_once_with(
                campaign,
                attempt_root,
                phase="official",
                candidate_id=None,
                _historical_manifest_controls=True,
            )

    def test_control_epoch_resolves_replacement_attempt_from_bound_source(self) -> None:
        """replacement 执行必须到冻结 source Campaign 取失败 attempt。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "replacement"
            predecessor = root / "control-refresh"
            source = root / "failed-source"
            for path, campaign_id in (
                (campaign, "replacement"),
                (predecessor, "control-refresh"),
                (source, "failed-source"),
            ):
                path.mkdir()
                (path / "campaign.json").write_text(
                    json.dumps({"campaign_id": campaign_id}) + "\n",
                    encoding="utf-8",
                )
            attempt = (
                source
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "attempt-a"
            )
            attempt.mkdir(parents=True)
            predecessor_manifest = predecessor / "campaign.json"
            source_manifest = source / "campaign.json"
            manifest = {
                "predecessor": {
                    "campaign_dir": str(predecessor.resolve()),
                    "campaign_id": "control-refresh",
                    "campaign_manifest_sha256": codex_upgrade.file_sha256(
                        predecessor_manifest
                    ),
                    "reason": "candidate_recovery_control_replacement",
                }
            }
            (campaign / "predecessor-import.json").write_text(
                json.dumps(
                    {
                        "schema_version": (
                            codex_upgrade.PREDECESSOR_CONTROL_REPLACEMENT_IMPORT_SCHEMA
                        ),
                        "reason": "candidate_recovery_control_replacement",
                        "control_replacement": {
                            "source_campaign": {
                                "campaign_dir": str(source.resolve()),
                                "campaign_id": "failed-source",
                                "campaign_manifest_sha256": (
                                    codex_upgrade.file_sha256(source_manifest)
                                ),
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            resolved = codex_upgrade._control_epoch_source_campaign_dir(
                campaign,
                manifest,
            )
            self.assertEqual(resolved, source.resolve())
            self.assertEqual(
                codex_upgrade._capture_attempt_path(
                    resolved,
                    "candidate",
                    "candidate-a",
                    "attempt-a",
                ),
                attempt.resolve(),
            )

    def test_parser_requires_controls_and_exposes_official_phase(self) -> None:
        parser = codex_upgrade._build_parser()
        command = next(
            action.choices["control-epoch"]
            for action in parser._actions
            if getattr(action, "choices", None) and "control-epoch" in action.choices
        )
        actions = {action.dest: action for action in command._actions}
        self.assertEqual(set(actions["phase"].choices), {"official", "candidate"})
        self.assertFalse(actions["candidate_id"].required)
        self.assertFalse(actions["attempt_id"].required)
        for name in (
            "predecessor_stop_ledger_dir",
            "predecessor_stop_receipt",
            "recovery_timing_ledger_dir",
            "recovery_timing_receipt",
            "recovery_arm64_environment_root",
            "recovery_arm64_environment_receipt",
            "job_rehearsal_root",
            "job_rehearsal_receipt",
        ):
            self.assertTrue(actions[name].required, name)

    def test_official_sealed_source_binds_stage_and_prior_recovery(self) -> None:
        """official epoch 只读绑定当前封存阶段和已使用的恢复原因。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            official_path = campaign / "official" / "result.json"
            official_path.parent.mkdir(parents=True)
            official_path.write_text("{}\n", encoding="utf-8")
            manifest = {
                "campaign_mode": "formal",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                        "sha256": "5" * 64,
                    }
                },
            }
            prior = {
                "campaign_dir": "/campaign/prior",
                "campaign_id": "prior-recovery",
                "campaign_manifest_sha256": "6" * 64,
                "reason": "sealed_stage_control_recovery",
            }
            previous_controls = {
                "upgrade_timing": {"ledger_dir": "/ledger/recovery"},
                "arm64_environment": {"evidence_root": "/p0/recovery"},
                "job_rehearsal": {"evidence_root": "/noop/recovery"},
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_successor_reason_ancestor",
                    return_value=prior,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_stage_result",
                    return_value={
                        "status": "complete",
                        "package_digest": "7" * 64,
                    },
                ) as stage_loader,
                mock.patch.object(
                    codex_upgrade,
                    "_sealed_stage_recovery_context",
                    return_value={"effective_controls": previous_controls},
                ),
            ):
                source = (
                    codex_upgrade._official_sealed_control_epoch_source_context(
                        campaign,
                        manifest,
                    )
                )
            self.assertEqual(source["mode"], "official_sealed")
            self.assertEqual(source["previous_sealed_stage_recovery"], prior)
            self.assertEqual(source["previous_controls"], previous_controls)
            self.assertEqual(source["official_stage"]["sha256"], codex_upgrade.file_sha256(official_path))
            self.assertEqual(
                codex_upgrade._control_epoch_required_phase(source),
                "VC-2",
            )
            stage_loader.assert_called_once_with(
                campaign,
                "capture-official",
                _ignore_checkpoint=True,
                _skip_evidence_scan=True,
                _historical_manifest_controls=True,
                _verified_campaign_manifest=manifest,
            )

    def test_official_sealed_source_rejects_existing_classification(self) -> None:
        """出现分类草案后不能再发布 official epoch。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            (campaign / "classification").mkdir(parents=True)
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "分类草案或结果",
            ):
                codex_upgrade._official_sealed_control_epoch_source_context(
                    campaign,
                    {"campaign_mode": "formal"},
                )

    def test_official_epoch_stage_replay_does_not_reload_same_manifest(self) -> None:
        """epoch 写入后的源阶段重放不得递归加载同一 Campaign 控制收据。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            official_path = campaign / "official" / "result.json"
            official_path.parent.mkdir(parents=True)
            manifest_path = campaign / "campaign.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            manifest = {
                "campaign_id": "formal-official-epoch",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
            }
            core = {
                "stage": "capture-official",
                "campaign_id": manifest["campaign_id"],
                "campaign_mode": manifest["campaign_mode"],
                "campaign_purpose": manifest["campaign_purpose"],
                "candidate_purpose": None,
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    manifest_path
                ),
                "status": "complete",
                "attempt": {},
                "seal_preview": {},
            }
            payload = {**core, "package_digest": codex_upgrade._fingerprint(core)}
            official_path.write_text(json.dumps(payload), encoding="utf-8")
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    side_effect=AssertionError("不得递归重载 Campaign"),
                ),
                mock.patch.object(codex_upgrade, "_validate_stage_contract"),
                mock.patch.object(codex_upgrade, "_verify_campaign_binding"),
            ):
                replayed = codex_upgrade._load_stage_result(
                    campaign,
                    "capture-official",
                    _shallow=True,
                    _verified_campaign_manifest=manifest,
                )
            self.assertEqual(replayed, payload)

    def test_sealed_recovery_proxy_uses_explicit_control_override(self) -> None:
        """代理清单只能重放已解析 recovery 控制，不得重新发现当前 epoch。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            campaign.mkdir()
            manifest_path = campaign / "campaign.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            official_path = campaign / "official" / "result.json"
            official_path.parent.mkdir()
            official_path.write_text("{}\n", encoding="utf-8")
            digest = "a" * 64
            effective_controls = {
                "upgrade_timing": {"ledger_dir": "/ledger/recovery"},
                "arm64_environment": {"evidence_root": "/p0/recovery"},
                "job_rehearsal": {"evidence_root": "/noop/recovery"},
            }
            transition_core = {
                "schema_version": codex_upgrade.TOOL_EVALUATION_TRANSITION_SCHEMA,
                "status": "approved",
                "phase": "official",
                "campaign_id": "formal-sealed-recovery",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    manifest_path
                ),
                "recovery_controls": {"recovery": effective_controls},
            }
            transition = {
                **transition_core,
                "transition_digest": codex_upgrade._fingerprint(transition_core),
            }
            transition_path = campaign / "transition.json"
            transition_path.write_text(json.dumps(transition), encoding="utf-8")
            manifest = {
                "campaign_id": "formal-sealed-recovery",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "tool_identity": {"files_sha256": "b" * 64},
            }
            official = {
                "evaluation_transition": {
                    "path": "transition.json",
                    "sha256": codex_upgrade.file_sha256(transition_path),
                },
                "evidence_manifest": {"path": "evidence.json", "sha256": digest},
                "package_digest": "c" * 64,
            }
            allowed_path = next(iter(codex_upgrade._SEALED_STAGE_RECOVERY_ALLOWED_FILES))
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value={"files_sha256": "d" * 64},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_changed_files",
                    return_value=[{"path": allowed_path}],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity_drift",
                    return_value={"production": []},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_control_receipts",
                ) as verifier,
            ):
                context = codex_upgrade._sealed_stage_recovery_context(
                    campaign,
                    manifest,
                    official,
                    require_active=False,
                )
            self.assertEqual(context["effective_controls"], effective_controls)
            verifier.assert_called_once()
            self.assertEqual(
                verifier.call_args.kwargs["_control_override"],
                {
                    "upgrade_timing": effective_controls["upgrade_timing"],
                    "arm64_environment": effective_controls["arm64_environment"],
                },
            )

    def test_official_epoch_uses_recovery_controls_not_manifest_controls(self) -> None:
        """official epoch 必须续接 sealed-stage 实际控制，不能退回初始清单。"""

        manifest_controls = {"upgrade_timing": {"ledger_dir": "/ledger/initial"}}
        recovery_controls = {"upgrade_timing": {"ledger_dir": "/ledger/recovery"}}
        resolved = codex_upgrade._control_epoch_previous_controls(
            {"control_receipts": manifest_controls},
            {
                "mode": "official_sealed",
                "previous_controls": recovery_controls,
            },
            None,
        )
        self.assertEqual(resolved, recovery_controls)
        self.assertNotEqual(resolved, manifest_controls)

    def test_epoch_loader_uses_same_previous_control_resolver_as_writer(self) -> None:
        """epoch 加载器不得绕过发布器使用的 previous-controls 解析规则。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_previous_controls",
                    side_effect=codex_upgrade.ConfigurationError(
                        "previous-control-resolver-used"
                    ),
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "previous-control-resolver-used",
                ),
            ):
                self._load(campaign, manifest, context)

    def test_active_unsealed_scan_propagates_historical_stage_context(self) -> None:
        """状态辅助路径读取封存阶段时必须保留历史控制上下文。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            result_path = campaign / "official" / "result.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                codex_upgrade,
                "_load_stage_result",
                return_value={
                    "attempt": {
                        "path": "official/attempts/attempt-a/attempt.json"
                    }
                },
            ) as stage_loader:
                active = codex_upgrade._active_unsealed_attempts(
                    campaign,
                    "official",
                )
            self.assertEqual(active, [])
            stage_loader.assert_called_once_with(
                campaign,
                "capture-official",
                None,
                _historical_manifest_controls=True,
            )

    def test_successor_parser_exposes_bounded_control_replacement(self) -> None:
        parser = codex_upgrade._build_parser()
        command = next(
            action.choices["successor"]
            for action in parser._actions
            if getattr(action, "choices", None) and "successor" in action.choices
        )
        actions = {action.dest: action for action in command._actions}
        reason = actions["reason"]
        self.assertIn(
            "candidate_recovery_control_replacement",
            reason.choices,
        )
        for name in (
            "predecessor_control_epoch",
            "predecessor_control_runtime_repair",
            "predecessor_unpublished_ledger_dir",
            "predecessor_unpublished_stop_receipt",
            "predecessor_supervisor_run_dir",
        ):
            self.assertIn(name, actions)

    def test_control_replacement_binds_complete_terminal_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory).resolve() / "run-a"
            run_dir.mkdir(mode=0o700)
            owner_nonce = "a" * 64
            state = {
                "state": "failed",
                "campaign_id": "audit-a",
                "phase": "official",
                "owner_pid": 123,
                "owner_nonce": owner_nonce,
                "terminal_at_utc": "2026-09-03T18:00:00Z",
            }
            stop = {
                "schema_version": codex_upgrade.codex_upgrade_supervisor.STOP_SCHEMA,
                "event_type": "watchdog-aborted",
                "reason": "campaign-activity-deadline-expired",
                "detected_at_utc": "2026-09-03T18:00:00Z",
                "detected_at_epoch": 1.0,
                "owner_pid": 123,
                "owner_nonce": owner_nonce,
                "campaign_id": "audit-a",
                "phase": "official",
            }
            stop["receipt_sha256"] = codex_upgrade.codex_upgrade_supervisor._sha256(
                codex_upgrade.codex_upgrade_supervisor._canonical(stop)
            )
            (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            (run_dir / "stop-receipt.json").write_text(
                json.dumps(stop), encoding="utf-8"
            )
            (run_dir / "events.ndjson").write_text("{}\n", encoding="utf-8")
            (run_dir / "minute-ledger.ndjson").write_text("{}\n", encoding="utf-8")
            audit = {
                "schema_version": "codex-upgrade-supervisor/v1",
                "run_dir": str(run_dir),
                "event_count": 1,
                "watchdog_heartbeat_count": 1,
                "minute_record_count": 1,
                "classification_counts": {"failed": 1},
                "coverage_start_epoch": 1.0,
                "coverage_end_epoch": 2.0,
                "audit_incomplete": False,
                "integrity_errors": [],
                "state": "failed",
            }
            with mock.patch.object(
                codex_upgrade.codex_upgrade_supervisor,
                "_audit_command",
                return_value=audit,
            ):
                binding = codex_upgrade._control_replacement_supervisor_audit(
                    run_dir
                )
            self.assertEqual(binding["summary"], audit)
            self.assertEqual(binding["state"]["path"], "state.json")

            broken = dict(audit)
            broken["audit_incomplete"] = True
            with (
                mock.patch.object(
                    codex_upgrade.codex_upgrade_supervisor,
                    "_audit_command",
                    return_value=broken,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "audit_incomplete=false",
                ),
            ):
                codex_upgrade._control_replacement_supervisor_audit(run_dir)

    def test_control_replacement_context_preserves_source_and_zero_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_dir = root / "source"
            predecessor_dir = root / "control-refresh"
            source_dir.mkdir()
            predecessor_dir.mkdir()
            (source_dir / "campaign.json").write_text("{}\n", encoding="utf-8")
            (predecessor_dir / "campaign.json").write_text("{}\n", encoding="utf-8")
            epoch_path = (
                predecessor_dir
                / codex_upgrade.CONTROL_EPOCH_DIRECTORY
                / codex_upgrade.CONTROL_EPOCH_FILENAME
            )
            epoch_path.parent.mkdir()
            epoch_payload = {"receipt_sha256": "1" * 64}
            epoch_path.write_text(json.dumps(epoch_payload), encoding="utf-8")
            source = {
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "attempt_digest": "2" * 64,
                "candidate_identity_sha256": "3" * 64,
                "source_transition": {"path": "transition.json", "sha256": "4" * 64},
                "recovery_scope_sha256": "5" * 64,
                "planned_job_ids": ["job-a", "job-b"],
                "execute_job_ids": ["job-b"],
                "reused_job_ids": ["job-a"],
                "failed_job_ids": ["job-b"],
                "pending_job_ids": [],
                "production_paths": ["producer.sh"],
                "target_scenario": {"path": "inputs/target.json", "sha256": "6" * 64},
            }
            controls = {
                "upgrade_timing": {
                    "ledger_dir": "/control/effective",
                    "evidence_decision": "reuse",
                },
                "arm64_environment": {"marker": "arm"},
                "job_rehearsal": {"marker": "rehearsal"},
            }
            effective_epoch = {
                "source": source,
                "successor_controls": controls,
            }
            source_manifest = {"campaign_id": "source-campaign"}
            predecessor_manifest = {
                "baseline_version": "0.149.1",
                "target_version": "0.151.0",
                "campaign_purpose": "production_replacement",
                "predecessor": {
                    "campaign_dir": str(source_dir),
                    "campaign_id": "source-campaign",
                    "campaign_manifest_sha256": codex_upgrade.file_sha256(
                        source_dir / "campaign.json"
                    ),
                    "reason": "candidate_recovery_control_refresh",
                },
                "control_receipts": {
                    "upgrade_timing": {"ledger_dir": "/control/frozen"}
                },
            }
            source_attempt = {
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "path": "candidates/candidate-a/attempts/attempt-a/attempt.json",
                "sha256": "7" * 64,
                "attempt_digest": "2" * 64,
                "identity_sha256": "3" * 64,
                "status": "failed",
            }
            unpublished = {
                "ledger_dir": "/control/unpublished",
                "receipt": {"path": "stop.json", "sha256": "8" * 64, "bytes": 1},
                "upgrade_id": "unpublished",
                "evidence_decision": "reuse",
                "active_phase": "VC-4",
                "head_sequence": 1,
                "head_sha256": "9" * 64,
                "total_elapsed_seconds": 1,
                "total_live_request_count": 0,
                "total_deadline_at_utc": "2026-09-03T18:00:00Z",
            }
            audit = {"run_dir": str(root / "audit")}
            arguments = mock.Mock(
                reason="candidate_recovery_control_replacement",
                predecessor_control_epoch=epoch_path,
                predecessor_control_runtime_repair=None,
                predecessor_unpublished_ledger_dir=Path("/control/unpublished"),
                predecessor_unpublished_stop_receipt=Path("/control/unpublished/stop.json"),
                predecessor_supervisor_run_dir=root / "audit",
                predecessor_candidate_id="candidate-a",
                predecessor_attempt_id="attempt-a",
            )
            with (
                mock.patch.object(
                    codex_upgrade, "_control_epoch_files", return_value=[epoch_path]
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_control_epoch_receipt",
                    return_value=effective_epoch,
                ) as load_epoch,
                mock.patch.object(
                    codex_upgrade,
                    "_control_epoch_empty_boundary",
                    return_value=codex_upgrade._control_epoch_zero_boundary(),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=source_manifest,
                ) as load_source_manifest,
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=source_manifest,
                ) as require_source_campaign,
                mock.patch.object(
                    codex_upgrade,
                    "_successor_abandoned_attempt",
                    return_value=source_attempt,
                ) as load_source_attempt,
                mock.patch.object(
                    codex_upgrade,
                    "_control_replacement_stopped_ledger",
                    return_value=unpublished,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_control_replacement_supervisor_audit",
                    return_value=audit,
                ),
            ):
                context = codex_upgrade._control_replacement_context(
                    arguments,
                    predecessor_dir,
                    predecessor_manifest,
                )
            load_epoch.assert_called_once_with(
                predecessor_dir,
                predecessor_manifest,
                _allow_unrepaired_tool_drift=True,
            )
            load_source_manifest.assert_called_once_with(
                source_dir,
                _control_epoch_bootstrap=True,
            )
            require_source_campaign.assert_called_once_with(
                source_dir,
                source_manifest,
            )
            load_source_attempt.assert_called_once_with(
                source_dir,
                "candidate-a",
                "attempt-a",
                _historical_manifest_controls=True,
            )
            self.assertEqual(context["source"], source)
            self.assertEqual(context["source_attempt"], source_attempt)
            self.assertEqual(context["effective_controls"], controls)
            self.assertEqual(
                context["execution_summary"],
                {"executed_job_ids": [], "scanned_bytes": 0, "live_request_count": 0},
            )

    def test_frozen_manifest_controls_replay_without_override(self) -> None:
        """epoch 自举和替代后继都必须按清单原生路径重放冻结控制。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timing_root = root / "timing"
            arm_root = root / "arm"
            rehearsal_root = root / "rehearsal"
            for evidence_root in (timing_root, arm_root, rehearsal_root):
                evidence_root.mkdir(mode=0o700)
            ledger = timing_root / "ledger.json"
            timing_receipt = timing_root / "checkpoint.json"
            arm_receipt_path = arm_root / "receipt.json"
            rehearsal_receipt_path = rehearsal_root / "receipt.json"
            for path in (
                ledger,
                timing_receipt,
                arm_receipt_path,
                rehearsal_receipt_path,
            ):
                path.write_text("{}\n", encoding="utf-8")

            digest = "a" * 64
            timing = {
                "ledger_dir": str(timing_root),
                "ledger_plan_sha256": codex_upgrade.file_sha256(ledger),
                "receipt": {
                    "path": timing_receipt.name,
                    "sha256": codex_upgrade.file_sha256(timing_receipt),
                    "bytes": timing_receipt.stat().st_size,
                },
                "upgrade_id": "old-ledger",
                "evidence_decision": "reuse",
                "checkpoint_head_sha256": digest,
            }
            arm = {
                "evidence_root": str(arm_root),
                "receipt": {
                    "path": arm_receipt_path.name,
                    "sha256": codex_upgrade.file_sha256(arm_receipt_path),
                    "bytes": arm_receipt_path.stat().st_size,
                },
                "subject_id": "old-ledger",
                "contract_sha256": digest,
                "continuity_identity_sha256": digest,
            }
            rehearsal = {
                "evidence_root": str(rehearsal_root),
                "receipt": {
                    "path": rehearsal_receipt_path.name,
                    "sha256": codex_upgrade.file_sha256(rehearsal_receipt_path),
                    "bytes": rehearsal_receipt_path.stat().st_size,
                },
                "preflight_campaign_id": "old-preflight",
                "preflight_campaign_manifest_sha256": digest,
                "execution_contract_sha256": digest,
                "runtime_identity_sha256": digest,
                "job_count": 1,
                "job_set_sha256": digest,
            }
            controls = {
                "upgrade_timing": timing,
                "arm64_environment": arm,
                "job_rehearsal": rehearsal,
            }
            manifest = {
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "baseline_version": "0.149.1",
                "target_version": "0.151.0",
                "predecessor": {
                    "reason": "candidate_recovery_control_refresh",
                },
                "control_receipts": controls,
            }
            replayed_rehearsal = {
                "preflight_campaign": {
                    "campaign_id": "old-preflight",
                    "manifest_sha256": digest,
                },
                "execution_contract_sha256": digest,
                "job_count": 1,
                "job_set_sha256": digest,
            }
            with (
                mock.patch.object(
                    codex_upgrade, "_load_control_epoch_receipt", return_value=None
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_timing_ledger,
                    "replay",
                    return_value={
                        "summary": {
                            "upgrade_id": "old-ledger",
                            "evidence_decision": "reuse",
                            "head_sha256": digest,
                        }
                    },
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_arm64_environment_receipt,
                    "replay",
                    return_value={
                        "status": "passed",
                        "phase": "p0",
                        "subject_id": "old-ledger",
                        "contract_sha256": digest,
                        "continuity_identity_sha256": digest,
                    },
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_job_rehearsal_receipt,
                    "replay",
                    return_value=replayed_rehearsal,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_job_rehearsal_contract_from_manifest",
                    return_value={"schema_version": "fixture"},
                ) as contract_builder,
                mock.patch.object(
                    codex_upgrade,
                    "_assert_job_rehearsal_compatible",
                    return_value={"runtime_identity_sha256": digest},
                ),
            ):
                cases = (
                    ("candidate_recovery_control_refresh", True),
                    ("candidate_recovery_control_replacement", False),
                )
                for reason, historical in cases:
                    with self.subTest(reason=reason, historical=historical):
                        manifest["predecessor"]["reason"] = reason
                        codex_upgrade._verify_control_receipts(
                            root,
                            manifest,
                            require_active=False,
                            _historical_manifest_controls=historical,
                        )
                        self.assertIsNone(
                            contract_builder.call_args.kwargs[
                                "control_receipts_override"
                            ]
                        )

    def test_bootstrap_replays_base_epoch_with_historical_preflight_tool(self) -> None:
        """base epoch 自举时从已绑定 preflight 取回完整历史工具身份。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            target_path = campaign / "inputs" / "target.json"
            target_path.parent.mkdir(parents=True)
            target_path.write_text("{}\n", encoding="utf-8")
            old_digest = "a" * 64
            current_digest = "b" * 64
            historical_tool = {
                "files_sha256": old_digest,
                "components": {"orchestrator": {"sha256": "c" * 64}},
            }
            manifest = {
                "campaign_id": "formal-control-refresh",
                "campaign_mode": "formal",
                "target_version": "0.151.0",
                "target_sha256": "d" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                    },
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "e" * 64,
                        "code_mode_host_sha256": "f" * 64,
                    },
                },
                "configuration": {},
                "tool_identity": historical_tool,
                "predecessor": {"reason": "candidate_recovery_control_refresh"},
            }
            controls = {
                "job_rehearsal": {"execution_contract_sha256": old_digest},
            }
            rehearsal = {
                "preflight_campaign": {
                    "path": str(Path(directory) / "historical-preflight"),
                },
            }
            observed_manifest: dict[str, object] = {}

            def replay_preflight(
                _receipt: object,
                control_manifest: dict[str, object],
            ) -> tuple[Path, dict[str, object]]:
                observed_manifest.update(control_manifest)
                return Path(directory) / "historical-preflight", {}

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value={"files_sha256": current_digest},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value={"tool_identity": historical_tool},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_recovery_rehearsal_preflight_from_receipt",
                    side_effect=replay_preflight,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_recovery_rehearsal_target_scenario_override",
                    return_value=None,
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    side_effect=lambda **kwargs: {
                        "tool_files_sha256": kwargs["tool_files_sha256"]
                    },
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    side_effect=lambda contract: contract["tool_files_sha256"],
                ),
            ):
                contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                    campaign,
                    manifest,
                    tool_files_sha256_override=old_digest,
                    recovery_rehearsal_receipt=rehearsal,
                    control_receipts_override=controls,
                    _allow_historical_tool_identity=True,
                )

            self.assertEqual(contract["tool_files_sha256"], old_digest)
            self.assertEqual(observed_manifest["tool_identity"], historical_tool)

    def test_normal_epoch_replay_rejects_historical_tool_drift(self) -> None:
        """非 bootstrap 加载不得借历史 preflight 放宽当前工具漂移。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            target_path = campaign / "inputs" / "target.json"
            target_path.parent.mkdir(parents=True)
            target_path.write_text("{}\n", encoding="utf-8")
            old_digest = "a" * 64
            manifest = {
                "campaign_mode": "formal",
                "target_version": "0.151.0",
                "target_sha256": "d" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                    },
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "e" * 64,
                        "code_mode_host_sha256": "f" * 64,
                    },
                },
                "configuration": {},
                "tool_identity": {"files_sha256": old_digest},
                "predecessor": {"reason": "candidate_recovery_control_refresh"},
            }
            controls = {
                "job_rehearsal": {"execution_contract_sha256": old_digest},
            }
            rehearsal = {
                "preflight_campaign": {"path": str(Path(directory) / "old")},
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value={"files_sha256": "b" * 64},
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    side_effect=lambda **kwargs: {
                        "tool_files_sha256": kwargs["tool_files_sha256"]
                    },
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    side_effect=lambda contract: contract["tool_files_sha256"],
                ),
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "当前工具身份",
                ):
                    codex_upgrade._job_rehearsal_contract_from_manifest(
                        campaign,
                        manifest,
                        tool_files_sha256_override=old_digest,
                        recovery_rehearsal_receipt=rehearsal,
                        control_receipts_override=controls,
                    )

    def test_runtime_repair_replays_and_replaces_only_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            path, payload, current_invariants = self._add_runtime_repair(
                campaign,
                manifest,
                context,
            )
            loaded = self._load(
                campaign,
                manifest,
                context,
                current_invariants=current_invariants,
            )
            self.assertEqual(
                loaded["runtime_repair"]["receipt_sha256"],
                payload["receipt_sha256"],
            )
            self.assertEqual(
                loaded["successor_controls"]["upgrade_timing"],
                context["successor_controls"]["upgrade_timing"],
            )
            self.assertEqual(loaded["invariants"], current_invariants)
            self.assertEqual(loaded["runtime_repair"]["sha256"], codex_upgrade.file_sha256(path))

    def test_runtime_repair_rejects_control_budget_and_invariant_drift(self) -> None:
        mutations = (
            ("Ledger", lambda payload: payload["successor_controls"].update({
                "upgrade_timing": {"ledger_dir": "/control/other", "upgrade_id": "other"}
            }), "只能替换"),
            ("P0", lambda payload: payload["successor_controls"].update({
                "arm64_environment": {"marker": "other-arm"}
            }), "只能替换"),
            ("预算", lambda payload: payload["budget"].update({
                "cumulative_elapsed_seconds": 121
            }), "改变了既有预算"),
            ("源闭集", lambda payload: payload["source"].update({
                "execute_job_ids": ["candidate-core-mitm"]
            }), "闭集"),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                campaign, manifest, context = self._fixture(Path(directory))
                path, payload, current_invariants = self._add_runtime_repair(
                    campaign,
                    manifest,
                    context,
                )
                mutate(payload)
                core = dict(payload)
                core.pop("receipt_sha256")
                payload["receipt_sha256"] = codex_upgrade._fingerprint(core)
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, message):
                    self._load(
                        campaign,
                        manifest,
                        context,
                        current_invariants=current_invariants,
                    )

        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            _path, _payload, current_invariants = self._add_runtime_repair(
                campaign,
                manifest,
                context,
            )
            current_invariants["tool_production_sha256"] = "c" * 64
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "产出、compose"):
                self._load(
                    campaign,
                    manifest,
                    context,
                    current_invariants=current_invariants,
                )
            loaded = self._load(
                campaign,
                manifest,
                context,
                current_invariants=current_invariants,
                allow_unrepaired_tool_drift=True,
            )
            self.assertEqual(
                loaded["runtime_repair"]["receipt_sha256"],
                _payload["receipt_sha256"],
            )
            manifest["predecessor"] = {
                "reason": "candidate_recovery_control_replacement"
            }
            authorized = self._load(
                campaign,
                manifest,
                context,
                current_invariants=current_invariants,
                allowed_production_paths={
                    "run_sub2api_openai_mitm_matrix.sh"
                },
            )
            self.assertEqual(
                authorized["runtime_repair"]["receipt_sha256"],
                _payload["receipt_sha256"],
            )

    def test_runtime_repair_allows_one_amendment_and_rejects_third(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign, manifest, context = self._fixture(Path(directory))
            manifest["predecessor"] = {
                "reason": "candidate_recovery_control_replacement"
            }
            path, payload, current_invariants = (
                self._add_runtime_repair_amendment(
                    campaign,
                    manifest,
                    context,
                )
            )
            loaded = self._load(
                campaign,
                manifest,
                context,
                current_invariants=current_invariants,
            )
            self.assertEqual(
                loaded["runtime_repair"]["receipt_sha256"],
                payload["receipt_sha256"],
            )
            self.assertEqual(
                loaded["runtime_repair"]["sha256"],
                codex_upgrade.file_sha256(path),
            )
            later_invariants = {
                **current_invariants,
                "tool_files_sha256": "d" * 64,
            }
            replayed = self._load(
                campaign,
                manifest,
                context,
                current_invariants=later_invariants,
            )
            self.assertEqual(
                replayed["invariants"]["tool_files_sha256"],
                later_invariants["tool_files_sha256"],
            )
            repair_payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                repair_payload["invariants"]["tool_files_sha256"],
                current_invariants["tool_files_sha256"],
            )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "禁止第三份"):
                codex_upgrade._create_control_epoch_runtime_repair(
                    mock.Mock(),
                    campaign,
                    manifest,
                )

    def test_plan_identity_consumes_runtime_repaired_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "campaign"
            epoch_path = codex_upgrade._control_epoch_path(campaign)
            epoch_path.parent.mkdir(parents=True)
            epoch_path.write_text("{}\n", encoding="utf-8")
            source_root = (
                root
                / "predecessor"
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "attempt-a"
            )
            source_root.mkdir(parents=True)
            predecessor_manifest = root / "predecessor" / "campaign.json"
            predecessor_manifest.write_text(
                json.dumps({"campaign_id": "predecessor"}) + "\n",
                encoding="utf-8",
            )
            target_source = root / "target-source"
            target_source.mkdir()
            (target_source / "Cargo.lock").write_text("lock\n", encoding="utf-8")
            target_package = root / "codex-package.tar"
            target_package.write_text("package\n", encoding="utf-8")
            expected_tool = {"files_sha256": "6" * 64}
            current_tool = {"files_sha256": "b" * 64}
            source = {
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "attempt_digest": "1" * 64,
                "planned_job_ids": ["job-a", "job-b"],
                "execute_job_ids": ["job-b"],
                "reused_job_ids": ["job-a"],
            }
            epoch = {
                "receipt_sha256": "2" * 64,
                "source": source,
                "invariants": {"tool_files_sha256": "b" * 64},
                "runtime_repair": {"receipt_sha256": "3" * 64},
            }
            package_identity = {
                "asset_sha256": "4" * 64,
                "code_mode_host_sha256": "5" * 64,
            }
            manifest = {
                "campaign_id": "formal-control-refresh",
                "predecessor": {
                    "campaign_dir": str((root / "predecessor").resolve()),
                    "campaign_id": "predecessor",
                    "campaign_manifest_sha256": codex_upgrade.file_sha256(
                        predecessor_manifest
                    ),
                    "reason": "candidate_recovery_control_refresh",
                },
                "configuration": {
                    "target_source": str(target_source),
                    "target_package": str(target_package),
                },
                "official_identity": {
                    "source_tree_sha256": "source-tree",
                    "cargo_lock_sha256": codex_upgrade.file_sha256(
                        target_source / "Cargo.lock"
                    ),
                    "package": package_identity,
                },
                "target_version": "0.151.0",
                "target_sha256": "7" * 64,
                "tool_identity": expected_tool,
            }
            attempt = {
                "campaign_id": "predecessor",
                "phase": "candidate",
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "attempt_digest": "1" * 64,
            }
            with (
                mock.patch.object(codex_upgrade, "_verify_control_receipts"),
                mock.patch.object(
                    codex_upgrade,
                    "_directory_tree_digest",
                    return_value="source-tree",
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_codex_package",
                    return_value=package_identity,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=current_tool,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_component_drift",
                    return_value={
                        "changed_components": ["orchestrator"],
                        "changed_paths": {"orchestrator": ["codex_upgrade.py"]},
                    },
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_control_epoch_receipt",
                    return_value=epoch,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_component_bundle",
                    return_value={},
                ),
            ):
                impact = codex_upgrade._verify_plan_identity(
                    campaign,
                    manifest,
                    operation="capture-run",
                    attempt_root=source_root,
                    attempt=attempt,
                )
        self.assertEqual(impact["kind"], "control_epoch")
        self.assertEqual(impact["affected_job_ids"], ["job-b"])

    def _c0154_vc5_epoch_source(self) -> dict[str, object]:
        """构造唯一 VC-5 控制续期的冻结 9/8/1 来源。"""

        planned = list(
            codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH[
                "planned_job_ids"
            ]
        )
        execute = [
            str(
                codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH[
                    "execute_job_id"
                ]
            )
        ]
        return {
            "candidate_id": codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH[
                "candidate_id"
            ],
            "attempt_id": codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH[
                "attempt_id"
            ],
            "attempt_digest": "1" * 64,
            "candidate_identity_sha256": "2" * 64,
            "source_transition": {
                "path": "predecessor-import.json",
                "sha256": "3" * 64,
            },
            "recovery_scope_sha256": "4" * 64,
            "planned_job_ids": planned,
            "execute_job_ids": execute,
            "reused_job_ids": sorted(set(planned) - set(execute)),
            "failed_job_ids": execute,
            "pending_job_ids": [],
            "production_paths": [
                codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH[
                    "production_path"
                ]
            ],
            "target_scenario": {
                "path": "inputs/target.json",
                "sha256": "5" * 64,
            },
        }

    def test_c0154_vc5_epoch_identity_is_exact(self) -> None:
        """目录、ID、摘要、版本和 reason 任一漂移都不得获得例外。"""

        identity = codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / str(identity["campaign_id"])
            campaign.mkdir()
            manifest = {
                "campaign_id": identity["campaign_id"],
                "campaign_mode": "formal",
                "target_version": identity["target_version"],
                "predecessor": {"reason": identity["predecessor_reason"]},
            }
            manifest_path = campaign / "campaign.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            digest = codex_upgrade.file_sha256(manifest_path)
            with mock.patch.dict(
                identity,
                {"campaign_manifest_sha256": digest},
            ):
                self.assertTrue(
                    codex_upgrade._is_c0154_vc5_failed_job_control_epoch_campaign(
                        campaign,
                        manifest,
                    )
                )
                self.assertFalse(
                    codex_upgrade._is_c0154_vc5_failed_job_control_epoch_campaign(
                        campaign,
                        {**manifest, "campaign_id": "wrong-campaign"},
                    )
                )
                self.assertFalse(
                    codex_upgrade._is_c0154_vc5_failed_job_control_epoch_campaign(
                        campaign,
                        {
                            **manifest,
                            "predecessor": {"reason": "wrong-reason"},
                        },
                    )
                )
            self.assertFalse(
                codex_upgrade._is_c0154_vc5_failed_job_control_epoch_campaign(
                    campaign,
                    manifest,
                )
            )

    def test_c0154_vc5_epoch_source_is_exact_9_8_1(self) -> None:
        """仅接受 9 planned、8 reused、A15 单执行且 pending 为空。"""

        identity = codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / str(identity["campaign_id"])
            predecessor = root / "predecessor"
            campaign.mkdir()
            predecessor.mkdir()
            (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
            (campaign / "predecessor-import.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            manifest = {
                "campaign_id": identity["campaign_id"],
                "campaign_mode": "formal",
                "target_version": identity["target_version"],
                "predecessor": {
                    "reason": identity["predecessor_reason"],
                    "campaign_dir": str(predecessor.resolve()),
                },
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                        "sha256": "5" * 64,
                    }
                },
            }
            source = self._c0154_vc5_epoch_source()
            scope = {
                key: source[key]
                for key in (
                    "planned_job_ids",
                    "reused_job_ids",
                    "execute_job_ids",
                    "failed_job_ids",
                    "pending_job_ids",
                )
            }
            scope.update(
                reservation_exists=False,
                live_request_count=0,
                scanned_bytes=0,
            )
            abandoned = {
                "attempt_digest": "1" * 64,
                "identity_sha256": "2" * 64,
            }

            def build(observed_scope: dict[str, object]) -> dict[str, object]:
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_is_c0154_vc5_failed_job_control_epoch_campaign",
                        return_value=True,
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_published_c0154_v7_recovery_coordinates",
                        return_value=(
                            identity["candidate_id"],
                            identity["attempt_id"],
                        ),
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_require_c0154_v7_recovery_source_binding",
                        return_value={
                            "predecessor_manifest": {"campaign_id": "source"},
                            "abandoned_candidate_attempt": abandoned,
                        },
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_validate_failed_job_tool_recovery_source",
                        return_value=observed_scope,
                    ),
                ):
                    return (
                        codex_upgrade._c0154_vc5_failed_job_control_epoch_source_context(
                            campaign,
                            manifest,
                            candidate_id=str(identity["candidate_id"]),
                            attempt_id=str(identity["attempt_id"]),
                        )
                    )

            observed = build(scope)
            self.assertEqual(len(observed["planned_job_ids"]), 9)
            self.assertEqual(len(observed["reused_job_ids"]), 8)
            self.assertEqual(
                observed["execute_job_ids"],
                ["candidate-frozen-core"],
            )
            self.assertEqual(observed["pending_job_ids"], [])
            for field, value in (
                ("execute_job_ids", ["candidate-core-mitm", "candidate-frozen-core"]),
                ("reused_job_ids", list(scope["reused_job_ids"])[1:]),
                ("pending_job_ids", ["candidate-frozen-core"]),
            ):
                with self.subTest(field=field):
                    changed = json.loads(json.dumps(scope))
                    changed[field] = value
                    with self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "9/8/1",
                    ):
                        build(changed)

    def test_c0154_vc5_epoch_uses_vc0_stop_and_vc4_successor(self) -> None:
        """旧 Ledger 只允许 VC-0 stop；新 Ledger 仍必须从 VC-4 继续。"""

        identity = codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH
        manifest = {
            "campaign_id": identity["campaign_id"],
            "target_version": identity["target_version"],
            "predecessor": {"reason": identity["predecessor_reason"]},
        }
        source = self._c0154_vc5_epoch_source()
        self.assertEqual(
            codex_upgrade._control_epoch_stop_required_phase(manifest, source),
            "VC-0",
        )
        self.assertEqual(codex_upgrade._control_epoch_required_phase(source), "VC-4")
        changed = {**source, "pending_job_ids": ["candidate-frozen-core"]}
        self.assertEqual(
            codex_upgrade._control_epoch_stop_required_phase(manifest, changed),
            "VC-4",
        )

    def test_c0154_vc5_epoch_legacy_boundary_is_single_object_only(self) -> None:
        """直接入口仅豁免精确对象；campaign-run 和其他 Formal 仍拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            campaign.mkdir()
            (campaign / "campaign.json").write_text(
                json.dumps(
                    {
                        "campaign_mode": "formal",
                        "target_version": "0.154.0",
                    }
                ),
                encoding="utf-8",
            )
            arguments = type(
                "Arguments",
                (),
                {
                    "campaign_dir": campaign,
                    "predecessor_campaign_dir": None,
                },
            )()
            with (
                mock.patch.object(codex_upgrade.os, "environ", {}),
                mock.patch.object(
                    codex_upgrade,
                    "_is_c0154_vc5_failed_job_control_epoch_campaign",
                    return_value=True,
                ),
            ):
                codex_upgrade._reject_campaign_run_legacy_write(
                    arguments,
                    "control-epoch",
                )
            with (
                mock.patch.object(codex_upgrade.os, "environ", {}),
                mock.patch.object(
                    codex_upgrade,
                    "_is_c0154_vc5_failed_job_control_epoch_campaign",
                    return_value=False,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "正式 Campaign 禁止旧写入入口",
                ),
            ):
                codex_upgrade._reject_campaign_run_legacy_write(
                    arguments,
                    "control-epoch",
                )
            with (
                mock.patch.object(
                    codex_upgrade.os,
                    "environ",
                    {codex_upgrade.codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"},
                ),
                self.assertRaises(codex_upgrade.ConfigurationError),
            ):
                codex_upgrade._reject_campaign_run_legacy_write(
                    arguments,
                    "control-epoch",
                )

    def test_campaign_timing_ledger_prefers_effective_epoch_controls(self) -> None:
        """批次治理必须消费 epoch 的新 Ledger；无 epoch 时仍读取清单。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "campaign"
            old = root / "old-ledger"
            new = root / "new-ledger"
            campaign.mkdir()
            old.mkdir()
            new.mkdir()
            for ledger, marker in ((old, "old"), (new, "new")):
                (ledger / "ledger.json").write_text(marker, encoding="utf-8")
            manifest = {
                "control_receipts": {
                    "upgrade_timing": {
                        "ledger_dir": str(old.resolve()),
                        "ledger_plan_sha256": codex_upgrade.file_sha256(
                            old / "ledger.json"
                        ),
                    }
                }
            }
            effective = {
                "successor_controls": {
                    "upgrade_timing": {
                        "ledger_dir": str(new.resolve()),
                        "ledger_plan_sha256": codex_upgrade.file_sha256(
                            new / "ledger.json"
                        ),
                    }
                }
            }
            with mock.patch.object(
                codex_upgrade,
                "_load_control_epoch_receipt",
                return_value=effective,
            ):
                self.assertEqual(
                    codex_upgrade._campaign_timing_ledger_dir(
                        campaign,
                        manifest,
                    ),
                    new.resolve(),
                )
            with mock.patch.object(
                codex_upgrade,
                "_load_control_epoch_receipt",
                return_value=None,
            ):
                self.assertEqual(
                    codex_upgrade._campaign_timing_ledger_dir(
                        campaign,
                        manifest,
                    ),
                    old.resolve(),
                )

    def test_c0154_vc5_epoch_preserves_completed_vc0_controls(self) -> None:
        """控制续期复用既有 P0／发布认证，不把 VC-0～VC-4 重新执行。"""

        frozen = {
            "p0_gate": {"receipt_digest": "1" * 64},
            "release_certification": {"receipt_sha256": "2" * 64},
        }
        manifest = {
            "target_version": "0.154.0",
            "tool_identity": {"policy_version": 6},
            "control_receipts": frozen,
        }
        controls = {
            "upgrade_timing": {"marker": "new"},
            "arm64_environment": {"marker": "new"},
        }
        with mock.patch.object(
            codex_upgrade,
            "_is_c0154_vc5_failed_job_control_epoch_campaign",
            return_value=True,
        ):
            codex_upgrade._preserve_c0154_vc0_control_receipts(
                Path("/campaign"),
                manifest,
                controls,
            )
        self.assertEqual(controls["p0_gate"], frozen["p0_gate"])
        self.assertEqual(
            controls["release_certification"],
            frozen["release_certification"],
        )
        self.assertIsNot(controls["p0_gate"], frozen["p0_gate"])

    def test_c0154_vc5_epoch_rejects_repair_or_second_epoch(self) -> None:
        """精确例外不得扩张为 runtime repair 或第二份 epoch。"""

        identity = codex_upgrade.C0154_VC5_FAILED_JOB_CONTROL_EPOCH
        manifest = {
            "campaign_id": identity["campaign_id"],
            "target_version": identity["target_version"],
            "predecessor": {"reason": identity["predecessor_reason"]},
        }
        source = self._c0154_vc5_epoch_source()
        for extra_name in (
            codex_upgrade.CONTROL_EPOCH_RUNTIME_REPAIR_FILENAME,
            "control-epoch-02.json",
        ):
            with self.subTest(extra_name=extra_name), tempfile.TemporaryDirectory() as directory:
                campaign = Path(directory) / "campaign"
                epoch_root = campaign / codex_upgrade.CONTROL_EPOCH_DIRECTORY
                epoch_root.mkdir(parents=True)
                (epoch_root / codex_upgrade.CONTROL_EPOCH_FILENAME).write_text(
                    "{}\n",
                    encoding="utf-8",
                )
                (epoch_root / extra_name).write_text("{}\n", encoding="utf-8")
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_control_epoch_source_context",
                        return_value=source,
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_is_c0154_vc5_failed_job_control_epoch_campaign",
                        return_value=True,
                    ),
                    self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "禁止 runtime repair 或第二纪元",
                    ),
                ):
                    codex_upgrade._load_control_epoch_receipt(
                        campaign,
                        manifest,
                    )

    def test_c0154_vc5_epoch_rejects_non_vc0_or_live_stop(self) -> None:
        """旧 Ledger 必须恰为 VC-0 stopped 且累计 live 为零。"""

        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger"
            ledger.mkdir()
            receipt = ledger / "stop.json"
            receipt.write_text("{}\n", encoding="utf-8")
            timing = {
                "ledger_dir": str(ledger.resolve()),
                "upgrade_id": "vc5-control-renewal",
                "evidence_decision": "reuse",
            }
            manifest = {"control_receipts": {"upgrade_timing": timing}}
            base = {
                "status": "stopped",
                "active_phase": "VC-0",
                "upgrade_id": "vc5-control-renewal",
                "evidence_decision": "reuse",
                "head_sequence": 2,
                "head_sha256": "a" * 64,
                "total_elapsed_seconds": 60,
                "total_live_request_count": 0,
                "total_deadline_at_utc": "2026-09-17T11:07:42+00:00",
            }
            for field, value in (
                ("active_phase", "VC-4"),
                ("total_live_request_count", 1),
            ):
                with self.subTest(field=field):
                    summary = {**base, field: value}
                    with (
                        mock.patch.object(
                            codex_upgrade.codex_upgrade_timing_ledger,
                            "replay",
                            return_value={"summary": summary},
                        ),
                        mock.patch.object(
                            codex_upgrade.codex_upgrade_timing_ledger,
                            "inspect_ledger",
                            return_value=summary,
                        ),
                        self.assertRaisesRegex(
                            codex_upgrade.ConfigurationError,
                            "当前 VC-0 停线终态 head",
                        ),
                    ):
                        codex_upgrade._control_epoch_stop_checkpoint(
                            manifest,
                            ledger,
                            receipt,
                            expected_timing=timing,
                            required_phase="VC-0",
                        )

    def test_control_epoch_rejects_every_nonzero_boundary_counter(self) -> None:
        """reservation／attempt／checkpoint／live／scan 任一非零均失败关闭。"""

        for field in codex_upgrade._control_epoch_zero_boundary():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                campaign, manifest, context = self._fixture(Path(directory))
                path = codex_upgrade._control_epoch_path(campaign)
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["boundary"][field] = 1
                unsigned = dict(payload)
                unsigned.pop("receipt_sha256")
                payload["receipt_sha256"] = codex_upgrade._fingerprint(unsigned)
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "发布边界",
                ):
                    self._load(campaign, manifest, context)


if __name__ == "__main__":
    unittest.main()
