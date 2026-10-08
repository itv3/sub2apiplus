"""只使用合成 Persona 和无秘密数据构造完整 FW-D Campaign。

``SyntheticCampaign(root)`` 构造 v1 Campaign（逐字保持原夹具行为）；``SyntheticCampaign(root, schema="v2")``
构造 Campaign v2（B1-12），其冻结工具身份按当前受管树现算——测试应先用 ``ManagedToolTree`` 把受管树复制一份并
替换 ``receipts._tool_repository_root``，既隔离并发改动，也能在副本上模拟工具演进。
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_control import receipts as receipts_module
from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)
from tools.official_client_control.contracts import (
    atomic_assertion_ledger_identity_sha256,
    candidate_identity_sha256,
    campaign_identity_sha256,
    capability_key,
    invalidation_review_sha256,
    profile_approval_identity_sha256,
    rule_migration_ledger_identity_sha256,
)
from tools.official_client_control.receipts import (
    candidate_revision_facts,
    control_tool_bundle_sha256,
    current_candidate_revision,
    current_evaluation_baseline,
    current_tool_manifest,
    evaluation_baseline_facts,
    finalize_activation,
    finalize_candidate_build,
    finalize_candidate_delivery,
    finalize_promotion,
    finalize_validation,
    finalize_validation_gate,
    store_bundle_from_manifest,
    tool_evolutions,
)
from tools.official_client_control.store import ControlStore


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
TARGET_IMAGE = f"sha256:{'1' * 64}"
ROLLBACK_IMAGE = f"sha256:{'2' * 64}"


class ManagedToolTree:
    """两个受管根的受管文件副本（只复制受管清单里的文件）。

    ``active()`` 期间 ``receipts._tool_repository_root`` 指向副本：Campaign v2 的清单核对与写入时有效性都在
    副本上现算，其它进程对真实工作树的并发改动不影响测试；``write`` 在副本里改或加文件以模拟工具演进，
    ``restore`` 撤销全部改动。

    副本里的 Claude 编排器换成只定义身份策略闭包根的替身（``orchestrator_stub``）：v3 引擎的三层摘要因此在
    副本上可确定性地现算，不随真实编排器（B1-09 及以后仍在演进）的中间状态变化。
    """

    def __init__(self, destination: Path) -> None:
        engine = receipts_module._identity_engine("claude")
        source_root = Path(engine.REPOSITORY_ROOT)
        digests = engine.managed_tree_digests(source_root, engine.load_policy())
        self.root = destination
        for key in digests:
            root_name, _, relative = key.partition("/")
            target = destination / "tools" / root_name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_root / "tools" / root_name / relative, target)
        policy = engine.load_policy()
        orchestrator = self.path(str(policy["orchestrator"]["file"]))
        orchestrator.parent.mkdir(parents=True, exist_ok=True)
        orchestrator.write_bytes(orchestrator_stub(policy))
        self._originals: dict[Path, bytes | None] = {}

    @contextmanager
    def active(self) -> Iterator["ManagedToolTree"]:
        with mock.patch.object(receipts_module, "_tool_repository_root", return_value=self.root):
            yield self

    def path(self, key: str) -> Path:
        """``<根目录名>/<相对路径>`` → 副本中的路径。"""

        root_name, _, relative = key.partition("/")
        return self.root / "tools" / root_name / relative

    def _remember(self, path: Path) -> None:
        if path not in self._originals:
            self._originals[path] = path.read_bytes() if path.exists() else None

    def write(self, key: str, content: bytes) -> None:
        path = self.path(key)
        self._remember(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def remove(self, key: str) -> None:
        path = self.path(key)
        self._remember(path)
        path.unlink(missing_ok=True)

    def restore(self) -> None:
        for path, original in self._originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        self._originals.clear()


def orchestrator_stub(policy: dict[str, Any]) -> bytes:
    """编排器替身源码：只定义策略登记的 wire／evidence 闭包根，函数体为空。"""

    names = sorted(
        set(policy["orchestrator"]["wire_roots"]) | set(policy["orchestrator"]["evidence_roots"])
    )
    lines = ['"""Store 测试用 Claude 编排器替身：只定义身份策略登记的闭包根。"""', ""]
    for name in names:
        lines += ["", f"def {name}():", "    return None", ""]
    return "\n".join(lines).encode("utf-8")


def synthetic_tool_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    """由现算清单得到七项工具身份：三层摘要由 v3 引擎在同一（副本）仓库根上现算，与建 Campaign 时的核对同源。"""

    return receipts_module.tool_identity_from_manifest(manifest)


class SyntheticCampaign:
    """按文档顺序生成可在单元测试中变异的完整受管事实链。"""

    def __init__(self, root: Path, *, schema: str = "v1") -> None:
        if schema not in {"v1", "v2"}:
            raise ValueError(f"不支持的 Campaign 合同：{schema}")
        self.root = root
        self.schema = schema
        self.external_root = root / "external"
        self.external_root.mkdir(mode=0o700)
        self.store_root = root / "store"
        self.store = ControlStore.initialize(
            self.store_root, "2026-08-18T00:00:00Z"
        )
        self.persona = {
            "provider": "synthetic-provider",
            "official_product": "synthetic-cli",
            "auth_family": "oauth",
            "upstream_route_family": "synthetic-responses",
        }
        self.campaign_id = "synthetic-1.0.0-campaign"
        if schema == "v2":
            # v2：工具摘要字段取有效身份的 tool_files_sha256；Store 自身摘要按受管清单同一口径复算。
            manifest = current_tool_manifest("claude")
            self.tool_identity = synthetic_tool_identity(manifest)
            self.store_tool_sha256 = store_bundle_from_manifest(manifest)
            self.tool_sha256 = self.tool_identity["tool_files_sha256"]
        else:
            self.tool_sha256 = control_tool_bundle_sha256()
        self._clock = datetime(2026, 8, 18, tzinfo=timezone.utc)
        self.references: dict[str, dict[str, Any]] = {}
        self._prepare_external_files()

    def _time(self) -> str:
        self._clock += timedelta(minutes=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    def _write_external(self, relative: str, content: bytes) -> dict[str, Any]:
        path = self.external_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"path": relative, "sha256": sha256_file(path), "bytes": len(content)}

    def _prepare_external_files(self) -> None:
        self.contract_binding = self._write_external(
            "contract/shared-contract.txt", b"synthetic read-only contract\n"
        )
        self.receipt_binding = self._write_external(
            "receipts/fw-c.json", b'{"result":"stable"}\n'
        )
        self.catalog_binding = self._write_external(
            "runtime/catalog.json", b'{"persona":"codex","result":"stable"}\n'
        )
        self.official_binding = self._write_external(
            "official/synthetic-cli.bin", b"synthetic official artifact\n"
        )
        self.evidence_binding = self._write_external(
            "evidence/spec-001.json", b'{"spec":"SPEC-001","verified":true}\n'
        )
        self.runtime_binding = self._write_external(
            "runtime/selector.json", b'{"active":null,"rollback":null}\n'
        )
        self.candidate_binding = self._write_external(
            "candidate/sub2api.bin", b"synthetic candidate artifact\n"
        )
        self.private_binding = self._write_external(
            "private/redacted-capture.bin", b"redacted synthetic private evidence\n"
        )

    def seal_manifest(
        self,
        kind: str,
        manifest_id: str,
        entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": f"official-client-{kind.replace('_', '-')}/v1",
            "persona": self.persona,
            "manifest_id": manifest_id,
            "entries": entries
            or [{"id": "entry-1", "facts": {"result": "pass"}}],
        }
        reference = self.store.seal_object(kind, payload)
        self.references[manifest_id] = reference
        return reference

    def bootstrap_and_campaign(self, **campaign_overrides: Any) -> None:
        """封存 bootstrap 并建立 Campaign；``campaign_overrides`` 覆盖冻结字段（如 platforms、oauth_org_id），
        供前序导入测试（B1-14）构造身份不同的两个 Campaign，缺省时行为不变。"""

        contract_sources = [self.contract_binding]
        bootstrap = {
            "schema_version": "official-client-control-bootstrap/v1",
            "source_commit": "1" * 40,
            "contract_sources": contract_sources,
            "contract_bundle_sha256": canonical_sha256(contract_sources),
            "fw_c_receipts": [self.receipt_binding],
            "runtime_catalog": [self.catalog_binding],
            "tool_bundle_sha256": self.tool_sha256,
            "result": "stable",
        }
        if self.schema == "v2":
            bootstrap["tool_bundle_sha256"] = self.store_tool_sha256
        self.references["bootstrap"] = self.store.seal_object("bootstrap", bootstrap)
        campaign = {
            "schema_version": "official-client-control-campaign/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "target_version": "1.0.0",
            "official_artifacts": [self.official_binding],
            "platforms": ["linux/amd64"],
            "entrypoints": ["synthetic-cli"],
            "default_conditions": ["privacy=default"],
            "tool_bundle_sha256": self.tool_sha256,
            "bootstrap_ref": self.references["bootstrap"],
            "created_at_utc": self._time(),
            "identity_sha256": "",
        }
        if self.schema == "v2":
            campaign.update(self.campaign_v2_fields())
        campaign.update(campaign_overrides)
        campaign["identity_sha256"] = campaign_identity_sha256(campaign)
        self.campaign_document = campaign
        self.store.create_campaign(campaign)

    def campaign_v2_fields(self) -> dict[str, Any]:
        """Campaign v2 在 v1 之上新增与改写的字段（合成值）。"""

        stages = {
            "VC-0": [],
            "VC-1": ["VC-0"],
            "VC-2": ["VC-1"],
            "VC-3": ["VC-2"],
            "VC-4": ["VC-3"],
            "VC-5": ["VC-4"],
            "VC-6": ["VC-5"],
        }
        return {
            "schema_version": "official-client-control-campaign/v2",
            "tool_bundle_sha256": self.store_tool_sha256,
            "client": "claude",
            "campaign_purpose": "production_replacement",
            "oauth_org_id": "synthetic-org-0001",
            "model_catalog_sha256": SHA_B,
            "execution_permissions": {
                "hosts": ["arm64-synthetic"],
                "actions": ["candidate_build", "official_capture"],
            },
            "stage_dependencies": stages,
            "stage_budgets_minutes": {stage: 120 for stage in stages},
            "absolute_deadline_utc": "2026-08-25T00:00:00Z",
            "supervisor_parameters": {"watchdog_seconds": 30},
            "project_ledger_ref": {
                "path": "upgrade-project-ledger-claude/plan.json",
                "plan_sha256": SHA_C,
            },
            "tool_identity": dict(self.tool_identity),
        }

    def evolution_payload(self, *, approved_at_utc: str | None = None) -> dict[str, Any]:
        """把当前受管代码登记为新有效身份的演进 payload（接在当前链尾）。"""

        campaign = self.store.load_campaign(self.campaign_id)
        chain = tool_evolutions(self.store, campaign)
        source = chain[-1][1]["to_identity"] if chain else campaign["tool_identity"]
        target = synthetic_tool_identity(current_tool_manifest("claude"))
        index = len(chain) + 1
        return {
            "client": "claude",
            "campaign_id": self.campaign_id,
            "index": index,
            "from_identity": source,
            "to_identity": target,
            "evolution_receipt": {
                "path": f"control/tool-evolutions/e{index:03d}/evolution.json",
                "sha256": canonical_sha256({"index": index, "to": target}),
            },
            "approved_by": "synthetic-operator",
            "approved_at_utc": approved_at_utc or self._time(),
            "previous_ref": chain[-1][0] if chain else None,
        }

    def register_evolution(self) -> dict[str, Any]:
        """登记工具演进，并让后续夹具写入使用新有效身份的工具摘要。"""

        payload = self.evolution_payload()
        reference = self.store.seal_object("tool_evolution_registration", payload)
        self.tool_identity = dict(payload["to_identity"])
        self.tool_sha256 = self.tool_identity["tool_files_sha256"]
        self.references[f"tool-evolution-{payload['index']}"] = reference
        return reference

    def open_candidate_revision(
        self, candidate_id: str, *, invalidation_ref: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """追加下一条候选 revision 事实，取代当前 revision。

        ``invalidation_ref`` 缺省为 null（B1-12 的直接取代，原行为）；B3-05 的测试传入被取代 revision 的作废对象。
        """

        campaign = self.store.load_campaign(self.campaign_id)
        revisions = candidate_revision_facts(self.store, campaign)
        previous = revisions[-1] if revisions else None
        reference = self.store.append_fact(
            self.campaign_id,
            "candidate_revision_recorded",
            {
                "revision": previous["payload"]["revision"] + 1 if previous else 2,
                "candidate_id": candidate_id,
                "supersedes": self.store.fact_ref(previous) if previous else None,
                "invalidation_ref": invalidation_ref,
            },
            self._time(),
        )
        self.references[f"candidate-revision-{candidate_id}"] = reference
        return reference

    def _record_discovery(self) -> None:
        self.references["discovery"] = self.store.append_fact(
            self.campaign_id,
            "discovery_recorded",
            {
                "version": "1.0.0",
                "source": "synthetic-fixture",
                "discovered_at_utc": self._time(),
                "tool_sha256": self.tool_sha256,
                "artifact_refs": [self.official_binding],
            },
            self._time(),
        )
        self.references["comparison-policy"] = self.seal_manifest(
            "operational_evidence", "comparison-policy"
        )

    def evidence_package_payload(self) -> dict[str, Any]:
        """VC-1 的 EvidencePackage v3 payload（不含迁移结论）；需先完成 Discovery。"""

        return {
            "schema_version": "official-client-evidence-package/v3",
            "persona": self.persona,
            "version": "1.0.0",
            "official_artifacts": [self.official_binding],
            "platforms": ["linux/amd64"],
            "entrypoints": ["synthetic-cli"],
            "default_conditions": ["privacy=default"],
            "comparison_policy_ref": self.references["comparison-policy"],
            "completeness_ref": self.references["comparison-policy"],
            "producer_tool_sha256": self.tool_sha256,
            "evidence_items": [
                {
                    "evidence_id": "EVIDENCE-SPEC-001",
                    "source_ids": ["SPEC-001"],
                    "evidence_refs": [self.evidence_binding],
                    "applicability": ["linux/amd64", "privacy=default"],
                }
            ],
        }

    def record_discovery_and_evidence(self) -> None:
        """执行 VC-1，只封存不含迁移结论的 EvidencePackage v3。"""

        self._record_discovery()
        evidence = self.evidence_package_payload()
        self.references["evidence-package"] = self.store.seal_object(
            "evidence_package", evidence
        )
        self.references["evidence-fact"] = self.store.append_fact(
            self.campaign_id,
            "evidence_recorded",
            {
                "discovery_fact_ref": self.references["discovery"],
                "evidence_package_ref": self.references["evidence-package"],
            },
            self._time(),
        )

    def classify_rules(self, evidence_level: str = "verified") -> None:
        """执行 VC-2，在同一 Campaign 追加分类对象与分类事实。"""

        migration = {
            "schema_version": "official-client-rule-migration-ledger/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "baseline_version": "0.9.0",
            "target_version": "1.0.0",
            "evidence_package_ref": self.references["evidence-package"],
            "baseline_spec_ids": [],
            "target_spec_ids": ["SPEC-001"],
            "rules": [
                {
                    "spec_id": "SPEC-001",
                    "migration_decision": "add",
                    "evidence_level": evidence_level,
                    "rule_lifecycle": "candidate",
                    "compatibility_class": "request_egress",
                    "evidence_item_ids": ["EVIDENCE-SPEC-001"],
                    "applicability": ["linux/amd64", "privacy=default"],
                }
            ],
            "affected_rules": ["SPEC-001"],
            "inherited_rules": [],
            "identity_sha256": "",
        }
        migration["identity_sha256"] = rule_migration_ledger_identity_sha256(
            migration
        )
        self.references["rule-migration-ledger"] = self.store.seal_object(
            "rule_migration_ledger", migration
        )
        atomic = {
            "schema_version": "official-client-atomic-assertion-ledger/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "target_version": "1.0.0",
            "rule_migration_ledger_ref": self.references["rule-migration-ledger"],
            "scenario_only_owner_ids": [],
            "assertions": [
                {
                    "assertion_id": "ASSERT-SPEC-001-001",
                    "owner_kind": "required_rule",
                    "owner_id": "SPEC-001",
                    "evidence_item_ids": ["EVIDENCE-SPEC-001"],
                    "conditions": ["linux/amd64", "privacy=default"],
                    "channels": ["R", "M"],
                    "result": "pass",
                }
            ],
            "identity_sha256": "",
        }
        atomic["identity_sha256"] = atomic_assertion_ledger_identity_sha256(atomic)
        self.references["atomic-assertion-ledger"] = self.store.seal_object(
            "atomic_assertion_ledger", atomic
        )
        self.references["classification-fact"] = self.store.append_fact(
            self.campaign_id,
            "rule_classification_recorded",
            {
                "evidence_fact_ref": self.references["evidence-fact"],
                "evidence_package_ref": self.references["evidence-package"],
                "rule_migration_ledger_ref": self.references[
                    "rule-migration-ledger"
                ],
                "atomic_assertion_ledger_ref": self.references[
                    "atomic-assertion-ledger"
                ],
            },
            self._time(),
        )

    def approve_evidence(self) -> None:
        """执行 VC-3 的第一道批准，显式绑定 VC-2 分类事实。"""

        self.references["evidence-approval"] = self.store.append_fact(
            self.campaign_id,
            "evidence_approved",
            {
                "evidence_fact_ref": self.references["evidence-fact"],
                "evidence_package_ref": self.references["evidence-package"],
                "classification_fact_ref": self.references["classification-fact"],
                "reviewer": "synthetic-reviewer",
                "review_ref": "review/evidence-1",
            },
            self._time(),
        )

    def discovery_and_evidence(self, evidence_level: str = "verified") -> None:
        """供完整链测试使用：连续执行 VC-1、VC-2 和 Evidence 批准。"""

        self.record_discovery_and_evidence()
        self.classify_rules(evidence_level)
        self.approve_evidence()

    def discovery_and_legacy_evidence(
        self,
        evidence_level: str = "verified",
        schema_version: str = "official-client-evidence-package/v1",
    ) -> None:
        """构造 v1/v2 历史链，证明升级后仍可只读重放。"""

        if schema_version not in {
            "official-client-evidence-package/v1",
            "official-client-evidence-package/v2",
        }:
            raise ValueError(f"不支持的历史 EvidencePackage：{schema_version}")

        self._record_discovery()
        evidence = {
            "schema_version": schema_version,
            "persona": self.persona,
            "version": "1.0.0",
            "official_artifacts": [self.official_binding],
            "platforms": ["linux/amd64"],
            "entrypoints": ["synthetic-cli"],
            "default_conditions": ["privacy=default"],
            "comparison_policy_ref": self.references["comparison-policy"],
            "producer_tool_sha256": self.tool_sha256,
            "rules": [
                {
                    "spec_id": "SPEC-001",
                    "evidence_level": evidence_level,
                    "rule_lifecycle": "candidate",
                    "compatibility_class": "request_egress",
                    "migration_decision": "add",
                    "evidence_refs": [self.evidence_binding],
                    "applicability": ["linux/amd64", "privacy=default"],
                }
            ],
        }
        if schema_version == "official-client-evidence-package/v2":
            evidence["completeness_ref"] = self.references["comparison-policy"]
        self.references["evidence-package"] = self.store.seal_object(
            "evidence_package", evidence
        )
        self.references["evidence-fact"] = self.store.append_fact(
            self.campaign_id,
            "evidence_recorded",
            {
                "discovery_fact_ref": self.references["discovery"],
                "evidence_package_ref": self.references["evidence-package"],
            },
            self._time(),
        )
        self.references["evidence-approval"] = self.store.append_fact(
            self.campaign_id,
            "evidence_approved",
            {
                "evidence_fact_ref": self.references["evidence-fact"],
                "evidence_package_ref": self.references["evidence-package"],
                "reviewer": "synthetic-reviewer",
                "review_ref": "review/evidence-legacy",
            },
            self._time(),
        )

    def profile_objects(self) -> None:
        self.references["operational-evidence"] = self.seal_manifest(
            "operational_evidence", "operational-evidence"
        )
        op_ref = self.references["operational-evidence"]
        self.references["descriptor"] = self.store.seal_object(
            "persona_descriptor",
            {
                "schema_version": "official-client-persona-descriptor/v1",
                "persona": self.persona,
                "routes": ["synthetic-inference"],
                "sinks": ["synthetic-upstream"],
                "explicit_exclusions": ["api-key-auth"],
            },
        )
        self.references["profile-schema"] = self.store.seal_object(
            "profile_schema",
            {
                "schema_version": "official-client-profile-schema/v1",
                "persona": self.persona,
                "version": "1.0.0",
                "schema_id": "synthetic-profile-v1",
                "document": {"fields": ["endpoint", "headers", "body", "transport"]},
            },
        )
        self.references["snapshot"] = self.store.seal_object(
            "snapshot",
            {
                "schema_version": "official-client-snapshot/v1",
                "persona": self.persona,
                "version": "1.0.0",
                "profile_digest": SHA_A,
                "profile_schema_ref": self.references["profile-schema"],
                "compiler_attestation_sha256": SHA_B,
                "document": {"endpoint": "/synthetic", "transport": "h2"},
            },
        )
        self.references["release-bundle"] = self.store.seal_object(
            "release_bundle",
            {
                "schema_version": "official-client-release-bundle/v1",
                "persona": self.persona,
                "version": "1.0.0",
                "profile_digest": SHA_A,
                "snapshot_ref": self.references["snapshot"],
                "endpoint_digest": "3" * 64,
                "transport_digest": "4" * 64,
                "state_digest": "5" * 64,
                "policy_digest": "6" * 64,
                "document": {"endpoint": "/synthetic", "transport": "h2"},
            },
        )
        self.references["release"] = self.store.seal_object(
            "release_artifact",
            {
                "schema_version": "official-client-release-artifact/v1",
                "persona": self.persona,
                "version": "1.0.0",
                "profile_digest": SHA_A,
                "snapshot_ref": self.references["snapshot"],
                "release_bundle_ref": self.references["release-bundle"],
            },
        )
        self.references["ingress-observation"] = self.store.seal_object(
            "ingress_observation",
            {
                "schema_version": "official-client-ingress-observation/v1",
                "persona": self.persona,
                "observed_at_utc": self._time(),
                "source_refs": [op_ref],
                "aliases": [
                    {
                        "alias_id": "alias-official",
                        "logical_ingress_id": "official-cli",
                        "physical_route": "internal://synthetic/official",
                        "caller_ids": ["official-driver"],
                    },
                    {
                        "alias_id": "alias-responses",
                        "logical_ingress_id": "third-responses",
                        "physical_route": "/v1/responses",
                        "caller_ids": ["gateway"],
                    },
                ],
            },
        )
        self.references["ingress-inventory"] = self.store.seal_object(
            "production_ingress_inventory",
            self.ingress_inventory_payload("retained_legacy"),
        )
        self.references["egress-observation"] = self.store.seal_object(
            "egress_observation",
            {
                "schema_version": "official-client-egress-observation/v1",
                "persona": self.persona,
                "observed_at_utc": self._time(),
                "source_refs": [op_ref],
                "egresses": [
                    {
                        "egress_id": "aux-refresh",
                        "route_id": "oauth-refresh",
                        "sink_id": "oauth-provider",
                        "oauth_related": True,
                        "kind": "lifecycle",
                    },
                    {
                        "egress_id": "infer-main",
                        "route_id": "synthetic-inference",
                        "sink_id": "synthetic-upstream",
                        "oauth_related": True,
                        "kind": "inference",
                    },
                    {
                        "egress_id": "unknown-aux",
                        "route_id": "unknown-auxiliary",
                        "sink_id": "synthetic-upstream",
                        "oauth_related": True,
                        "kind": "auxiliary",
                    },
                ],
            },
        )
        self.references["egress-inventory"] = self.store.seal_object(
            "egress_disposition_inventory",
            self.egress_inventory_payload(final=False),
        )
        self.references["boundary"] = self.seal_manifest(
            "operational_evidence", "boundary-assertion", [
                {"id": "outside-fail-close", "facts": {"result": "pass"}}
            ]
        )
        capabilities = sorted(
            [
                self.capability("official-cli", "official"),
                self.capability("third-responses", "responses"),
            ],
            key=capability_key,
        )
        self.references["support"] = self.store.seal_object(
            "support_envelope",
            {
                "schema_version": "official-client-support-envelope/v1",
                "persona": self.persona,
                "capabilities": capabilities,
                "unsupported_conditions": ["privacy=disabled"],
                "target_spec_ids": ["SPEC-001"],
                "production_ingress_inventory_ref": self.references["ingress-inventory"],
                "boundary_assertion_refs": [self.references["boundary"]],
            },
        )
        for kind, manifest_id in (
            ("persona_derivation", "persona-derivation"),
            ("compatibility_boundary", "compatibility-boundary"),
        ):
            self.references[manifest_id] = self.seal_manifest(kind, manifest_id)
        self.references["scenario-plan"] = self.store.seal_object(
            "scenario_plan",
            {
                "schema_version": "official-client-scenario-plan/v1",
                "persona": self.persona,
                "manifest_id": "scenario-plan",
                "scenarios": [
                    {
                        "id": "baseline",
                        "spec_ids": ["SPEC-001"],
                        "ingress_protocol_classes": ["official", "responses"],
                        "conditions": ["privacy=default"],
                        "assertion_ids": ["wire-convergence"],
                    }
                ],
            },
        )
        self.references["deployment-plan"] = self.store.seal_object(
            "deployment_plan",
            {
                "schema_version": "official-client-deployment-plan/v1",
                "persona": self.persona,
                "manifest_id": "deployment-plan",
                "active_support_capabilities": capabilities,
                "rollback_operational_capabilities": capabilities,
                "deployment_traffic_capabilities": capabilities,
                "rollback_target_kind": "legacy_deployment",
                "failure_policy": "persona_fail_close",
            },
        )

    def capability(self, ingress_id: str, protocol: str) -> dict[str, Any]:
        return {
            "platform": "linux/amd64",
            "logical_ingress_id": ingress_id,
            "protocol_class": protocol,
            "privacy_mode": "default",
            "model": "synthetic-model",
            "feature": "baseline",
            "endpoint": "/synthetic",
        }

    def ingress_inventory_payload(self, disposition: str) -> dict[str, Any]:
        evidence = [self.references["operational-evidence"]]
        return {
            "schema_version": "official-client-production-ingress-inventory/v1",
            "persona": self.persona,
            "observation_ref": self.references["ingress-observation"],
            "entries": [
                {
                    "logical_ingress_id": "official-cli",
                    "physical_alias_ids": ["alias-official"],
                    "caller_ids": ["official-driver"],
                    "adapter_id": "official-adapter",
                    "route_id": "official-route",
                    "ingress_kind": "official",
                    "protocol_class": "official",
                    "current_disposition": disposition,
                    "evidence_refs": evidence,
                },
                {
                    "logical_ingress_id": "third-responses",
                    "physical_alias_ids": ["alias-responses"],
                    "caller_ids": ["gateway"],
                    "adapter_id": "responses-adapter",
                    "route_id": "responses-route",
                    "ingress_kind": "third_party",
                    "protocol_class": "responses",
                    "current_disposition": disposition,
                    "evidence_refs": evidence,
                },
            ],
        }

    @staticmethod
    def managed_policy() -> dict[str, str]:
        return {
            "authentication": "trusted-oauth-refresh",
            "endpoint": "/oauth/token",
            "client": "managed-refresh-client",
            "timeout_policy": "10s",
            "retry_policy": "bounded-two-attempts",
            "secret_policy": "memory-only",
            "audit_policy": "metadata-only",
        }

    def egress_inventory_payload(self, *, final: bool) -> dict[str, Any]:
        evidence = [self.references["operational-evidence"]]
        return {
            "schema_version": "official-client-egress-disposition-inventory/v1",
            "persona": self.persona,
            "observation_ref": self.references["egress-observation"],
            "entries": [
                {
                    "egress_id": "aux-refresh",
                    "current_disposition": "non_persona_managed",
                    "current_guard_state": "enforced" if final else "legacy_observe",
                    "spec_ids": [],
                    "managed_policy": self.managed_policy(),
                    "runtime_assertion_refs": evidence,
                },
                {
                    "egress_id": "infer-main",
                    "current_disposition": "persona_strict" if final else "denied",
                    "current_guard_state": "enforced" if final else "legacy_observe",
                    "spec_ids": ["SPEC-001"] if final else [],
                    "managed_policy": None,
                    "runtime_assertion_refs": evidence,
                },
                {
                    "egress_id": "unknown-aux",
                    "current_disposition": "denied",
                    "current_guard_state": "enforced" if final else "legacy_observe",
                    "spec_ids": [],
                    "managed_policy": None,
                    "runtime_assertion_refs": evidence,
                },
            ],
        }

    def profile_approve(self, purpose: str = "production_replacement") -> None:
        evidence = [self.references["operational-evidence"]]
        payload = {
            "evidence_approval_ref": self.references["evidence-approval"],
            "approval_purpose": purpose,
            "persona_descriptor_ref": self.references["descriptor"],
            "profile_schema_ref": self.references["profile-schema"],
            "snapshot_ref": self.references["snapshot"],
            "release_artifact_ref": self.references["release"],
            "support_envelope_ref": self.references["support"],
            "production_ingress_inventory_ref": self.references["ingress-inventory"],
            "egress_disposition_inventory_ref": self.references["egress-inventory"],
            "persona_derivation_ref": self.references["persona-derivation"],
            "compatibility_boundary_ref": self.references["compatibility-boundary"],
            "scenario_plan_ref": self.references["scenario-plan"],
            "deployment_plan_ref": self.references["deployment-plan"],
            "target_spec_ids": ["SPEC-001"],
            "ingress_target_dispositions": [
                {
                    "logical_ingress_id": "official-cli",
                    "target_disposition": "migrated_strict",
                    "evidence_refs": evidence,
                },
                {
                    "logical_ingress_id": "third-responses",
                    "target_disposition": "migrated_strict",
                    "evidence_refs": evidence,
                },
            ],
            "egress_target_dispositions": [
                {
                    "egress_id": "aux-refresh",
                    "target_disposition": "non_persona_managed",
                    "spec_ids": [],
                    "managed_policy": self.managed_policy(),
                    "evidence_refs": evidence,
                },
                {
                    "egress_id": "infer-main",
                    "target_disposition": "persona_strict",
                    "spec_ids": ["SPEC-001"],
                    "managed_policy": None,
                    "evidence_refs": evidence,
                },
                {
                    "egress_id": "unknown-aux",
                    "target_disposition": "denied",
                    "spec_ids": [],
                    "managed_policy": None,
                    "evidence_refs": evidence,
                },
            ],
            "reviewer": "synthetic-reviewer",
            "review_ref": "review/profile-1",
            "identity_sha256": "",
        }
        payload["identity_sha256"] = profile_approval_identity_sha256(payload)
        self.references["profile-approval"] = self.store.append_fact(
            self.campaign_id, "profile_approved", payload, self._time()
        )

    def candidate_and_accept(self) -> None:
        self.freeze_candidate()
        self.run_scenario()
        self.record_pair_and_accept()

    def freeze_candidate(self) -> None:
        candidate = {
            "candidate_id": "synthetic-candidate-1",
            "profile_approval_ref": self.references["profile-approval"],
            "release_artifact_ref": self.references["release"],
            "support_envelope_ref": self.references["support"],
            "source_tree_sha256": "7" * 64,
            "test_tree_sha256": "8" * 64,
            "dependency_lock_sha256": "9" * 64,
            "target_architecture": "linux/amd64",
            "build_id": "synthetic-build-1",
            "image_digest": TARGET_IMAGE,
            "candidate_purpose": "production_replacement",
            "identity_sha256": "",
        }
        candidate["identity_sha256"] = candidate_identity_sha256(candidate)
        self.references["candidate"] = self.store.append_fact(
            self.campaign_id, "candidate_frozen", candidate, self._time()
        )

    def run_scenario(self, capture_result: str = "pass", attempt_id: str = "attempt-1") -> None:
        previous: dict[str, Any] | None = None
        for stage, kind, result in (
            ("prepare", "scenario_prepared", "prepared"),
            ("capture", "scenario_captured", capture_result),
            ("seal", "scenario_sealed", "pass"),
            ("approve", "scenario_approved", "pass"),
        ):
            payload: dict[str, Any] = {
                "candidate_id": "synthetic-candidate-1",
                "scenario_id": "baseline",
                "attempt_id": attempt_id,
                "stage": stage,
                "previous_stage_ref": previous,
                "artifact_refs": [] if stage == "prepare" else [
                    self.references["operational-evidence"]
                ],
                "result": result,
            }
            if stage == "approve":
                payload |= {
                    "reviewer": "synthetic-reviewer",
                    "review_ref": "review/scenario-1",
                }
            previous = self.store.append_fact(
                self.campaign_id, kind, payload, self._time()
            )
            if result == "failed":
                self.references["failed-scenario-stage"] = previous
                return
        self.references["scenario-approval"] = previous

    def record_pair_and_accept(self) -> None:
        self.references["pair"] = self.store.append_fact(
            self.campaign_id,
            "pair_recorded",
            {
                "pair_id": "PAIR-SPEC-001",
                "spec_id": "SPEC-001",
                "candidate_id": "synthetic-candidate-1",
                "release_artifact_ref": self.references["release"],
                "condition_sha256": SHA_C,
                "scenario_approval_refs": [self.references["scenario-approval"]],
                "official_result": {
                    "ingress_id": "official-cli",
                    "translation": "lossless",
                    "result": "pass",
                    "final_wire_sha256": SHA_A,
                },
                "third_party_results": [
                    {
                        "protocol_class": "responses",
                        "ingress_id": "third-responses",
                        "translation": "lossless",
                        "result": "pass",
                        "final_wire_sha256": SHA_A,
                    }
                ],
                "dynamic_field_checks": [
                    {
                        "id": "dynamic-session",
                        "dimensions": ["format", "lifecycle", "relation", "source"],
                        "result": "pass",
                    }
                ],
            },
            self._time(),
        )
        self.references["acceptance"] = self.store.append_fact(
            self.campaign_id,
            "acceptance_recorded",
            {
                "candidate_id": "synthetic-candidate-1",
                "profile_approval_ref": self.references["profile-approval"],
                "candidate_ref": self.references["candidate"],
                "pair_refs": [self.references["pair"]],
                "boundary_assertion_refs": [self.references["boundary"]],
                "inventory_assertion_refs": [self.references["operational-evidence"]],
                "acceptance_purpose": "production_replacement",
                "result": "accepted",
            },
            self._time(),
        )

    def candidate_build_input(self, tag: str = "strict") -> dict[str, Any]:
        """VC-4 已封存构建输入；同一 tag 的模型目录只封存一次（重试 finalizer 时复用）。"""

        def key(name: str) -> str:
            return name if tag == "strict" else f"{name}-{tag}"

        candidate_purpose = self.store.load_fact(
            self.references["profile-approval"]
        )["payload"]["approval_purpose"]
        if key("model-catalog") not in self.references:
            self.seal_manifest("model_capability_catalog", key("model-catalog"))
        artifact_entries = [
            {
                "role": "backend",
                "binding": self.candidate_binding,
                "source_sha256": "7" * 64,
            }
        ]
        validation_scope_sha256 = canonical_sha256(
            {"execute": ["baseline", "gate-runtime"], "reuse": []}
        )
        build_input = {
            "candidate_id": f"synthetic-candidate-{tag}",
            "candidate_purpose": candidate_purpose,
            "profile_approval_ref": self.references["profile-approval"],
            "release_artifact_ref": self.references["release"],
            "support_envelope_ref": self.references["support"],
            "source_identity": {
                "git_commit": "3" * 40,
                "source_tree_sha256": "7" * 64,
                "test_tree_sha256": "8" * 64,
                "dependency_lock_sha256": "9" * 64,
                "clean": True,
            },
            "profile_identity": {
                "profile_schema_ref": self.references["profile-schema"],
                "snapshot_ref": self.references["snapshot"],
                "release_artifact_ref": self.references["release"],
                "model_capability_catalog_ref": self.references[key("model-catalog")],
                "support_envelope_ref": self.references["support"],
                "profile_digest": SHA_A,
            },
            "validation_scope_sha256": validation_scope_sha256,
            "build_identity": {
                "build_id": f"synthetic-build-{tag}",
                "deployed_version": "1.0.0",
                "target_architecture": "linux/amd64",
                "toolchain": ["go-1.25", "node-24"],
                "parameters": {"offline": True, "target": "linux/amd64"},
                "parameters_sha256": canonical_sha256(
                    {"offline": True, "target": "linux/amd64"}
                ),
            },
            "artifact_inventory": {
                "entries": artifact_entries,
                "file_count": len(artifact_entries),
                "total_bytes": sum(
                    item["binding"]["bytes"] for item in artifact_entries
                ),
                "inventory_sha256": canonical_sha256(artifact_entries),
            },
            "image": {
                "reference": f"registry.local/sub2api@{TARGET_IMAGE}",
                "image_digest": TARGET_IMAGE,
                "image_id": f"sha256:{'3' * 64}",
                "manifest_digest": TARGET_IMAGE,
            },
            "completed_at_utc": self._time(),
        }
        return build_input

    def strict_validate(
        self,
        *,
        finalize: bool = True,
        gate_result: str = "pass",
        stop_after_gate: bool = False,
        tag: str = "strict",
    ) -> None:
        """按 VC-4／VC-5 严格合同冻结 Candidate 并生成完成事实。

        ``tag`` 区分同一 Campaign 内的多轮候选（例如换 revision 后的新候选）；缺省 ``strict`` 时全部标识符与
        引用键与原夹具逐字相同。
        """

        def key(name: str) -> str:
            return name if tag == "strict" else f"{name}-{tag}"

        build_input = self.candidate_build_input(tag)
        candidate_purpose = build_input["candidate_purpose"]
        validation_scope_sha256 = build_input["validation_scope_sha256"]
        self.references[key("candidate-build-receipt")] = finalize_candidate_build(
            self.store, self.campaign_id, build_input
        )
        candidate = {
            "schema_version": "official-client-validation-candidate/v2",
            "candidate_id": f"synthetic-candidate-{tag}",
            "profile_approval_ref": self.references["profile-approval"],
            "release_artifact_ref": self.references["release"],
            "support_envelope_ref": self.references["support"],
            "source_tree_sha256": "7" * 64,
            "test_tree_sha256": "8" * 64,
            "dependency_lock_sha256": "9" * 64,
            # 候选绑定本轮构建输入，便于独立夹具提供真实架构、镜像与 Profile 字节。
            "target_architecture": build_input["build_identity"]["target_architecture"],
            "build_id": build_input["build_identity"]["build_id"],
            "image_digest": build_input["image"]["image_digest"],
            "candidate_purpose": candidate_purpose,
            "candidate_build_receipt_ref": self.references[key("candidate-build-receipt")],
            "identity_sha256": "",
        }
        candidate["identity_sha256"] = candidate_identity_sha256(candidate)
        self.references[f"candidate-{tag}"] = self.store.append_fact(
            self.campaign_id, "candidate_frozen", candidate, self._time()
        )
        self.references[key("validation-runtime-before")] = self.store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": self.persona,
                "catalog_digest": "d" * 64,
                "production_active_ref": None,
                "production_rollback_ref": None,
                "observed_at_utc": self._time(),
                "source_ref": self.runtime_binding,
            },
        )
        self.references[key("validation-selector-before")] = self.store.append_fact(
            self.campaign_id,
            "selector_observed",
            {
                "catalog_snapshot_ref": self.references[key("validation-runtime-before")],
                "observation_kind": "read_only",
            },
            self._time(),
        )
        run_conditions = {
            "run_nonce": f"nonce-{tag}-1",
            "environment_sha256": "4" * 64,
            "tool_sha256": self.tool_sha256,
            "configuration_sha256": "5" * 64,
            "network_sha256": "6" * 64,
            "account_model_sha256": "a" * 64,
        }
        validation_plan = {
            "schema_version": "official-client-validation-execution-plan/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "plan_id": f"validation-plan-{tag}",
            "candidate_id": f"synthetic-candidate-{tag}",
            "candidate_ref": self.references[f"candidate-{tag}"],
            "candidate_build_receipt_ref": self.references[key("candidate-build-receipt")],
            "profile_approval_ref": self.references["profile-approval"],
            "scenario_plan_ref": self.references["scenario-plan"],
            "atomic_assertion_ledger_ref": self.references[
                "atomic-assertion-ledger"
            ],
            "validation_scope_sha256": validation_scope_sha256,
            "execute_item_ids": ["baseline", "gate-runtime"],
            "reuse_item_ids": [],
            "external_gates": [
                {
                    "gate_id": "gate-runtime",
                    "requirement_sha256": "b" * 64,
                    "source": "execute",
                    "command": ["python3", "-m", "synthetic_gate"],
                    "working_directory": "/srv/sub2api",
                    "host_id": "dmit-synthetic",
                    "architecture": "linux/amd64",
                    "reused_receipt_ref": None,
                    "previous_failed_receipt_ref": None,
                }
            ],
            "run_conditions": run_conditions,
            "production_selector_before_ref": self.references[key("validation-selector-before")],
            "vircs_state": {
                "ownership": "operator_managed",
                "verification": "unverified",
                "action": "not_touched",
            },
            "identity_sha256": "",
        }
        validation_plan["identity_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in validation_plan.items()
                if key != "identity_sha256"
            }
        )
        self.references[key("validation-plan")] = self.store.seal_object(
            "validation_execution_plan", validation_plan
        )
        attempt = {
            "schema_version": "official-client-validation-attempt/v1",
            "attempt_id": f"attempt-{tag}-1",
            "candidate_id": f"synthetic-candidate-{tag}",
            "candidate_ref": self.references[f"candidate-{tag}"],
            "candidate_build_receipt_ref": self.references[key("candidate-build-receipt")],
            "validation_execution_plan_ref": self.references[key("validation-plan")],
            "run_conditions": run_conditions,
            "production_selector_before_ref": self.references[key("validation-selector-before")],
            "vircs_state": validation_plan["vircs_state"],
            "result": "created",
            "identity_sha256": "",
        }
        attempt["identity_sha256"] = canonical_sha256(
            {key: value for key, value in attempt.items() if key != "identity_sha256"}
        )
        self.references[key("validation-attempt")] = self.store.append_fact(
            self.campaign_id, "validation_attempt_created", attempt, self._time()
        )
        previous: dict[str, Any] | None = None
        for stage, kind, result in (
            ("prepare", "scenario_prepared", "prepared"),
            ("capture", "scenario_captured", "pass"),
            ("seal", "scenario_sealed", "pass"),
            ("approve", "scenario_approved", "pass"),
        ):
            scenario: dict[str, Any] = {
                "schema_version": "official-client-scenario-stage/v2",
                "candidate_id": f"synthetic-candidate-{tag}",
                "scenario_id": "baseline",
                "attempt_id": f"attempt-{tag}-1",
                "validation_attempt_ref": self.references[key("validation-attempt")],
                "stage": stage,
                "previous_stage_ref": previous,
                "artifact_refs": []
                if stage == "prepare"
                else [self.references["operational-evidence"]],
                "result": result,
            }
            if stage == "approve":
                scenario |= {
                    "reviewer": "synthetic-reviewer",
                    "review_ref": f"review/scenario-{tag}",
                }
            previous = self.store.append_fact(
                self.campaign_id, kind, scenario, self._time()
            )
        self.references[f"scenario-approval-{tag}"] = previous
        evidence_package = {
            "schema_version": "official-client-candidate-evidence-package/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "target_version": "1.0.0",
            "package_id": f"candidate-evidence-{tag}",
            "candidate_id": f"synthetic-candidate-{tag}",
            "attempt_id": f"attempt-{tag}-1",
            "candidate_ref": self.references[f"candidate-{tag}"],
            "candidate_build_receipt_ref": self.references[key("candidate-build-receipt")],
            "validation_attempt_ref": self.references[key("validation-attempt")],
            "atomic_assertion_ledger_ref": self.references[
                "atomic-assertion-ledger"
            ],
            "scenario_approval_refs": [self.references[f"scenario-approval-{tag}"]],
            "scenario_assertion_results": [],
            "raw_evidence_inventory": [self.evidence_binding],
            "environment_before_ref": self.references["operational-evidence"],
            "environment_after_ref": self.references["operational-evidence"],
            "recovery_evidence_ref": self.references["operational-evidence"],
            "secret_scan_evidence_ref": self.references["operational-evidence"],
            "artifact_count": 1,
            "inventory_sha256": canonical_sha256([self.evidence_binding]),
            "identity_sha256": "",
        }
        evidence_package["identity_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in evidence_package.items()
                if key != "identity_sha256"
            }
        )
        self.references[key("candidate-evidence-package")] = self.store.seal_object(
            "candidate_evidence_package", evidence_package
        )
        self.references[key("validation-gate-receipt")] = finalize_validation_gate(
            self.store,
            self.campaign_id,
            self.references[key("validation-attempt")],
            {
                "gate_id": "gate-runtime",
                "started_at_utc": self._time(),
                "completed_at_utc": self._time(),
                "exit_code": 0 if gate_result == "pass" else 1,
                "output_sha256": "c" * 64,
                "result": gate_result,
            },
        )
        if stop_after_gate:
            return
        self.references[f"pair-{tag}"] = self.store.append_fact(
            self.campaign_id,
            "pair_recorded",
            {
                "schema_version": "official-client-pair/v2",
                "pair_id": "PAIR-SPEC-001",
                "spec_id": "SPEC-001",
                "candidate_id": f"synthetic-candidate-{tag}",
                "profile_approval_ref": self.references["profile-approval"],
                "release_artifact_ref": self.references["release"],
                "condition_sha256": SHA_C,
                "scenario_approval_refs": [
                    self.references[f"scenario-approval-{tag}"]
                ],
                "candidate_evidence_package_ref": self.references[key("candidate-evidence-package")],
                "atomic_assertion_ledger_ref": self.references[
                    "atomic-assertion-ledger"
                ],
                "official_result": {
                    "ingress_id": "official-cli",
                    "translation": "lossless",
                    "result": "pass",
                    "final_wire_sha256": SHA_A,
                    "evidence_refs": [self.references["operational-evidence"]],
                },
                "candidate_result": {
                    "result": "pass",
                    "final_wire_sha256": SHA_A,
                    "evidence_refs": [self.references["operational-evidence"]],
                },
                "third_party_results": [
                    {
                        "protocol_class": "responses",
                        "ingress_id": "third-responses",
                        "translation": "lossless",
                        "result": "pass",
                        "final_wire_sha256": SHA_A,
                        "evidence_refs": [self.references["operational-evidence"]],
                    }
                ],
                "dynamic_field_checks": [
                    {
                        "id": "dynamic-session",
                        "dimensions": ["format", "lifecycle", "relation", "source"],
                        "result": "pass",
                    }
                ],
                "assertion_results": [
                    {
                        "assertion_id": "ASSERT-SPEC-001-001",
                        "owner_kind": "required_rule",
                        "owner_id": "SPEC-001",
                        "result": "pass",
                        "source": "execute",
                    }
                ],
                "implementation_anchors": ["backend/synthetic.go:1"],
                "test_anchors": ["backend/synthetic_test.go:1"],
                "result_source": "execute",
                "comparison_mode": "dual_wire",
            },
            self._time(),
        )
        acceptance = {
            "schema_version": "official-client-acceptance/v2",
            "candidate_id": f"synthetic-candidate-{tag}",
            "profile_approval_ref": self.references["profile-approval"],
            "candidate_ref": self.references[f"candidate-{tag}"],
            "candidate_build_receipt_ref": self.references[key("candidate-build-receipt")],
            "validation_attempt_ref": self.references[key("validation-attempt")],
            "candidate_evidence_package_ref": self.references[key("candidate-evidence-package")],
            "atomic_assertion_ledger_ref": self.references[
                "atomic-assertion-ledger"
            ],
            "pair_refs": [self.references[f"pair-{tag}"]],
            "boundary_assertion_refs": [self.references["boundary"]],
            "inventory_assertion_refs": [self.references["operational-evidence"]],
            "external_gate_receipt_refs": [
                self.references[key("validation-gate-receipt")]
            ],
            "acceptance_purpose": candidate_purpose,
            "result": (
                "accepted"
                if candidate_purpose == "production_replacement"
                else "validation_only"
            ),
            "identity_sha256": "",
        }
        acceptance["identity_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in acceptance.items()
                if key != "identity_sha256"
            }
        )
        self.references[f"acceptance-{tag}"] = self.store.append_fact(
            self.campaign_id, "acceptance_recorded", acceptance, self._time()
        )
        self.references[key("validation-selector-after")] = self.store.append_fact(
            self.campaign_id,
            "selector_observed",
            {
                "catalog_snapshot_ref": self.references[key("validation-runtime-before")],
                "observation_kind": "read_only",
            },
            self._time(),
        )
        if finalize:
            self.references[key("validation-completed")] = finalize_validation(
                self.store,
                self.campaign_id,
                self.references[f"acceptance-{tag}"],
                self.references[key("validation-selector-after")],
                self._time(),
            )

    def strict_deliver(self, *, fail_stage: str | None = None) -> None:
        """按 VC-6 严格合同记录 DMIT 四阶段并签发候选交付收据。"""

        stage_checks = {
            "candidate_active": ["health", "smoke"],
            "rollback_verified": ["auth", "data", "dependencies", "health"],
            "candidate_restored": ["health", "profile", "smoke"],
            "stable_observed": ["identity", "safety", "state"],
        }
        plan = {
            "schema_version": "official-client-candidate-delivery-plan/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "plan_id": "delivery-plan-strict",
            "delivery_id": "delivery-strict-1",
            "candidate_id": "synthetic-candidate-strict",
            "candidate_ref": self.references["candidate-strict"],
            "candidate_build_receipt_ref": self.references[
                "candidate-build-receipt"
            ],
            "validation_completed_ref": self.references["validation-completed"],
            "acceptance_ref": self.references["acceptance-strict"],
            "candidate_evidence_package_ref": self.references[
                "candidate-evidence-package"
            ],
            "host_id": "dmit-synthetic",
            "architecture": "linux/amd64",
            "compose_sha256": "d" * 64,
            "environment_sha256": "4" * 64,
            "configuration_sha256": "5" * 64,
            "network_sha256": "6" * 64,
            "runtime_profile_sha256": SHA_A,
            "rollback_runtime_profile_sha256": "e" * 64,
            "candidate_image_digest": TARGET_IMAGE,
            "rollback_image_digest": ROLLBACK_IMAGE,
            "observation_window_seconds": 60,
            "stage_checks": stage_checks,
            "environment_freeze_ref": self.references["operational-evidence"],
            "rollback_material_refs": [self.references["operational-evidence"]],
            "vircs_state": {
                "ownership": "operator_managed",
                "verification": "unverified",
                "action": "not_touched",
            },
            "identity_sha256": "",
        }
        plan["identity_sha256"] = canonical_sha256(
            {key: value for key, value in plan.items() if key != "identity_sha256"}
        )
        self.references["delivery-plan"] = self.store.seal_object(
            "candidate_delivery_plan", plan
        )
        delivery_refs: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        failure_evidence_ref: dict[str, Any] | None = None
        if fail_stage is not None:
            failure_evidence_ref = self.seal_manifest(
                "operational_evidence",
                f"delivery-{fail_stage}-failure",
                [{"id": "stage-failure", "facts": {"result": "failed"}}],
            )
        for stage in (
            "candidate_active",
            "rollback_verified",
            "candidate_restored",
            "stable_observed",
        ):
            is_rollback = stage == "rollback_verified"
            observed_seconds = (
                60 if stage == "stable_observed" and stage != fail_stage else 0
            )
            payload = {
                "schema_version": "official-client-candidate-delivery-fact/v1",
                "delivery_id": "delivery-strict-1",
                "stage": stage,
                "candidate_id": "synthetic-candidate-strict",
                "candidate_ref": self.references["candidate-strict"],
                "candidate_build_receipt_ref": self.references[
                    "candidate-build-receipt"
                ],
                "validation_completed_ref": self.references[
                    "validation-completed"
                ],
                "acceptance_ref": self.references["acceptance-strict"],
                "delivery_plan_ref": self.references["delivery-plan"],
                "previous_stage_ref": previous,
                "host_id": "dmit-synthetic",
                "architecture": "linux/amd64",
                "compose_sha256": "d" * 64,
                "environment_sha256": "4" * 64,
                "configuration_sha256": "5" * 64,
                "network_sha256": "6" * 64,
                "runtime_profile_sha256": "e" * 64 if is_rollback else SHA_A,
                "image_digest": ROLLBACK_IMAGE if is_rollback else TARGET_IMAGE,
                "check_ids": stage_checks[stage],
                "evidence_refs": [
                    failure_evidence_ref
                    if stage == fail_stage
                    else self.references["operational-evidence"]
                ],
                "observed_seconds": observed_seconds,
                "result": "failed" if stage == fail_stage else "pass",
                "identity_sha256": "",
            }
            payload["identity_sha256"] = canonical_sha256(
                {key: value for key, value in payload.items() if key != "identity_sha256"}
            )
            previous = self.store.append_fact(
                self.campaign_id, "candidate_delivery_recorded", payload, self._time()
            )
            delivery_refs.append(previous)
            if stage == fail_stage:
                self.references["delivery-fact-refs"] = delivery_refs
                self.references["failed-delivery-stage"] = previous
                return
        self.references["delivery-fact-refs"] = delivery_refs
        archive = {
            "schema_version": "official-client-private-archive-manifest/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "manifest_id": "private-archive-strict",
            "redacted": True,
            "entries": [self.private_binding],
            "entry_count": 1,
            "total_bytes": self.private_binding["bytes"],
            "manifest_sha256": canonical_sha256([self.private_binding]),
            "identity_sha256": "",
        }
        archive["identity_sha256"] = canonical_sha256(
            {key: value for key, value in archive.items() if key != "identity_sha256"}
        )
        self.references["private-archive"] = self.store.seal_object(
            "private_archive_manifest", archive
        )
        package = {
            "schema_version": "official-client-candidate-delivery-package/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "package_id": "delivery-package-strict",
            "delivery_id": "delivery-strict-1",
            "candidate_id": "synthetic-candidate-strict",
            "candidate_ref": self.references["candidate-strict"],
            "candidate_build_receipt_ref": self.references[
                "candidate-build-receipt"
            ],
            "validation_completed_ref": self.references["validation-completed"],
            "acceptance_ref": self.references["acceptance-strict"],
            "candidate_evidence_package_ref": self.references[
                "candidate-evidence-package"
            ],
            "delivery_plan_ref": self.references["delivery-plan"],
            "delivery_fact_refs": delivery_refs,
            "deployment_configuration_ref": self.references["operational-evidence"],
            "public_evidence_index_refs": [self.references["operational-evidence"]],
            "rollback_material_refs": [self.references["operational-evidence"]],
            "private_archive_manifest_ref": self.references["private-archive"],
            "candidate_image_digest": TARGET_IMAGE,
            "artifact_count": 3,
            "identity_sha256": "",
        }
        package["identity_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "identity_sha256"}
        )
        self.references["delivery-package"] = self.store.seal_object(
            "candidate_delivery_package", package
        )
        self.references["candidate-delivery-receipt"] = finalize_candidate_delivery(
            self.store, self.campaign_id, self.references["delivery-package"]
        )

    def promote(self) -> None:
        self.references["promotion-diff"] = self.seal_manifest(
            "promotion_diff",
            "promotion-diff",
            [{"id": "candidate-to-production", "facts": {"wire_delta": "none"}}],
        )
        self.references["promotion-fact"] = self.store.append_fact(
            self.campaign_id,
            "release_promoted",
            {
                "candidate_ref": self.references["candidate"],
                "acceptance_ref": self.references["acceptance"],
                "release_artifact_ref": self.references["release"],
                "promotion_diff_ref": self.references["promotion-diff"],
            },
            self._time(),
        )
        self.references["promotion-receipt"] = finalize_promotion(
            self.store, self.campaign_id, self.references["promotion-fact"]
        )

    def deploy(self, *, finalize_activation_receipt: bool = True) -> None:
        op_ref = self.references["operational-evidence"]
        self.references["runtime-before"] = self.store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": self.persona,
                "catalog_digest": "d" * 64,
                "production_active_ref": None,
                "production_rollback_ref": None,
                "observed_at_utc": self._time(),
                "source_ref": self.runtime_binding,
            },
        )
        self.references["selector-before"] = self.store.append_fact(
            self.campaign_id,
            "selector_observed",
            {
                "catalog_snapshot_ref": self.references["runtime-before"],
                "observation_kind": "read_only",
            },
            self._time(),
        )
        self.references["active-envelope"] = self.store.seal_object(
            "active_support_envelope",
            {
                "schema_version": "official-client-active-support-envelope/v1",
                "persona": self.persona,
                "support_envelope_ref": self.references["support"],
                "profile_approval_ref": self.references["profile-approval"],
                "acceptance_ref": self.references["acceptance"],
                "release_artifact_ref": self.references["release"],
                "capabilities": self.store.load_object(self.references["support"])["payload"][
                    "capabilities"
                ],
            },
        )
        self.references["rollback-envelope"] = self.store.seal_object(
            "rollback_operational_envelope",
            {
                "schema_version": "official-client-rollback-operational-envelope/v1",
                "persona": self.persona,
                "rollback_kind": "legacy_deployment",
                "rollback_release_ref": None,
                "operational_bindings": {
                    "image_digest": ROLLBACK_IMAGE,
                    "selector_snapshot_ref": self.references["runtime-before"],
                    "configuration_ref": op_ref,
                    "dependencies_ref": op_ref,
                    "routes_ref": op_ref,
                    "exercise_receipt_ref": op_ref,
                },
                "capabilities": self.store.load_object(self.references["support"])["payload"][
                    "capabilities"
                ],
                "wire_evidence_scope": "diagnostic_only",
            },
        )
        self.references["traffic-envelope"] = self.store.seal_object(
            "deployment_traffic_envelope",
            {
                "schema_version": "official-client-deployment-traffic-envelope/v1",
                "persona": self.persona,
                "active_support_envelope_ref": self.references["active-envelope"],
                "rollback_operational_envelope_ref": self.references["rollback-envelope"],
                "production_ingress_inventory_ref": self.references["ingress-inventory"],
                "capabilities": self.store.load_object(self.references["support"])["payload"][
                    "capabilities"
                ],
            },
        )
        self.references["runtime-after"] = self.store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": self.persona,
                "catalog_digest": "e" * 64,
                "production_active_ref": self.references["release"],
                "production_rollback_ref": None,
                "observed_at_utc": self._time(),
                "source_ref": self.runtime_binding,
            },
        )
        previous: dict[str, Any] | None = None
        for stage in (
            "accepted_not_activated",
            "canary_passed",
            "active",
            "rollback_verified",
            "restored_active",
        ):
            runtime_ref = (
                self.references["runtime-before"]
                if stage in {"accepted_not_activated", "canary_passed", "rollback_verified"}
                else self.references["runtime-after"]
            )
            image = ROLLBACK_IMAGE if stage == "rollback_verified" else TARGET_IMAGE
            previous = self.store.append_fact(
                self.campaign_id,
                stage,
                {
                    "deployment_id": "synthetic-deployment-1",
                    "stage": stage,
                    "candidate_id": "synthetic-candidate-1",
                    "previous_stage_ref": previous,
                    "acceptance_ref": self.references["acceptance"],
                    "promotion_receipt_ref": self.references["promotion-receipt"],
                    "active_support_envelope_ref": self.references["active-envelope"],
                    "rollback_operational_envelope_ref": self.references["rollback-envelope"],
                    "deployment_traffic_envelope_ref": self.references["traffic-envelope"],
                    "runtime_catalog_snapshot_ref": runtime_ref,
                    "image_digest": image,
                    "evidence_refs": [op_ref],
                },
                self._time(),
            )
            self.references[f"deployment-{stage}"] = previous
            if stage == "active":
                self.references["selector-after"] = self.store.append_fact(
                    self.campaign_id,
                    "selector_activated",
                    {
                        "catalog_snapshot_ref": self.references["runtime-after"],
                        "observation_kind": "activated",
                        "deployment_ref": previous,
                    },
                    self._time(),
                )
        self.references["final-ingress"] = self.store.seal_object(
            "production_ingress_inventory",
            self.ingress_inventory_payload("migrated_strict"),
        )
        self.references["final-egress"] = self.store.seal_object(
            "egress_disposition_inventory",
            self.egress_inventory_payload(final=True),
        )
        self.references["inventory-current"] = self.store.append_fact(
            self.campaign_id,
            "inventory_current_appended",
            {
                "deployment_ref": self.references["deployment-restored_active"],
                "production_ingress_inventory_ref": self.references["final-ingress"],
                "egress_disposition_inventory_ref": self.references["final-egress"],
            },
            self._time(),
        )
        if finalize_activation_receipt:
            self.references["activation-receipt"] = finalize_activation(
                self.store,
                self.campaign_id,
                self.references["deployment-restored_active"],
                self.references["selector-before"],
                self.references["selector-after"],
                self.references["inventory-current"],
            )

    def complete(self) -> None:
        self.bootstrap_and_campaign()
        self.discovery_and_evidence()
        self.profile_objects()
        self.profile_approve()
        self.candidate_and_accept()
        self.promote()
        self.deploy()

    def dump_reference(self, name: str, path: Path) -> None:
        path.write_bytes(canonical_json_bytes(self.references[name]))

    def debug_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    # ------------------------------------------------------------------
    # Claude 升级 B3-05／B4-04／B4-07 的夹具（只增不改：以上原有方法的行为不变）
    # ------------------------------------------------------------------

    def candidate_invalidation_payload(
        self, candidate_id: str, *, reason: str = "候选源码需要修改"
    ) -> dict[str, Any]:
        """作废当前候选 revision 的 payload（B3-05），review_sha256 按审核内容现算。"""

        campaign = self.store.load_campaign(self.campaign_id)
        revisions = candidate_revision_facts(self.store, campaign)
        current = revisions[-1] if revisions else None
        payload: dict[str, Any] = {
            "campaign_id": self.campaign_id,
            "candidate_id": candidate_id,
            "revision": current["payload"]["revision"] if current else 1,
            "revision_ref": self.store.fact_ref(current) if current else None,
            "reason": reason,
            "evidence_refs": [self.references["operational-evidence"]],
            "approved_by": "synthetic-reviewer",
            "approved_at_utc": self._time(),
            "review_sha256": "",
        }
        payload["review_sha256"] = invalidation_review_sha256(payload)
        return payload

    def invalidate_candidate(self, candidate_id: str) -> dict[str, Any]:
        """封存候选作废对象：当前 revision 进入 revision_required。"""

        reference = self.store.seal_object(
            "candidate_invalidation", self.candidate_invalidation_payload(candidate_id)
        )
        self.references[f"candidate-invalidation-{candidate_id}"] = reference
        return reference

    def baseline_commit(self, number: int, tag: str = "strict") -> str:
        """合成的评估基线 COMMIT 摘要（编排器侧 COMMIT 的 commit_sha256 在 Store 里只作不透明摘要）。"""

        return canonical_sha256({"candidate": f"synthetic-candidate-{tag}", "evaluation_baseline": number})

    def open_evaluation_baseline(
        self, number: int, *, tag: str = "strict", kind: str = "evaluator-only"
    ) -> dict[str, Any]:
        """在当前候选 revision 内为 tag 候选开评估基线 b<number>（B4-04）。"""

        campaign = self.store.load_campaign(self.campaign_id)
        revision, _candidate = current_candidate_revision(self.store, campaign)
        earlier = [
            fact
            for fact in evaluation_baseline_facts(self.store, campaign)
            if fact["formed_under"]["candidate_revision"] == revision
        ]
        reference = self.store.append_fact(
            self.campaign_id,
            "evaluation_baseline_recorded",
            {
                "baseline": number,
                "baseline_commit_sha256": self.baseline_commit(number, tag),
                "candidate_id": f"synthetic-candidate-{tag}",
                "candidate_ref": self.references[f"candidate-{tag}"],
                "supersedes": self.store.fact_ref(earlier[-1]) if earlier else None,
                "kind": kind,
            },
            self._time(),
        )
        self.references[f"evaluation-baseline-{tag}-b{number}"] = reference
        return reference

    def _current_baseline_fields(self) -> dict[str, Any]:
        baseline = current_evaluation_baseline(
            self.store, self.store.load_campaign(self.campaign_id)
        )
        if baseline is None:
            raise ValueError("当前没有评估基线（b0），不能写 v3 结果")
        return {
            "evaluation_baseline": baseline["baseline"],
            "baseline_commit_sha256": baseline["baseline_commit_sha256"],
        }

    def rerun_pair_under_baseline(self, tag: str = "strict") -> dict[str, Any]:
        """在当前评估基线下重跑 tag 候选的规则：把 b0 的 PAIR 以 v3 合同重写一份（受影响规则）。"""

        base = self.store.load_fact(self.references[f"pair-{tag}"])["payload"]
        payload = dict(base)
        payload["schema_version"] = "official-client-pair/v3"
        payload.update(self._current_baseline_fields())
        reference = self.store.append_fact(self.campaign_id, "pair_recorded", payload, self._time())
        self.references[f"pair-{tag}-b{payload['evaluation_baseline']}"] = reference
        return reference

    def baseline_acceptance_payload(
        self, pair_refs: list[dict[str, Any]], tag: str = "strict"
    ) -> dict[str, Any]:
        """当前评估基线下的 Acceptance v3 payload：除 PAIR 与基线字段外沿用 b0 的 Acceptance。"""

        base = self.store.load_fact(self.references[f"acceptance-{tag}"])["payload"]
        payload = dict(base)
        payload["schema_version"] = "official-client-acceptance/v3"
        payload["pair_refs"] = sorted(pair_refs, key=canonical_sha256)
        payload.update(self._current_baseline_fields())
        payload["identity_sha256"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "identity_sha256"}
        )
        return payload

    def accept_under_baseline(
        self, pair_refs: list[dict[str, Any]], *, tag: str = "strict", finalize: bool = True
    ) -> dict[str, Any]:
        """在当前评估基线下写 Acceptance v3，并（可选）派生该基线的 VC-5 完成事实。"""

        payload = self.baseline_acceptance_payload(pair_refs, tag)
        number = payload["evaluation_baseline"]
        reference = self.store.append_fact(
            self.campaign_id, "acceptance_recorded", payload, self._time()
        )
        self.references[f"acceptance-{tag}-b{number}"] = reference
        if finalize:
            self.references[f"validation-completed-{tag}-b{number}"] = finalize_validation(
                self.store,
                self.campaign_id,
                reference,
                self.references["validation-selector-after"],
                self._time(),
            )
        return reference

    # ---- B4-07 交付恢复段 ----

    DELIVERY_STAGE_CHECKS = {
        "candidate_active": ["health", "smoke"],
        "rollback_verified": ["auth", "data", "dependencies", "health"],
        "candidate_restored": ["health", "profile", "smoke"],
        "stable_observed": ["identity", "safety", "state"],
    }

    def seal_delivery_plan(self, *, delivery_id: str = "delivery-strict-1") -> dict[str, Any]:
        """封存严格候选的交付计划（与 ``strict_deliver`` 同值；同内容已封存时复用）。"""

        plan = {
            "schema_version": "official-client-candidate-delivery-plan/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "plan_id": "delivery-plan-strict" if delivery_id == "delivery-strict-1" else f"plan-{delivery_id}",
            "delivery_id": delivery_id,
            "candidate_id": "synthetic-candidate-strict",
            "candidate_ref": self.references["candidate-strict"],
            "candidate_build_receipt_ref": self.references["candidate-build-receipt"],
            "validation_completed_ref": self.references["validation-completed"],
            "acceptance_ref": self.references["acceptance-strict"],
            "candidate_evidence_package_ref": self.references["candidate-evidence-package"],
            "host_id": "dmit-synthetic",
            "architecture": "linux/amd64",
            "compose_sha256": "d" * 64,
            "environment_sha256": "4" * 64,
            "configuration_sha256": "5" * 64,
            "network_sha256": "6" * 64,
            "runtime_profile_sha256": SHA_A,
            "rollback_runtime_profile_sha256": "e" * 64,
            "candidate_image_digest": TARGET_IMAGE,
            "rollback_image_digest": ROLLBACK_IMAGE,
            "observation_window_seconds": 60,
            "stage_checks": self.DELIVERY_STAGE_CHECKS,
            "environment_freeze_ref": self.references["operational-evidence"],
            "rollback_material_refs": [self.references["operational-evidence"]],
            "vircs_state": {
                "ownership": "operator_managed",
                "verification": "unverified",
                "action": "not_touched",
            },
            "identity_sha256": "",
        }
        plan["identity_sha256"] = canonical_sha256(
            {key: value for key, value in plan.items() if key != "identity_sha256"}
        )
        reference = self.store.seal_object_outcome(
            "candidate_delivery_plan", plan, allow_existing=True
        )["object_ref"]
        self.references[f"delivery-plan-{delivery_id}"] = reference
        return reference

    def delivery_stage_payload(
        self,
        stage: str,
        *,
        previous: dict[str, Any] | None,
        result: str = "pass",
        recovery: tuple[int, dict[str, Any]] | None = None,
        delivery_id: str = "delivery-strict-1",
        observed_seconds: int | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """候选交付事实 v2 的 payload：``recovery`` 为 ``(k, 段预约引用)`` 时是 dr<k> 的事实，否则是首轮交付。"""

        is_rollback = stage == "rollback_verified"
        if result == "failed":
            self._delivery_failures = getattr(self, "_delivery_failures", 0) + 1
            evidence = self.seal_manifest(
                "operational_evidence",
                f"delivery-failure-{self._delivery_failures}",
                [{"id": "stage-failure", "facts": {"result": "failed"}}],
            )
        else:
            evidence = self.references["operational-evidence"]
        if observed_seconds is None:
            observed_seconds = 60 if stage == "stable_observed" and result == "pass" else 0
        payload: dict[str, Any] = {
            "schema_version": "official-client-candidate-delivery-fact/v2",
            "delivery_id": delivery_id,
            "stage": stage,
            "candidate_id": "synthetic-candidate-strict",
            "candidate_ref": self.references["candidate-strict"],
            "candidate_build_receipt_ref": self.references["candidate-build-receipt"],
            "validation_completed_ref": self.references["validation-completed"],
            "acceptance_ref": self.references["acceptance-strict"],
            "delivery_plan_ref": self.references[f"delivery-plan-{delivery_id}"],
            "previous_stage_ref": previous,
            "host_id": "dmit-synthetic",
            "architecture": "linux/amd64",
            "compose_sha256": "d" * 64,
            "environment_sha256": "4" * 64,
            "configuration_sha256": "5" * 64,
            "network_sha256": "6" * 64,
            "runtime_profile_sha256": "e" * 64 if is_rollback else SHA_A,
            "image_digest": ROLLBACK_IMAGE if is_rollback else TARGET_IMAGE,
            "check_ids": self.DELIVERY_STAGE_CHECKS[stage],
            "evidence_refs": [evidence],
            "observed_seconds": observed_seconds,
            "result": result,
            "recovery_revision": None if recovery is None else f"dr{recovery[0]}",
            "recovery_ref": None if recovery is None else recovery[1],
            "identity_sha256": "",
        }
        payload.update(overrides or {})
        payload["identity_sha256"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "identity_sha256"}
        )
        return payload

    def record_delivery_stage(self, stage: str, **kwargs: Any) -> dict[str, Any]:
        """追加一条候选交付事实 v2（参数同 ``delivery_stage_payload``）。"""

        return self.store.append_fact(
            self.campaign_id,
            "candidate_delivery_recorded",
            self.delivery_stage_payload(stage, **kwargs),
            self._time(),
        )

    def record_delivery_run(
        self,
        stages: list[str],
        *,
        previous: dict[str, Any] | None = None,
        recovery: tuple[int, dict[str, Any]] | None = None,
        fail_stage: str | None = None,
        delivery_id: str = "delivery-strict-1",
    ) -> list[dict[str, Any]]:
        """按顺序推进一段交付：首条衔接 ``previous``，其后衔接本段前一阶段；遇到 ``fail_stage`` 记失败并停止。"""

        references: list[dict[str, Any]] = []
        for stage in stages:
            failed = stage == fail_stage
            previous = self.record_delivery_stage(
                stage,
                previous=previous,
                result="failed" if failed else "pass",
                recovery=recovery,
                delivery_id=delivery_id,
            )
            references.append(previous)
            if failed:
                break
        return references

    def delivery_recovery_payload(
        self,
        number: int,
        *,
        anchor_kind: str,
        anchor_ref: dict[str, Any],
        rerun_from_stage: str,
        previous_valid_stage_ref: dict[str, Any] | None,
        delivery_id: str = "delivery-strict-1",
        environment_overrides: dict[str, Any] | None = None,
        stages_to_rerun: list[str] | None = None,
    ) -> dict[str, Any]:
        """交付恢复段 dr<number> 的预约 payload（B4-07）。"""

        stages = [
            "candidate_active",
            "rollback_verified",
            "candidate_restored",
            "stable_observed",
        ]
        environment = {
            "host_id": "dmit-synthetic",
            "architecture": "linux/amd64",
            "compose_sha256": "d" * 64,
            "environment_sha256": "4" * 64,
            "configuration_sha256": "5" * 64,
            "network_sha256": "6" * 64,
            "evidence_ref": self.references["operational-evidence"],
        }
        environment.update(environment_overrides or {})
        return {
            "campaign_id": self.campaign_id,
            "delivery_id": delivery_id,
            "delivery_plan_ref": self.references[f"delivery-plan-{delivery_id}"],
            "recovery_revision": f"dr{number}",
            "anchor_kind": anchor_kind,
            "anchor_ref": anchor_ref,
            "rerun_from_stage": rerun_from_stage,
            "previous_valid_stage_ref": previous_valid_stage_ref,
            "stages_to_rerun": stages_to_rerun
            if stages_to_rerun is not None
            else stages[stages.index(rerun_from_stage):],
            "run_nonce": f"delivery-run-{delivery_id}-dr{number}",
            "environment_before": environment,
        }

    def reserve_delivery_recovery(self, number: int, **kwargs: Any) -> dict[str, Any]:
        """封存交付恢复段 dr<number> 的预约（参数同 ``delivery_recovery_payload``）。"""

        reference = self.store.seal_object(
            "candidate_delivery_recovery", self.delivery_recovery_payload(number, **kwargs)
        )
        self.references[f"delivery-recovery-dr{number}"] = reference
        return reference

    def delivery_stage_invalidation_payload(
        self, stage: str, stage_ref: dict[str, Any], *, delivery_id: str = "delivery-strict-1"
    ) -> dict[str, Any]:
        """经批准的交付阶段失效 payload（B4-07），review_sha256 按审核内容现算。"""

        payload: dict[str, Any] = {
            "campaign_id": self.campaign_id,
            "delivery_id": delivery_id,
            "invalidated_stage": stage,
            "invalidated_stage_ref": stage_ref,
            "reason": f"{stage} 的通过结论需要重跑核实",
            "evidence_refs": [self.references["operational-evidence"]],
            "approved_by": "synthetic-reviewer",
            "approved_at_utc": self._time(),
            "review_sha256": "",
        }
        payload["review_sha256"] = invalidation_review_sha256(payload)
        return payload

    def invalidate_delivery_stage(
        self, stage: str, stage_ref: dict[str, Any], *, delivery_id: str = "delivery-strict-1"
    ) -> dict[str, Any]:
        reference = self.store.seal_object(
            "candidate_delivery_stage_invalidation",
            self.delivery_stage_invalidation_payload(stage, stage_ref, delivery_id=delivery_id),
        )
        self.references[f"delivery-stage-invalidation-{stage}"] = reference
        return reference

    def seal_delivery_package(
        self,
        fact_refs: list[dict[str, Any]],
        *,
        delivery_id: str = "delivery-strict-1",
        package_id: str = "delivery-package-recovered",
    ) -> dict[str, Any]:
        """封存绑定给定四阶段事实的交付包（私有归档清单与 ``strict_deliver`` 同值，已封存即复用）。"""

        archive = {
            "schema_version": "official-client-private-archive-manifest/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "manifest_id": "private-archive-strict",
            "redacted": True,
            "entries": [self.private_binding],
            "entry_count": 1,
            "total_bytes": self.private_binding["bytes"],
            "manifest_sha256": canonical_sha256([self.private_binding]),
            "identity_sha256": "",
        }
        archive["identity_sha256"] = canonical_sha256(
            {key: value for key, value in archive.items() if key != "identity_sha256"}
        )
        archive_ref = self.store.seal_object_outcome(
            "private_archive_manifest", archive, allow_existing=True
        )["object_ref"]
        package = {
            "schema_version": "official-client-candidate-delivery-package/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "package_id": package_id,
            "delivery_id": delivery_id,
            "candidate_id": "synthetic-candidate-strict",
            "candidate_ref": self.references["candidate-strict"],
            "candidate_build_receipt_ref": self.references["candidate-build-receipt"],
            "validation_completed_ref": self.references["validation-completed"],
            "acceptance_ref": self.references["acceptance-strict"],
            "candidate_evidence_package_ref": self.references["candidate-evidence-package"],
            "delivery_plan_ref": self.references[f"delivery-plan-{delivery_id}"],
            "delivery_fact_refs": fact_refs,
            "deployment_configuration_ref": self.references["operational-evidence"],
            "public_evidence_index_refs": [self.references["operational-evidence"]],
            "rollback_material_refs": [self.references["operational-evidence"]],
            "private_archive_manifest_ref": archive_ref,
            "candidate_image_digest": TARGET_IMAGE,
            "artifact_count": 3,
            "identity_sha256": "",
        }
        package["identity_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "identity_sha256"}
        )
        reference = self.store.seal_object("candidate_delivery_package", package)
        self.references[package_id] = reference
        return reference
