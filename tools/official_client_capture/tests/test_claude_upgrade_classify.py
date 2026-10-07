"""Claude VC-2 规则分类 producer（Claude 升级控制面设计 B3-02）。

全部离线、零请求。夹具直接写出一个已完成 VC-1 封存的 Claude Campaign：VC-0／VC-1 checkpoint、``official/result.json``、
受管 Store（Campaign v2、Discovery／Evidence 两条事实、EvidencePackage v3 与完整性清单），完整性清单绑定的
DiscoveryInventory、CaptureIndex、跨来源矩阵、SinkInventory 与冻结的目标场景清单，以及每个 probe 一份带 FW-F v5 维度
证据的中继清单（三个只记录维度按场景清单落在对应 probe 上）。基线取真实的 ``claude_required_rules_2_1_226.json``。

* 机器草案：分母由输入计算；迁移决定与证据等级全部 ``blocked``、终态绑定全部为空；同输入重跑复用同一 revision。
* v5 只记录事实：逐条识别并带出是否出现、请求计数与相关规则；缺 v5 维度证据或缺对应维度的事实即失败关闭；未出现的事实
  不得绑定规则。
* 修订：新 revision 只写一次、同内容复用；改只读字段、原地改 revision 文件、错用终态都被门禁或加载拒绝。
* 批准：两份台账封存、分类事实写入、Store 仍是 ``evidence_recorded``、阶段结果与 VC-2 checkpoint；批准在分类事实写入后被
  SIGKILL，重跑收敛且事实数不增；再批准逐字复用，批准其他 revision、再起草案、输入漂移一律拒绝；change／delete 路径的
  两个规则集合与补丁计划。
* 分类流程不进 wire／evidence 闭包。
* 设置 ``CLAUDE_2_1_226_CLASSIFY_FIXTURE`` 指向含 2.1.226 历史 ``closure.json``、``discovery-inventory.json`` 与
  ``candidate-denominator.json`` 副本的目录时，另验收草案分母等于历史清零收据（7368／593／110／40）。
"""

from __future__ import annotations

import collections
import contextlib
import copy
import hashlib
import io
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_classify as classify
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import claude_upgrade_seal as seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture.capturelib import claude_fw_f_v5
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
from tools.official_client_control.contracts import derive_identity_conditions
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
TARGET = "2.1.285"
BASELINE = "2.1.226"
CAMPAIGN_ID = "claude-code-2_1_285-c1"
FIXTURE_ENV = "CLAUDE_2_1_226_CLASSIFY_FIXTURE"
# 只记录维度在夹具里的观测结果：两个灰度 Header 未出现（合法零流量），/usage 用量查询出现。
OBSERVED = {"header.usage_limit": False, "header.dispatch_id": False, "aux.usage": True}
STAGE_TIME = "2026-10-07T01:00:00Z"

DISCOVERY_INVENTORY = {
    "schema_version": evidence.DISCOVERY_INVENTORY_SCHEMA,
    "target_version": TARGET,
    "item_count": 3,
    "counts_by_source_kind": {"hitcc_document_atom_2_1_197": 1, "target_ast_call": 1, "target_unresolved_entry": 1},
    "counts_by_disposition": {"catalogued_context": 1, "mapped_semantic_candidate": 1, "out_of_scope_proven": 1},
    "items": [
        {
            "discovery_id": "HDOC-0001", "source_kind": "hitcc_document_atom_2_1_197", "proposition": "文档导航链接。",
            "disposition": "catalogued_context", "semantic_candidate_ids": [], "spec_ids": [], "evidence_paths": ["docs/a.md"],
            "rationale": "历史上下文。",
        },
        {
            "discovery_id": "TN-SINK-0001-1", "source_kind": "target_ast_call", "proposition": "目标 bundle 的推理请求调用点。",
            "disposition": "mapped_semantic_candidate", "semantic_candidate_ids": ["CAND-ALPHA"], "spec_ids": ["SPEC-BODY-001"],
            "evidence_paths": ["analysis/target-inventory.json"], "rationale": "归并到语义候选。",
        },
        {
            "discovery_id": "UNRESOLVED-EDGE-0001", "source_kind": "target_unresolved_entry", "proposition": "动态 require 边。",
            "disposition": "out_of_scope_proven", "semantic_candidate_ids": [], "spec_ids": [], "evidence_paths": ["analysis/target-inventory.json"],
            "rationale": "运行证据确认只指向已登记模块。",
        },
    ],
    "rule_generation": "forbidden",
}
CANDIDATES = {
    "schema_version": classify.CANDIDATE_DENOMINATOR_SCHEMA,
    "target_version": TARGET,
    "candidate_groups": {
        "semantic_candidate_families": [
            {"candidate_id": "CAND-ALPHA", "proposition": "推理请求 Body 合同。", "source_ids": ["TN-SINK-0001-1"]},
        ],
        "target_send_points": [
            {"candidate_id": "TN-SINK-0001-1", "proposition": "目标发送点。", "semantic_candidate_ids": ["CAND-ALPHA"]},
        ],
    },
    "counts": {"semantic_candidate_families": 1, "target_send_points": 1},
    "total_orthogonal_candidates": 2,
}


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def _write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _binding(path: Path, base: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": path.relative_to(base).as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _scenarios(version: str) -> dict[str, Any]:
    """真实 2.1.285 目标场景清单（全部 79 个 probe）；版本不同时只改 ``claude_version``（2.1.226 夹具用）。"""

    payload = json.loads(plan.scenario_manifest_path(TARGET).read_text(encoding="utf-8"))
    payload["claude_version"] = version
    return payload


def run_main(argv: list[str]) -> tuple[int, dict | None, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main(argv)
    text = stdout.getvalue()
    return code, (json.loads(text) if text.strip() else None), stderr.getvalue()


class _ClassifyCase(unittest.TestCase):
    """临时宿主数据根 + 受管工具树副本（Store 有效工具身份）+ 已完成 VC-1 封存的 Claude Campaign 写入器。"""

    tree: ManagedToolTree
    tool_identity: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool_identity = claude_upgrade._tool_identity(include_git=False)
        cls._tree_directory = tempfile.TemporaryDirectory()
        cls.tree = ManagedToolTree(Path(cls._tree_directory.name) / "repo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tree_directory.cleanup()

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        (self.data / "evidence" / "campaigns").mkdir(parents=True, mode=0o700)
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)

    # -- Campaign 夹具 ---------------------------------------------------------

    def build_campaign(
        self,
        *,
        target: str = TARGET,
        campaign_id: str = CAMPAIGN_ID,
        discovery: bytes | None = None,
        candidates: bytes | None = None,
        relay_overrides: dict[str, Any] | None = None,
        control_receipts: dict[str, Any] | None = None,
        wire_files: Mapping[str, Sequence[tuple[str, bytes, str, int | None]]] | None = None,
    ) -> Path:
        """``wire_files``（B3-03 补充项起）：probe → [(probe 目录内相对路径, 字节, 标签 kind, 有效连接序号)]，写进 VC-1 证据
        目录、登记进 capture manifest 并绑定到该 probe 的证据项（Wire 投影读它们）；缺省不写，B3-02 的用例不受影响。"""

        scenarios = _scenarios(target)
        scratch = self.root / f"store-build-{campaign_id}"
        scratch.mkdir()
        fixture = SyntheticCampaign(scratch, schema="v2")
        fixture.campaign_id = campaign_id
        fixture.bootstrap_and_campaign(
            target_version=target,
            platforms=["linux/arm64"],
            entrypoints=["claude-cli"],
            default_conditions=["privacy=default"],
            campaign_purpose="validation_only",
            absolute_deadline_utc="2026-12-31T00:00:00Z",
        )
        derived = derive_identity_conditions(fixture.campaign_document)[0]
        campaign = self.data / "evidence" / "campaigns" / campaign_id
        frozen = {
            "target_scenarios": _write_json(campaign / "inputs" / "target-scenarios.json", scenarios),
            "store_bootstrap": _write_json(
                campaign / "inputs" / "store-bootstrap.json", fixture.store.load_object(fixture.references["bootstrap"])["payload"]
            ),
            "store_campaign": _write_json(campaign / "inputs" / "store-campaign.json", fixture.campaign_document),
        }
        rules = list(plan.load_baseline_rules(plan.baseline_rule_manifest_path(BASELINE), BASELINE))
        manifest = {
            "schema_version": "official-client-upgrade-campaign/v1", "client": "claude", "campaign_id": campaign_id,
            "created_at_utc": "2026-10-07T00:00:00Z", "campaign_mode": "formal", "campaign_purpose": "validation_only",
            "baseline_version": BASELINE, "target_version": target, "target_sha256": "a" * 64,
            "official_identity": {"npm": {}, "gcs": {}, "binary_sha256": "a" * 64, "binary_equivalence": "npm=gcs"},
            # 控制收据缺省为空；候选级动作（B3-04）的夹具在这里绑定 Claude 计时账本。
            "baseline_identity": {}, "tool_identity": self.tool_identity, "control_receipts": dict(control_receipts or {}),
            "inputs": {name: {"path": path.relative_to(campaign).as_posix(), "sha256": claude_upgrade.file_sha256(path)} for name, path in frozen.items()},
            "configuration": {
                "platform": "linux/arm64",
                **{key: derived[key] for key in ("entrypoint", "privacy_mode", "oauth_org_id", "model_catalog_sha256")},
            },
            "required_rules": rules, "coverage_plan": {}, "jobs": [], "vc_control": {}, "predecessor": None,
        }
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        _write_bytes(campaign / "campaign.json", raw)
        (campaign / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
        seal_tests.write_vc_control(campaign)
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        store, store_campaign = seal._ensure_store_campaign(campaign, manifest)
        tool_sha = seal._require_store_tool_current(store, store_campaign)

        # VC-1 封存对象（路径都在宿主数据根内，完整性清单按数据根相对路径绑定）。
        drafts = campaign / "official" / "seal" / "g-fixture" / "drafts"
        discovery_path = _write_bytes(
            drafts / "discovery-inventory.json",
            discovery if discovery is not None else json.dumps(DISCOVERY_INVENTORY, ensure_ascii=False).encode("utf-8"),
        )
        probes = sorted({probe for scenario in scenarios["evidence_scenarios"] for probe in scenario["probe_ids"]})
        probe_scenarios = {
            probe: sorted(item["scenario_id"] for item in scenarios["evidence_scenarios"] if probe in item["probe_ids"]) for probe in probes
        }
        relay_overrides = relay_overrides or {}
        relays: dict[str, Path] = {}
        for probe in probes:
            dimensions = [scenario for scenario in probe_scenarios[probe] if scenario in claude_fw_f_v5.OBSERVED_ONLY_DIMENSIONS]
            dimension_evidence = relay_overrides.get(
                probe,
                {
                    "schema_version": claude_fw_f_v5.SCHEMA_DIMENSION_EVIDENCE,
                    "probe_id": probe,
                    "observed_only": [self.observed_entry(dimension) for dimension in dimensions],
                },
            )
            relays[probe] = _write_json(
                campaign / "official" / "attempts" / "attempt-fixture" / "evidence" / probe / "relay-manifest.json",
                {"schema_version": evidence.RELAY_MANIFEST_SCHEMA, "probe_id": probe, "status": "complete", "dimension_evidence": dimension_evidence},
            )
        matrix_rows = []
        for spec_id in rules:
            covering = sorted(item["scenario_id"] for item in scenarios["evidence_scenarios"] if spec_id in item["covers"])
            covering_probes = sorted({probe for item in scenarios["evidence_scenarios"] if item["scenario_id"] in covering for probe in item["probe_ids"]})
            matrix_rows.append(
                {
                    "spec_id": spec_id, "scenario_ids": covering, "probe_ids": covering_probes,
                    "evidence_ids": [f"EVIDENCE-{probe}" for probe in covering_probes],
                    "status": "covered" if covering_probes else "gap",
                }
            )
        evidence_dir = campaign / "official" / "attempts" / "attempt-fixture" / "evidence"
        wire_bindings: dict[str, list[dict[str, Any]]] = {}
        wire_artifacts: list[dict[str, Any]] = []
        for probe, files in sorted((wire_files or {}).items()):
            for relative, data, kind, ordinal in files:
                path = _write_bytes(evidence_dir / probe / relative, data)
                wire_bindings.setdefault(probe, []).append(_binding(path, self.data))
                artifact = {
                    "job_id": "fixture", "probe_id": probe, "path": path.relative_to(self.data).as_posix(), "kind": kind,
                    "parser": "opaque_bound_source", "labels": {"probe": probe}, "scenario_ids": [],
                }
                if ordinal is not None:
                    artifact["connection"] = {"ordinal": ordinal, "connection_id": ordinal}
                wire_artifacts.append(artifact)
        static = {
            "capture_manifest": _write_json(
                drafts / "capture-manifest.json",
                {
                    "schema_version": evidence.CAPTURE_MANIFEST_SCHEMA, "attempt": {"attempt_id": "attempt-fixture"},
                    "artifacts": wire_artifacts, "label_mismatches": [],
                },
            ),
            "capture_index": _write_json(drafts / "capture-index.json", {"schema_version": evidence.CAPTURE_INDEX_SCHEMA, "result": "passed"}),
            "discovery_inventory": discovery_path,
            "cross_source_matrix": _write_json(
                drafts / "cross-source-matrix.json",
                {
                    "schema_version": evidence.CROSS_SOURCE_MATRIX_SCHEMA, "campaign_id": campaign_id, "target_version": target,
                    "rules": matrix_rows, "rule_gaps": [], "closure": {"status": "closed", "reasons": []},
                },
            ),
            "target_inventory": _write_json(
                campaign / "analysis" / "target-inventory.json",
                {"schema_version": "claude-code-target-sink-inventory/v1", "target_version": target, "sinks": [], "unresolved_entries": []},
            ),
            "target_scenarios": frozen["target_scenarios"],
        }
        persona = dict(store_campaign["persona"])
        bindings = sorted(((evidence.COMPLETENESS_PURPOSES[name], _binding(path, self.data)) for name, path in static.items()), key=lambda pair: pair[1]["path"])
        completeness = {
            "schema_version": evidence.OPERATIONAL_EVIDENCE_SCHEMA,
            "persona": persona,
            "manifest_id": evidence.COMPLETENESS_MANIFEST_ID,
            "entries": [
                {"id": f"binding-{index:04d}", "facts": {"binding": binding, "purpose": purpose}}
                for index, (purpose, binding) in enumerate(bindings, start=1)
            ],
        }
        policy_ref = store.seal_object("operational_evidence", evidence.comparison_policy_payload(persona))
        completeness_ref = store.seal_object("operational_evidence", completeness)
        package = {
            "schema_version": evidence.EVIDENCE_PACKAGE_V3_SCHEMA,
            "persona": persona,
            "version": target,
            "official_artifacts": [dict(item) for item in store_campaign["official_artifacts"]],
            "platforms": list(store_campaign["platforms"]),
            "entrypoints": list(store_campaign["entrypoints"]),
            "default_conditions": list(store_campaign["default_conditions"]),
            "comparison_policy_ref": policy_ref,
            "completeness_ref": completeness_ref,
            "producer_tool_sha256": tool_sha,
            "evidence_items": [
                {
                    "evidence_id": f"EVIDENCE-{probe}",
                    "source_ids": probe_scenarios[probe],
                    "evidence_refs": [_binding(relays[probe], self.data), *wire_bindings.get(probe, [])],
                    "applicability": sorted(
                        [f"platform={derived['platform']}", f"privacy={derived['privacy_mode']}", f"probe={probe}", "response_mode=live"]
                    ),
                }
                for probe in probes
            ],
        }
        package_ref = store.seal_object("evidence_package", package)
        discovery_ref = store.append_fact(
            campaign_id,
            "discovery_recorded",
            {
                "version": target, "source": evidence.DISCOVERY_SOURCE, "discovered_at_utc": STAGE_TIME, "tool_sha256": tool_sha,
                "artifact_refs": [dict(item) for item in store_campaign["official_artifacts"]],
            },
            STAGE_TIME,
        )
        evidence_ref = store.append_fact(
            campaign_id, "evidence_recorded", {"discovery_fact_ref": discovery_ref, "evidence_package_ref": package_ref}, STAGE_TIME
        )
        attempt = _write_json(campaign / "official" / "attempts" / "attempt-fixture" / "attempt.json", {"status": "awaiting_receipts"})
        stage_path = _write_json(
            campaign / seal.STAGE_RESULT_RELATIVE,
            {
                "schema_version": seal.STAGE_RESULT_SCHEMA, "client": "claude", "campaign_id": campaign_id, "stage": "capture-official",
                "status": "complete", "mode": "captured",
                "attempt": {"path": attempt.relative_to(campaign).as_posix(), "sha256": claude_upgrade.file_sha256(attempt)},
                "seal": {"generation": "g-fixture"}, "predecessor_reuse": None,
                "store": {
                    "root": claude_upgrade_store.CAMPAIGN_STORE_DIRNAME, "campaign_id": campaign_id, "checkpoint": "evidence_recorded",
                    "discovery_fact_ref": discovery_ref, "evidence_fact_ref": evidence_ref, "evidence_package_ref": package_ref,
                },
                "execute_item_ids": sorted(job["id"] for job in scenarios["capture_jobs"]), "reuse_item_ids": [],
                "live_request_count": 0, "scanned_bytes": 0, "sealed_at_utc": STAGE_TIME,
            },
        )
        claude_upgrade_vc.complete_campaign_phase(
            campaign, manifest, phase="VC-1", stage_receipt_path=stage_path, completed_at_utc=STAGE_TIME,
            execute_item_ids=sorted(job["id"] for job in scenarios["capture_jobs"]), reuse_item_ids=[], live_request_count=0, scanned_bytes=0,
        )
        _write_bytes(
            campaign / "analysis" / "candidate-denominator.json",
            candidates if candidates is not None else json.dumps(CANDIDATES, ensure_ascii=False).encode("utf-8"),
        )
        return campaign

    @staticmethod
    def observed_entry(dimension: str) -> dict[str, Any]:
        spec = claude_fw_f_v5.OBSERVED_ONLY_DIMENSIONS[dimension]
        observed = OBSERVED[dimension]
        return {
            "dimension": dimension, "kind": spec["kind"], "target": spec["target"], "evaluable": True, "observed": observed,
            "observed_values": ["seen"] if observed and spec["kind"] == "header" else [],
            "observed_request_count": 1 if observed and spec["kind"] == "request_path" else 0,
            "conditions": {"driver": "print", "privacy_environment": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}},
        }

    def candidates_path(self, campaign: Path) -> Path:
        return campaign / "analysis" / "candidate-denominator.json"

    def draft(self, campaign: Path) -> dict[str, Any]:
        code, result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--candidates", str(self.candidates_path(campaign))])
        self.assertEqual(code, 2, stderr)
        return result

    def revise(self, campaign: Path, parent: str, directory: Path) -> tuple[int, dict | None, str]:
        return run_main(["classify", "--campaign-dir", str(campaign), "--from-revision", parent, "--revision-input", str(directory)])

    def approve_argv(self, campaign: Path, revision: str, review: str) -> list[str]:
        return ["classify", "--campaign-dir", str(campaign), "--approve-revision", revision, "--approve-review-sha256", review]

    # -- 审核人替身：把机器草案逐项处置成可批准的修订 -----------------------------

    def resolved_documents(self, draft_dir: Path, *, change: tuple[str, ...] = (), delete: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
        docs = {name: json.loads((draft_dir / filename).read_text(encoding="utf-8")) for name, filename in classify.DRAFT_FILES.items()}
        fallback = sorted({item for row in docs["rule_migration"]["rules"] for item in row["evidence_item_ids"]})[0]
        target_ids = [spec for spec in docs["rule_migration"]["target_spec_ids"] if spec not in delete]
        for row in docs["rule_migration"]["rules"]:
            decision = "delete" if row["spec_id"] in delete else "change" if row["spec_id"] in change else "inherit"
            row.update(
                migration_decision=decision,
                evidence_level="observed",
                rule_lifecycle="superseded" if decision == "delete" else "candidate",
                evidence_item_ids=row["evidence_item_ids"] or [fallback],
                applicability=row["applicability"] or ["platform=linux/arm64"],
                rationale=f"按目标证据复核：{decision}。",
            )
        affected = sorted([*change, *delete])
        docs["rule_migration"].update(
            target_spec_ids=target_ids,
            affected_rules=affected,
            inherited_rules=sorted(set(docs["rule_migration"]["target_spec_ids"]) - set(affected)),
        )
        target = docs["target_rules"]
        target["required_rules"] = [rule for rule in target["required_rules"] if rule["spec_id"] not in delete]
        profile = sum(len(rule["atomic_assertion_ids"]) for rule in target["required_rules"])
        scenario_only = sum(len(group["atomic_assertion_ids"]) for group in target["scenario_only_groups"])
        target.update(
            required_rule_count=len(target["required_rules"]),
            profile_atomic_assertion_count=profile,
            scenario_only_assertion_count=scenario_only,
            atomic_assertion_count=profile + scenario_only,
        )
        atomic = docs["atomic_assertions"]
        atomic["assertions"] = [row for row in atomic["assertions"] if row["owner_id"] not in delete]
        for row in atomic["assertions"]:
            row.update(
                evidence_item_ids=row["evidence_item_ids"] or [fallback],
                conditions=row["conditions"] or ["platform=linux/arm64"],
                channels=["P", "M"] if row["assertion_id"].startswith("SPEC-TLS") else ["R", "M"],
                result="observed",
                rationale="按目标证据复核通过。",
            )
        ledger = docs["discovery_dispositions"]
        for row in [*ledger["discoveries"], *ledger["candidates"]]:
            identifier = row.get("discovery_id") or row.get("candidate_id")
            row["bindings"] = [
                {"binding_type": "supporting_fact_bound", "fact_ids": [f"FACT-{identifier}"], "owner_kind": "record_only", "evidence_role": "test_context"}
            ]
        for row in ledger["observed_only_facts"]:
            row["bindings"] = copy.deepcopy(row["suggested_bindings"])
        scenarios = docs["scenarios"]
        for item in [*scenarios["evidence_scenarios"], *scenarios["capture_jobs"]]:
            item["covers"] = [rule for rule in item["covers"] if rule not in delete]
        for item in scenarios["evidence_scenarios"]:
            # 只覆盖被删规则的场景不再是规则证据场景，改为发现场景（场景清单要求 purpose 与 covers 一致）。
            if not item["covers"] and item["purpose"] == "rule_evidence":
                item["purpose"] = "discovery"
        docs["profile_patches"]["rule_patches"] = [
            {
                "spec_id": spec,
                "migration_decision": "delete" if spec in delete else "change",
                "profile_paths": [f"/rules/{spec}"],
                "rationale": "受影响规则的画像补丁计划。",
            }
            for spec in affected
        ]
        return docs

    def write_documents(self, docs: dict[str, dict[str, Any]], directory: Path) -> Path:
        directory.mkdir(parents=True)
        target_bytes = classify._serialize(docs["target_rules"])
        docs["scenarios"]["rule_manifest"] = classify._scenario_rule_binding(target_bytes, len(docs["target_rules"]["required_rules"]))
        for name, filename in classify.DRAFT_FILES.items():
            (directory / filename).write_bytes(target_bytes if name == "target_rules" else classify._serialize(docs[name]))
        return directory

    @staticmethod
    def reason_codes(result: dict[str, Any]) -> set[str]:
        return {reason["code"] for reason in result["gate"]["reasons"]}


class MachineDraftTests(_ClassifyCase):
    """机器草案：分母由输入计算、全部未决、同输入复用；v5 只记录事实的识别与失败关闭。"""

    def test_machine_draft_is_blocked_input_derived_and_idempotent(self) -> None:
        campaign = self.build_campaign()
        result = self.draft(campaign)
        self.assertEqual((result["status"], result["revision"], result["reused"]), ("classification_blocked", "rev-0001", False))
        manifest = json.loads(plan.baseline_rule_manifest_path(BASELINE).read_text(encoding="utf-8"))
        denominators = result["denominators"]
        self.assertEqual(
            (denominators["discovery_count"], denominators["candidate_count"], denominators["baseline_rule_count"], denominators["baseline_atomic_assertion_count"]),
            (3, 2, manifest["required_rule_count"], manifest["atomic_assertion_count"]),
        )
        self.assertEqual(denominators["observed_only_fact_count"], 3)
        self.assertEqual(denominators["observed_only_present_count"], 1)
        counts = result["gate"]["counts"]
        self.assertEqual((counts["target_rule_count"], counts["atomic_assertion_count"]), (40, 110))
        self.assertEqual((counts["profile_atomic_assertion_count"], counts["scenario_only_assertion_count"]), (106, 4))
        self.assertEqual(counts["migration_decisions"], {"blocked": 40})
        self.assertEqual(counts["discovery"], {"total": 3, "resolved": 0, "unresolved": 3})
        reasons = {reason["code"]: reason["count"] for reason in result["gate"]["reasons"]}
        self.assertEqual(
            reasons,
            {
                "migration_blocked": 40, "assertion_blocked": 110, "discovery_unresolved": 3, "candidate_unresolved": 2,
                "observed_only_unresolved": 3,
            },
        )
        draft_dir = Path(result["draft_dir"])
        self.assertEqual(sorted(path.name for path in draft_dir.iterdir()), sorted([*classify.DRAFT_FILES.values(), classify.DRAFT_RECEIPT_FILENAME]))
        migration = json.loads((draft_dir / "rule-migration.json").read_text(encoding="utf-8"))
        # 既有规则一律 blocked，不预填 inherit；证据项与适用条件只抄跨来源矩阵的线索。
        self.assertEqual({(row["migration_decision"], row["evidence_level"]) for row in migration["rules"]}, {("blocked", "blocked")})
        self.assertTrue(all(row["evidence_item_ids"] and row["applicability"] for row in migration["rules"]))
        self.assertEqual((migration["affected_rules"], migration["inherited_rules"]), ([], []))
        ledger = json.loads((draft_dir / "discovery-disposition-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual([row["bindings"] for row in [*ledger["discoveries"], *ledger["candidates"], *ledger["observed_only_facts"]]], [[]] * 8)
        scenarios = json.loads((draft_dir / "scenarios.json").read_text(encoding="utf-8"))
        self.assertEqual(scenarios["rule_manifest"]["sha256"], hashlib.sha256((draft_dir / "target-rules.json").read_bytes()).hexdigest())
        # 同输入重跑：复用同一 revision，不新增目录。
        again = self.draft(campaign)
        self.assertEqual((again["revision"], again["reused"], again["review_sha256"]), ("rev-0001", True, result["review_sha256"]))
        self.assertEqual(sorted(path.name for path in (campaign / classify.DRAFT_ROOT).iterdir()), ["rev-0001"])
        # revision 只写一次：原地改动在加载时被拒绝。
        (draft_dir / "profile-patches.json").write_text("{}", encoding="utf-8")
        code, _result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--candidates", str(self.candidates_path(campaign))])
        self.assertEqual(code, 1)
        self.assertIn("已被改动", stderr)

    def test_observed_only_facts_are_recognized_with_suggestions(self) -> None:
        campaign = self.build_campaign()
        result = self.draft(campaign)
        ledger = json.loads((Path(result["draft_dir"]) / "discovery-disposition-ledger.json").read_text(encoding="utf-8"))
        facts = {row["fact_id"]: row for row in ledger["observed_only_facts"]}
        self.assertEqual(
            sorted(facts),
            [
                "OBSERVED-v4-agent-usage-limit-header.usage_limit",
                "OBSERVED-v4-dispatch-account-negative-header.dispatch_id",
                "OBSERVED-v4-tui-usage-aux.usage",
            ],
        )
        usage = facts["OBSERVED-v4-tui-usage-aux.usage"]
        self.assertEqual((usage["observed"], usage["observed_request_count"], usage["related_spec_ids"]), (True, 1, ["SPEC-EP-011"]))
        self.assertEqual(usage["suggested_bindings"][0]["binding_type"], "rule_bound")
        limit = facts["OBSERVED-v4-agent-usage-limit-header.usage_limit"]
        self.assertEqual((limit["observed"], limit["target"], limit["evidence_id"]), (False, "anthropic-usage-limit", "EVIDENCE-v4-agent-usage-limit"))
        self.assertEqual(
            limit["suggested_bindings"],
            [{"binding_type": "supporting_fact_bound", "fact_ids": [limit["fact_id"]], "owner_kind": "record_only", "evidence_role": "observed_only_absent_record_only"}],
        )
        receipt = json.loads((Path(result["draft_dir"]) / classify.DRAFT_RECEIPT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(len(receipt["inputs"]["observed_only_sources"]), 79)

    def test_missing_v5_evidence_fails_closed(self) -> None:
        for index, (label, override, message) in enumerate(
            (
                ("中继清单缺 v5 维度证据", {"v4-native-tls-baseline": {"actual_wire_inventory": []}}, "缺少 FW-F v5 维度证据"),
                (
                    "只记录维度缺条目",
                    {"v4-tui-usage": {"schema_version": claude_fw_f_v5.SCHEMA_DIMENSION_EVIDENCE, "probe_id": "v4-tui-usage", "observed_only": []}},
                    "v5 只记录事实缺失",
                ),
            )
        ):
            with self.subTest(label=label):
                campaign = self.build_campaign(campaign_id=f"claude-code-2_1_285-v5-{index}", relay_overrides=override)
                code, _result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--candidates", str(self.candidates_path(campaign))])
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
                self.assertFalse((campaign / classify.DRAFT_ROOT).exists())

    def test_inputs_are_verified_before_drafting(self) -> None:
        campaign = self.build_campaign()
        # 候选分母声明计数与实际不一致：分母必须由内容计算。
        broken = copy.deepcopy(CANDIDATES)
        broken["total_orthogonal_candidates"] = 3
        _write_json(self.candidates_path(campaign), broken)
        code, _result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--candidates", str(self.candidates_path(campaign))])
        self.assertEqual(code, 1)
        self.assertIn("total_orthogonal_candidates=3", stderr)
        # VC-1 封存对象在 Store 绑定之后被改：摘要漂移即拒绝。
        _write_json(self.candidates_path(campaign), CANDIDATES)
        discovery = campaign / "official" / "seal" / "g-fixture" / "drafts" / "discovery-inventory.json"
        discovery.write_text(discovery.read_text(encoding="utf-8") + " ", encoding="utf-8")
        code, _result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--candidates", str(self.candidates_path(campaign))])
        self.assertEqual(code, 1)
        self.assertIn("VC-1 封存后漂移", stderr)
        # 候选分母不在 Campaign 目录内：拒绝。
        outside = _write_json(self.root / "outside.json", CANDIDATES)
        code, _result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--candidates", str(outside)])
        self.assertEqual(code, 1)
        self.assertIn("Campaign 目录内", stderr)
        # 参数组合：批准缺摘要、修订带候选。
        code, _result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--approve-revision", "rev-0001"])
        self.assertEqual(code, 1)
        self.assertIn("--approve-review-sha256", stderr)

    def test_classify_stays_outside_wire_and_evidence_closures(self) -> None:
        policy = claude_identity.load_policy()
        digests = claude_identity.managed_tree_digests(REPOSITORY_ROOT, policy)
        index = claude_identity._module_index(policy, digests)
        module = "tools.official_client_capture.claude_upgrade_classify"
        for layer, roots in (("wire_producer", "wire_roots"), ("evidence_semantics", "evidence_roots")):
            closure = claude_identity.closure(
                policy, REPOSITORY_ROOT, digests, claude_identity._orchestrator_roots(policy, index, policy["orchestrator"][roots]), layer=layer
            )
            with self.subTest(layer=layer):
                self.assertNotIn(module, {item["module"] for item in closure["symbols"]})
                self.assertNotIn(module, {item["module"] for item in closure["modules"]})
        self.assertEqual(claude_identity.classify_path(policy, "official_client_capture/claude_upgrade_classify.py"), "control")


class RevisionAndApprovalTests(_ClassifyCase):
    """修订、门禁、批准、崩溃收敛与封存后的拒绝。"""

    def test_revision_gates_reject_tampering_and_forbidden_bindings(self) -> None:
        campaign = self.build_campaign()
        draft = self.draft(campaign)
        docs = self.resolved_documents(Path(draft["draft_dir"]))
        for label, mutate, code_expected in (
            ("改只读字段", lambda d: d["discovery_dispositions"]["discoveries"][0].update(proposition="改名"), "discovery_fields_modified"),
            (
                "零流量绑定规则",
                lambda d: next(
                    row for row in d["discovery_dispositions"]["observed_only_facts"] if not row["observed"]
                ).update(bindings=[{"binding_type": "rule_bound", "spec_ids": ["SPEC-EP-011"], "evidence_role": "错误归类"}]),
                "observed_only_binding_forbidden",
            ),
            ("delete 不用 superseded", lambda d: d["rule_migration"]["rules"][0].update(migration_decision="delete"), "migration_invalid"),
            ("理由保留占位", lambda d: d["rule_migration"]["rules"][1].update(rationale=classify.RULE_PLACEHOLDER_RATIONALE), "rationale_placeholder"),
            ("断言缺 M 通道", lambda d: d["atomic_assertions"]["assertions"][0].update(channels=["R"]), "assertion_channel_invalid"),
            ("执行契约变化", lambda d: d["scenarios"]["capture_jobs"][0].update(required=not d["scenarios"]["capture_jobs"][0]["required"]), "scenarios_execution_changed"),
            ("类型错误", lambda d: d["atomic_assertions"]["assertions"][0].update(owner_id=["SPEC-BODY-001"]), "assertion_invalid"),
        ):
            with self.subTest(label=label):
                variant = copy.deepcopy(docs)
                mutate(variant)
                directory = self.write_documents(variant, self.root / f"variant-{code_expected}-{label}")
                code, result, stderr = self.revise(campaign, draft["revision"], directory)
                self.assertEqual((code, result["status"]), (2, "classification_blocked"), stderr)
                self.assertIn(code_expected, self.reason_codes(result))

    def test_approval_seals_ledgers_and_converges_after_sigkill(self) -> None:
        campaign = self.build_campaign()
        draft = self.draft(campaign)
        directory = self.write_documents(self.resolved_documents(Path(draft["draft_dir"])), self.root / "resolved")
        code, revised, stderr = self.revise(campaign, draft["revision"], directory)
        self.assertEqual((code, revised["status"], revised["revision"], revised["parent_revision"]), (2, "awaiting_classification_approval", "rev-0002", "rev-0001"), stderr)
        self.assertEqual(revised["gate"]["reasons"], [])
        # 同内容修订复用；blocked 的 rev-0001 与错误摘要都不能批准。
        code, again, _stderr = self.revise(campaign, draft["revision"], directory)
        self.assertEqual((again["revision"], again["reused"]), ("rev-0002", True))
        code, _result, stderr = run_main(self.approve_argv(campaign, "rev-0001", draft["review_sha256"]))
        self.assertEqual(code, 1)
        self.assertIn("门禁 blocked", stderr)
        code, _result, stderr = run_main(self.approve_argv(campaign, "rev-0002", "0" * 64))
        self.assertEqual(code, 1)
        self.assertIn("review_sha256 不一致", stderr)

        # 批准在分类事实写入后被硬杀（父监督器上下文）：分类事实已在，阶段结果与 VC-2 checkpoint 未写。
        store_root = claude_upgrade_store.campaign_store_root(campaign)
        approve = self.approve_argv(campaign, "rev-0002", revised["review_sha256"])
        producer = {
            "CODEX_UPGRADE_CAMPAIGN_RUN_ACTIVE": "1",
            "CODEX_UPGRADE_CAMPAIGN_RUN_DIR": "control/vc/runs/run-0002",
            "CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE": "b" * 64,
            "CODEX_UPGRADE_CAMPAIGN_ACTION_ID": "vc2-classify-approve",
        }
        status = seal_tests.run_killed(approve, kill_after="rule-classification-record", environment=producer)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        kinds = seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)
        self.assertEqual(kinds, collections.Counter({"discovery_recorded": 1, "evidence_recorded": 1, "rule_classification_recorded": 1}))
        self.assertFalse((campaign / classify.STAGE_RESULT_RELATIVE).exists())
        self.assertFalse(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-2").exists())
        # 未收尾时不能再起草案。
        code, _result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--candidates", str(self.candidates_path(campaign))])
        self.assertEqual(code, 1)
        self.assertIn("批准未收尾", stderr)

        code, approved, stderr = run_main(approve)
        self.assertEqual((code, approved["status"]), (0, "complete"), stderr)
        self.assertTrue(approved["store"]["classification_fact_reused"])
        self.assertEqual(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)["rule_classification_recorded"], 1)
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "evidence_recorded")
        facts = {fact["fact_kind"]: fact for fact in ControlStore(store_root).list_facts(CAMPAIGN_ID)}
        classification = facts["rule_classification_recorded"]
        self.assertEqual(classification["producer_run"]["action_id"], "vc2-classify-approve")
        self.assertEqual(classification["payload"]["evidence_fact_ref"], ControlStore(store_root).fact_ref(facts["evidence_recorded"]))
        store = ControlStore(store_root)
        migration = store.load_object(classification["payload"]["rule_migration_ledger_ref"])["payload"]
        atomic = store.load_object(classification["payload"]["atomic_assertion_ledger_ref"])["payload"]
        self.assertEqual((len(migration["rules"]), migration["affected_rules"], len(migration["inherited_rules"])), (40, [], 40))
        self.assertEqual(len(atomic["assertions"]), 110)
        self.assertEqual(atomic["rule_migration_ledger_ref"], classification["payload"]["rule_migration_ledger_ref"])

        # 阶段结果、批准副本与 VC-2 checkpoint（逐级重放到 VC-0）。
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        stage = classify.load_stage_result(campaign, manifest)
        self.assertEqual((stage["revision"], stage["reuse_item_ids"], stage["execute_item_ids"]), ("rev-0002", stage["inherited_rules"], []))
        self.assertEqual(classify.approved_rules(campaign, manifest, require_approved=True), tuple(stage["target_rules"]))
        approved_dir = campaign / classify.APPROVED_ROOT
        for name, filename in classify.DRAFT_FILES.items():
            self.assertEqual((approved_dir / filename).read_bytes(), (campaign / classify.DRAFT_ROOT / "rev-0002" / filename).read_bytes(), name)
        _path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(campaign, claude_upgrade_vc.vc_campaign_plan(campaign, manifest), "VC-2")
        self.assertEqual(checkpoint["reuse_item_ids"], stage["inherited_rules"])
        self.assertEqual(checkpoint["metrics"], {"live_request_count": 0, "scanned_bytes": 0})

        # 再批准逐字复用；批准别的 revision、再起草案一律拒绝。
        before = {path: claude_upgrade.file_sha256(path) for path in (campaign / classify.STAGE_RESULT_RELATIVE, claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-2"))}
        code, repeated, stderr = run_main(approve)
        self.assertEqual((code, repeated["checkpoint_sha256"]), (0, approved["checkpoint_sha256"]), stderr)
        self.assertEqual({path: claude_upgrade.file_sha256(path) for path in before}, before)
        self.assertEqual(len(ControlStore(store_root).list_facts(CAMPAIGN_ID)), 3)
        code, _result, stderr = run_main(self.approve_argv(campaign, "rev-0001", draft["review_sha256"]))
        self.assertEqual(code, 1)
        self.assertIn("禁止以 rev-0001 覆盖", stderr)
        code, _result, stderr = run_main(["classify", "--campaign-dir", str(campaign), "--candidates", str(self.candidates_path(campaign))])
        self.assertEqual(code, 1)
        self.assertIn("后继 Campaign", stderr)

    def test_change_and_delete_partition_and_input_drift(self) -> None:
        campaign = self.build_campaign()
        draft = self.draft(campaign)
        docs = self.resolved_documents(Path(draft["draft_dir"]), change=("SPEC-BODY-001",), delete=("SPEC-TOOL-024",))
        directory = self.write_documents(docs, self.root / "change-delete")
        code, revised, stderr = self.revise(campaign, draft["revision"], directory)
        self.assertEqual((code, revised["gate"]["reasons"]), (2, []), stderr)
        self.assertEqual(revised["gate"]["counts"]["affected_rules"], ["SPEC-BODY-001", "SPEC-TOOL-024"])
        self.assertEqual(revised["gate"]["counts"]["target_rule_count"], 39)
        # 输入漂移：候选分母在审核后被改，批准拒绝。
        original = self.candidates_path(campaign).read_bytes()
        changed = copy.deepcopy(CANDIDATES)
        changed["candidate_groups"]["semantic_candidate_families"][0]["proposition"] = "改过的命题。"
        _write_json(self.candidates_path(campaign), changed)
        code, _result, stderr = run_main(self.approve_argv(campaign, revised["revision"], revised["review_sha256"]))
        self.assertEqual(code, 1)
        self.assertIn("输入已漂移", stderr)
        _write_bytes(self.candidates_path(campaign), original)
        code, approved, stderr = run_main(self.approve_argv(campaign, revised["revision"], revised["review_sha256"]))
        self.assertEqual((code, approved["affected_rules"]), (0, ["SPEC-BODY-001", "SPEC-TOOL-024"]), stderr)
        store = ControlStore(claude_upgrade_store.campaign_store_root(campaign))
        classification = next(fact for fact in store.list_facts(CAMPAIGN_ID) if fact["fact_kind"] == "rule_classification_recorded")
        migration = store.load_object(classification["payload"]["rule_migration_ledger_ref"])["payload"]
        deleted = next(rule for rule in migration["rules"] if rule["spec_id"] == "SPEC-TOOL-024")
        self.assertEqual((deleted["migration_decision"], deleted["rule_lifecycle"]), ("delete", "superseded"))
        self.assertNotIn("SPEC-TOOL-024", migration["target_spec_ids"])
        self.assertIn("SPEC-TOOL-024", migration["baseline_spec_ids"])
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.assertNotIn("SPEC-TOOL-024", classify.approved_rules(campaign, manifest, require_approved=True))
        self.assertEqual(len(classify.approved_rules(campaign, manifest, require_approved=False)), 40)


@unittest.skipUnless(os.environ.get(FIXTURE_ENV), f"设置 {FIXTURE_ENV} 指向 2.1.226 历史清零收据副本目录时运行")
class HistoricalClearanceAcceptanceTests(_ClassifyCase):
    """验收（设计 B3-02）：2.1.226 夹具的草案分母等于历史清零收据（7368／593／110／40）。"""

    def test_draft_denominators_match_historical_clearance(self) -> None:
        fixture = Path(os.environ[FIXTURE_ENV])
        closure = json.loads((fixture / "closure.json").read_text(encoding="utf-8"))
        discovery = (fixture / "discovery-inventory.json").read_bytes()
        self.assertEqual(hashlib.sha256(discovery).hexdigest(), closure["input_bindings"]["discovery_inventory"]["sha256"])
        campaign = self.build_campaign(
            target=BASELINE,
            campaign_id="claude-code-2_1_226-acceptance",
            discovery=discovery,
            candidates=(fixture / "candidate-denominator.json").read_bytes(),
        )
        result = self.draft(campaign)
        denominators = result["denominators"]
        counts = result["gate"]["counts"]
        baseline = json.loads(plan.baseline_rule_manifest_path(BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(denominators["discovery_count"], closure["source_discovery_count"])
        self.assertEqual(denominators["candidate_count"], closure["orthogonal_candidate_count"])
        self.assertEqual(denominators["candidate_count"], closure["candidate_resolution_count"])
        self.assertEqual(counts["atomic_assertion_count"], closure["measured_rule_count"])
        self.assertEqual(counts["target_rule_count"], baseline["required_rule_count"])
        self.assertEqual(
            (denominators["discovery_count"], denominators["candidate_count"], counts["atomic_assertion_count"], counts["target_rule_count"]),
            (7368, 593, 110, 40),
        )
        self.assertEqual(counts["discovery"]["unresolved"], 7368)
        self.assertEqual(counts["candidate"]["unresolved"], 593)
        self.assertNotIn("input_reference_open", self.reason_codes(result))


if __name__ == "__main__":
    unittest.main()
