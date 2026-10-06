"""FW-D 对象图、Campaign、Inventory、Envelope 与状态转换门禁。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from .contracts import (
    BASELINE_SCOPED_FACT_KINDS,
    CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS,
    CANDIDATE_DELIVERY_IDENTITY_FIELDS,
    CANDIDATE_DELIVERY_STAGES,
    CANDIDATE_SCOPED_FACT_KINDS,
    DEPLOYMENT_STAGES,
    EVIDENCE_PACKAGE_V3_SCHEMA,
    FACT_DIMENSION,
    SCENARIO_FACT_STAGE,
    campaign_is_v2,
    capability_key,
    delivery_recovery_number,
    fact_is_v2,
    iter_fact_refs,
    iter_object_refs,
    payload_evaluation_baseline,
    persona_key,
    require_importable_object,
    validate_fact_document,
    validate_object_document,
)
from .errors import ControlError

if TYPE_CHECKING:
    from .store import ControlStore


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class WorkflowGates:
    """只根据不可变对象和追加事实作出准入判断。"""

    def __init__(self, store: "ControlStore") -> None:
        self.store = store

    @staticmethod
    def dimension_for(fact_kind: str) -> str:
        try:
            return FACT_DIMENSION[fact_kind]
        except KeyError as error:
            raise ControlError(f"未登记的 fact_kind：{fact_kind}") from error

    def _object(self, reference: dict[str, Any], expected_kind: str | None = None) -> dict[str, Any]:
        document = self.store.load_object(reference)
        if expected_kind is not None and document["object_kind"] != expected_kind:
            raise ControlError(
                f"对象引用类型错误：期望 {expected_kind}，实际 {document['object_kind']}"
            )
        return document["payload"]

    def _fact(self, reference: dict[str, Any], expected_kind: str | None = None) -> dict[str, Any]:
        fact = self.store.load_fact(reference)
        if expected_kind is not None and fact["fact_kind"] != expected_kind:
            raise ControlError(
                f"事实引用类型错误：期望 {expected_kind}，实际 {fact['fact_kind']}"
            )
        return fact

    @staticmethod
    def _same_persona(left: Any, right: Any, label: str) -> None:
        if persona_key(left) != persona_key(right):
            raise ControlError(f"{label} 的 Persona 不一致")

    def validate_new_object(self, document: dict[str, Any]) -> None:
        """封存新对象前的对象图门禁；与重放不同，这里还执行写入时有效性（当前受管代码、当前链尾）。"""

        validate_object_document(document)
        self._validate_object_payload_graph(
            document["object_kind"], document["payload"], new=True
        )

    def validate_object_graph(self, reference: dict[str, Any]) -> None:
        """已封存对象的对象图复核（重放与引用校验）；只证明形成时一致，与当前受管代码无关。"""

        document = self.store.load_object(reference)
        self._validate_object_payload_graph(document["object_kind"], document["payload"])

    def validate_imported_object(self, document: dict[str, Any]) -> None:
        """前序导入对象（B1-14）复制进本 Store 之前的复核：按重放口径核对象图，不跑写入时门禁。

        对象在前序 Store 形成时已过写入时门禁；这里只证明它脱离前序 Store 也能在本 Store 独立重放：类型可导入、
        不引用任何事实或收据（前序 Campaign 的事实、收据不会进入本 Store），嵌套对象已在本 Store（调用方按依赖
        顺序复制），对象图门禁通过。
        """

        validate_object_document(document)
        require_importable_object(document)
        self._validate_object_payload_graph(document["object_kind"], document["payload"])

    def _require_campaign_owned(
        self,
        campaign: dict[str, Any],
        reference: dict[str, Any],
        owner_campaign_id: str,
        label: str,
    ) -> None:
        """消费端核验（B1-14）：被引用者属于本 Campaign，或经 read_only_reuse 导入收据授权（见
        ``receipts.cross_campaign_denial`` 的四项）。v1 Campaign 原样抛出 ``label``，原文案与判定顺序逐字不变。
        """

        if owner_campaign_id == campaign["campaign_id"]:
            return
        from .receipts import authorize_cross_campaign_reference

        authorize_cross_campaign_reference(
            self.store, campaign, reference, owner_campaign_id=owner_campaign_id, label=label
        )

    def _precheck_fact_ref(
        self, campaign: dict[str, Any], reference: dict[str, Any], label: str
    ) -> None:
        """v2：跨 Campaign 的事实引用在解引用之前核验——前序 Campaign 的事实不在本 Store，解引用只会报
        「Campaign 不存在」而掩盖真正的拒绝理由。v1 不预检，保持原来「先解引用、后比对」的顺序与文案。
        """

        if campaign_is_v2(campaign) and reference["campaign_id"] != campaign["campaign_id"]:
            self._require_campaign_owned(campaign, reference, reference["campaign_id"], label)

    def _validate_object_payload_graph(
        self, kind: str, payload: dict[str, Any], *, new: bool = False
    ) -> None:
        if kind == "snapshot":
            schema = self._object(payload["profile_schema_ref"], "profile_schema")
            self._same_persona(payload["persona"], schema["persona"], "Snapshot/ProfileSchema")
            if payload["version"] != schema["version"]:
                raise ControlError("Snapshot 与 ProfileSchema 版本不一致")
        elif kind == "release_bundle":
            snapshot = self._object(payload["snapshot_ref"], "snapshot")
            self._require_release_coordinate(payload, snapshot, "ReleaseBundle/Snapshot")
        elif kind == "release_artifact":
            snapshot = self._object(payload["snapshot_ref"], "snapshot")
            bundle = self._object(payload["release_bundle_ref"], "release_bundle")
            self._require_release_coordinate(payload, snapshot, "ReleaseArtifact/Snapshot")
            self._require_release_coordinate(payload, bundle, "ReleaseArtifact/ReleaseBundle")
            if bundle["snapshot_ref"] != payload["snapshot_ref"]:
                raise ControlError("ReleaseArtifact 的 ReleaseBundle 未绑定同一 Snapshot")
        elif kind == "production_ingress_inventory":
            self._validate_ingress_closure(payload)
        elif kind == "egress_disposition_inventory":
            self._validate_egress_closure(payload)
        elif kind == "support_envelope":
            inventory = self._object(
                payload["production_ingress_inventory_ref"],
                "production_ingress_inventory",
            )
            self._same_persona(payload["persona"], inventory["persona"], "SupportEnvelope/Inventory")
            known_ingresses = {item["logical_ingress_id"] for item in inventory["entries"]}
            used_ingresses = {item["logical_ingress_id"] for item in payload["capabilities"]}
            unknown = sorted(used_ingresses - known_ingresses)
            if unknown:
                raise ControlError(f"SupportEnvelope 引用了未知生产入口：{unknown}")
        elif kind == "active_support_envelope":
            self._validate_active_support_envelope(payload)
        elif kind == "rollback_operational_envelope":
            if payload["rollback_release_ref"] is not None:
                release = self._object(payload["rollback_release_ref"], "release_artifact")
                self._same_persona(
                    payload["persona"], release["persona"], "RollbackEnvelope/Release"
                )
            self._object(
                payload["operational_bindings"]["selector_snapshot_ref"],
                "runtime_catalog_snapshot",
            )
        elif kind == "deployment_traffic_envelope":
            self._validate_deployment_traffic_envelope(payload)
        elif kind == "rule_migration_ledger":
            self._validate_rule_migration_ledger(payload)
        elif kind == "atomic_assertion_ledger":
            self._validate_atomic_assertion_ledger(payload)
        elif kind == "validation_execution_plan":
            self._validate_validation_execution_plan(payload, new=new)
        elif kind == "candidate_evidence_package":
            self._validate_candidate_evidence_package(payload)
        elif kind == "candidate_delivery_plan":
            self._validate_candidate_delivery_plan(payload)
        elif kind == "candidate_delivery_package":
            self._validate_candidate_delivery_package(payload, new=new)
        elif kind == "private_archive_manifest":
            campaign = self.store.load_campaign(payload["campaign_id"])
            self._same_persona(
                campaign["persona"], payload["persona"], "Campaign/PrivateArchiveManifest"
            )
        elif kind == "tool_evolution_registration":
            self._validate_tool_evolution_registration(payload, new=new)
        elif kind == "predecessor_import":
            self._validate_predecessor_import(payload, new=new)
        elif kind == "candidate_invalidation":
            self._validate_candidate_invalidation(payload, new=new)
        elif kind == "candidate_delivery_stage_invalidation":
            self._validate_candidate_delivery_stage_invalidation(payload, new=new)
        elif kind == "candidate_delivery_recovery":
            self._validate_candidate_delivery_recovery(payload, new=new)

    def validate_predecessor_import_target(
        self, payload: dict[str, Any], *, new: bool
    ) -> dict[str, Any]:
        """前序证据导入登记中与导入对象无关的部分（登记在复制对象之前先调用，尽早失败），返回目标 Campaign。

        重放与写入都核：目标 Campaign 是 v2 且 client 一致；批准时间不早于 Campaign 建立。写入时再核：当前受管代码
        匹配有效工具身份；``predecessor.store_root`` 不是本 Store、不与本 Store 互相嵌套；前序 Campaign ID 不与本 Store
        已有 Campaign 重名；列入的 EvidencePackage 没有被本 Campaign 的事实当本地证据引用过
        （``_require_unclaimed_evidence``）。重放不解引用 ``store_root``——它是形成期出处，前序 Store 之后可能已经搬走。
        """

        campaign = self.store.load_campaign(payload["campaign_id"])
        if not campaign_is_v2(campaign):
            raise ControlError("前序证据导入只属于 Campaign v2：v1 Campaign 不接受跨 Campaign 证据")
        if payload["client"] != campaign["client"]:
            raise ControlError("前序证据导入的 client 与 Campaign 不一致")
        if _parse_utc(payload["approved_at_utc"]) < _parse_utc(campaign["created_at_utc"]):
            raise ControlError("前序证据导入的批准时间早于 Campaign 建立时间")
        if not new:
            return campaign
        from .receipts import _require_effective_tool, require_predecessor_root_outside

        _require_effective_tool(self.store, campaign)
        require_predecessor_root_outside(self.store, Path(payload["predecessor"]["store_root"]))
        local = self.store.root / "campaigns" / f"{payload['predecessor']['campaign_id']}.json"
        if local.exists() or local.is_symlink():
            raise ControlError(
                "前序 Campaign ID 与本 Store 已有 Campaign 重名：跨 Store 引用会与本地 Campaign 混淆"
            )
        self._require_unclaimed_evidence(campaign, payload)
        return campaign

    def _require_unclaimed_evidence(
        self, campaign: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """写入时：新登记列出的 EvidencePackage 若已被本 Campaign 的事实直接引用，必须已经获得导入授权。

        包一旦被导入登记列出，本 Campaign 对它的引用（含既有事实的重放）就改按消费端核验判定；已被事实当本地证据
        引用、又没有 read_only_reuse 授权的包若再被登记，那些事实的重放会失败。这里在登记时拒绝，保证登记不会让
        既有事实失效。与 EvidenceFact 写入共用同一把维度锁（见 ``ControlStore.seal_object_outcome``），两者不会交错。
        """

        from .receipts import cross_campaign_denial

        packages = [
            item for item in payload["imported_objects"] if item["object_kind"] == "evidence_package"
        ]
        if not packages:
            return
        referenced = {
            (item["object_kind"], item["sha256"])
            for fact in self.store.list_facts(campaign["campaign_id"])
            for item in iter_object_refs(fact["payload"])
        }
        claimed = [
            item["sha256"]
            for item in packages
            if (item["object_kind"], item["sha256"]) in referenced
            and cross_campaign_denial(self.store, campaign, item) is not None
        ]
        if claimed:
            raise ControlError(
                "导入清单里的 EvidencePackage 已被本 Campaign 的事实当本地证据引用，登记为导入会让这些事实的重放"
                f"改按导入授权判定：{'、'.join(claimed)}"
            )

    def _validate_predecessor_import(self, payload: dict[str, Any], *, new: bool) -> None:
        """前序证据导入登记的对象图（设计 B1-14）：``validate_predecessor_import_target`` 的全部判据，加上导入对象
        都已在本 Store、类型可导入、只引用对象、自带所属 Campaign 时属于前序 Campaign，导入清单对对象引用闭合。"""

        self.validate_predecessor_import_target(payload, new=new)
        listed = {(item["object_kind"], item["sha256"]) for item in payload["imported_objects"]}
        for reference in payload["imported_objects"]:
            document = self.store.load_object(reference)
            require_importable_object(
                document, predecessor_campaign_id=payload["predecessor"]["campaign_id"]
            )
            missing = sorted(
                {
                    (item["object_kind"], item["sha256"])
                    for item in iter_object_refs(document["payload"])
                }
                - listed
            )
            if missing:
                raise ControlError(
                    "predecessor_import.imported_objects 未对对象引用闭合："
                    f"{reference['object_kind']}/{reference['sha256']} 引用了未列入的 "
                    + "、".join(f"{kind}/{digest}" for kind, digest in missing)
                )

    def _validate_tool_evolution_registration(
        self, payload: dict[str, Any], *, new: bool
    ) -> None:
        """工具演进登记的对象图（设计 B1-12「有效工具身份」）。

        重放与写入都核：所属 Campaign 是 v2 且 client 一致；index 1 的 ``from_identity`` 等于 Campaign 冻结身份，
        其后等于 ``previous_ref`` 那份登记的 ``to_identity``；批准时间不早于 Campaign 建立与上一份登记。写入时再核：
        新登记必须接在当前链尾（不得分叉），``to_identity`` 的七项必须全部等于当前受管代码的身份——可现算四项
        现算清单，wire／evidence／control 三层摘要用 v3 引擎 ``compute_identity`` 现算（B1-13 补齐）。
        """

        from .receipts import current_tool_identity, identity_drift, tool_evolutions

        campaign = self.store.load_campaign(payload["campaign_id"])
        if not campaign_is_v2(campaign):
            raise ControlError("v1 Campaign 不支持工具演进登记：控制面工具变化必须建立新的 Campaign")
        if payload["client"] != campaign["client"]:
            raise ControlError("工具演进登记的 client 与 Campaign 不一致")
        approved_at = _parse_utc(payload["approved_at_utc"])
        if approved_at < _parse_utc(campaign["created_at_utc"]):
            raise ControlError("工具演进登记的批准时间早于 Campaign 建立时间")
        if payload["index"] == 1:
            expected_from = campaign["tool_identity"]
        else:
            previous = self._object(payload["previous_ref"], "tool_evolution_registration")
            if (
                previous["campaign_id"] != payload["campaign_id"]
                or previous["index"] != payload["index"] - 1
            ):
                raise ControlError("工具演进登记的 previous_ref 不是同一 Campaign 的上一份登记")
            if approved_at < _parse_utc(previous["approved_at_utc"]):
                raise ControlError("工具演进登记的批准时间早于上一份登记")
            expected_from = previous["to_identity"]
        if payload["from_identity"] != expected_from:
            raise ControlError("工具演进登记的 from_identity 未承接上一份有效工具身份")
        if not new:
            return
        chain = tool_evolutions(self.store, campaign)
        tail = chain[-1][0] if chain else None
        if payload["index"] != len(chain) + 1 or payload["previous_ref"] != tail:
            raise ControlError("工具演进登记必须接在当前链尾：index 或 previous_ref 不是当前有效身份")
        drift = identity_drift(payload["to_identity"], current_tool_identity(payload["client"]))
        if drift:
            raise ControlError(
                f"工具演进登记的 to_identity 不是当前受管代码的身份：{'、'.join(drift)}"
            )

    def _validate_candidate_invalidation(self, payload: dict[str, Any], *, new: bool) -> None:
        """候选作废对象的对象图（设计 B3-05）。

        重放与写入都核：所属 Campaign 是 v2；批准时间不早于 Campaign 建立；被作废的 revision 可达（显式 revision
        由 ``revision_ref`` 指向的 revision 事实给出、候选一致；没有显式 r1 事实时才允许 ``revision_ref`` 为 null）；
        同一 revision 只作废一次；候选没有 VC-5 完成事实（已验收的候选不得作废——作废之后的写入一律被拒，所以重放
        时出现即说明作废发生在验收之后）。写入时再核：当前受管代码匹配有效工具身份；被作废的是当前 revision、
        候选属于它（隐含 r1 不限定候选）。
        """

        from .receipts import (
            _require_effective_tool,
            candidate_invalidations,
            candidate_revision_facts,
            current_candidate_revision,
        )

        campaign = self.store.load_campaign(payload["campaign_id"])
        if not campaign_is_v2(campaign):
            raise ControlError("v1 Campaign 不支持候选作废：v1 没有候选 revision")
        if _parse_utc(payload["approved_at_utc"]) < _parse_utc(campaign["created_at_utc"]):
            raise ControlError("候选作废的批准时间早于 Campaign 建立时间")
        revision = payload["revision"]
        reference = payload["revision_ref"]
        if reference is None:
            if any(
                fact["payload"]["revision"] == 1
                for fact in candidate_revision_facts(self.store, campaign)
            ):
                raise ControlError("r1 已有显式 revision 事实：作废 r1 必须以 revision_ref 引用它")
        else:
            fact = self._fact(reference, "candidate_revision_recorded")
            if (
                fact["payload"]["revision"] != revision
                or fact["payload"]["candidate_id"] != payload["candidate_id"]
            ):
                raise ControlError("候选作废与所引 revision 事实的 revision／候选不一致")
        others = [
            entry
            for entry in candidate_invalidations(self.store, campaign)
            if entry[1]["revision"] == revision and entry[1] != payload
        ]
        if others:
            raise ControlError(
                f"候选 revision r{revision} 只能作废一次：已有作废对象 {others[0][0]['sha256']}"
            )
        if any(
            fact["fact_kind"] == "validation_completed"
            and fact["payload"]["candidate_id"] == payload["candidate_id"]
            for fact in self.store.list_facts(campaign["campaign_id"], "validation")
        ):
            raise ControlError(
                f"候选 {payload['candidate_id']} 已完成 VC-5 验收（validation_completed），不得作废"
            )
        if not new:
            return
        _require_effective_tool(self.store, campaign)
        current_revision, current_candidate = current_candidate_revision(self.store, campaign)
        if revision != current_revision:
            raise ControlError(
                f"只能作废当前候选 revision r{current_revision}：r{revision} 已不是当前 revision"
            )
        if current_candidate is not None and payload["candidate_id"] != current_candidate:
            raise ControlError(
                f"候选 {payload['candidate_id']} 不是当前 revision r{revision} 的候选（当前 {current_candidate}）"
            )

    def _validate_candidate_delivery_stage_invalidation(
        self, payload: dict[str, Any], *, new: bool
    ) -> None:
        """经批准的交付阶段失效的对象图（设计 B4-07 门禁 ⑧）。

        重放与写入都核：所属 Campaign 是 v2；批准时间不早于 Campaign 建立与被失效的事实；``invalidated_stage_ref``
        确为同一 delivery、同一阶段的 pass。写入时再核：当前受管代码匹配有效工具身份；delivery 尚无交付收据（已
        finalize 一律拒绝）；没有待消费的阶段失效；最新交付段已收口；被失效的 pass 在当前有效链上。是否被恢复段
        消费、消费时是否仍在有效链上，由重放时的交付链分析（``analyze_delivery_chain``）核对。
        """

        from .receipts import _require_effective_tool, delivery_chain_state, delivery_receipts

        campaign = self.store.load_campaign(payload["campaign_id"])
        if not campaign_is_v2(campaign):
            raise ControlError("v1 Campaign 不支持交付阶段失效")
        approved_at = _parse_utc(payload["approved_at_utc"])
        if approved_at < _parse_utc(campaign["created_at_utc"]):
            raise ControlError("交付阶段失效的批准时间早于 Campaign 建立时间")
        fact = self._fact(payload["invalidated_stage_ref"], "candidate_delivery_recorded")
        stage_payload = fact["payload"]
        if (
            stage_payload["delivery_id"] != payload["delivery_id"]
            or stage_payload["stage"] != payload["invalidated_stage"]
        ):
            raise ControlError("⑧ 阶段失效指向的事实不是同一 delivery 的同一阶段")
        if stage_payload["result"] != "pass":
            raise ControlError("⑧ 只能失效已通过的阶段：失败阶段以 failure 锚点续跑")
        if approved_at < _parse_utc(fact["issued_at_utc"]):
            raise ControlError("交付阶段失效的批准时间早于被失效的阶段事实")
        if not new:
            return
        _require_effective_tool(self.store, campaign)
        finalized = delivery_receipts(
            self.store, campaign["campaign_id"], stage_payload["delivery_plan_ref"]
        )
        if finalized:
            raise ControlError(
                f"③ 候选交付 {payload['delivery_id']} 已 finalize（终态收据 {finalized[0][0]['sha256']}）："
                "已 finalize 的 delivery 不得申请阶段失效"
            )
        state = delivery_chain_state(self.store, campaign["campaign_id"], payload["delivery_id"])
        if state.pending is not None:
            raise ControlError("同一 delivery 已有待消费的阶段失效：先预约恢复段消费它")
        if not state.latest_closed:
            raise ControlError(
                "阶段失效只能在最新交付段收口（失败或到达 stable_observed）后批准："
                f"{'首轮交付' if state.latest == 0 else f'dr{state.latest}'}仍在推进"
            )
        current = state.valid.get(payload["invalidated_stage"])
        if current is None or self.store.fact_ref(current) != payload["invalidated_stage_ref"]:
            raise ControlError("被失效的阶段事实不在当前有效链上（已被恢复段取代或从未生效）")

    def _validate_candidate_delivery_recovery(
        self, payload: dict[str, Any], *, new: bool
    ) -> None:
        """交付恢复段 ``dr<k>`` 预约的对象图（设计 B4-07）。

        重放与写入都核：所属 Campaign 是 v2；交付计划属于同一 Campaign、同一 delivery；``environment_before`` 的
        环境身份 6 项与冻结计划逐项相等、证据全部通过（门禁 ⑤，环境变化须开新的 delivery_id）；failure 锚点确为
        同一 delivery 在 ``rerun_from_stage`` 的失败事实，approved_invalidation 锚点确为同一 delivery 对该阶段的已批准
        失效（门禁 ⑧）；``previous_valid_stage_ref`` 确为同一 delivery 前一阶段的 pass。写入时再核：当前受管代码
        匹配有效工具身份；delivery 未 finalize；编号接续、run_nonce 不复用；最新交付段已收口（门禁 ⑦）；failure
        锚点是最新一段收口的失败事实且没有待消费的阶段失效，approved_invalidation 锚点就是待消费的那一份；
        ``previous_valid_stage_ref`` 是 ``rerun_from_stage`` 之前最近一份仍有效的 pass（门禁 ②）。
        """

        from .receipts import (
            _require_effective_tool,
            analyze_delivery_chain,
            delivery_facts,
            delivery_receipts,
            delivery_recoveries,
            delivery_stage_invalidations,
        )

        campaign = self.store.load_campaign(payload["campaign_id"])
        if not campaign_is_v2(campaign):
            raise ControlError("v1 Campaign 不支持交付恢复段")
        campaign_id = campaign["campaign_id"]
        delivery_id = payload["delivery_id"]
        plan = self._object(payload["delivery_plan_ref"], "candidate_delivery_plan")
        if plan["campaign_id"] != campaign_id or plan["delivery_id"] != delivery_id:
            raise ControlError("交付恢复段预约的交付计划不是同一 Campaign 的同一 delivery")
        environment = payload["environment_before"]
        for key in CANDIDATE_DELIVERY_ENVIRONMENT_FIELDS:
            if environment[key] != plan[key]:
                raise ControlError(
                    f"⑤ 交付恢复段的环境身份 {key} 与冻结计划不一致：环境变化不能在同一 delivery 内恢复，"
                    "须开新的 delivery_id"
                )
        self._require_manifest_pass(
            self._object(environment["evidence_ref"], "operational_evidence"),
            "交付恢复段/environment_before",
        )
        stage = payload["rerun_from_stage"]
        index = CANDIDATE_DELIVERY_STAGES.index(stage)
        if payload["anchor_kind"] == "failure":
            anchor = self._fact(payload["anchor_ref"], "candidate_delivery_recorded")["payload"]
            if (
                anchor["delivery_id"] != delivery_id
                or anchor["delivery_plan_ref"] != payload["delivery_plan_ref"]
                or anchor["result"] != "failed"
                or anchor["stage"] != stage
            ):
                raise ControlError(
                    "failure 锚点必须是同一 delivery 在 rerun_from_stage 的失败阶段事实：已通过的阶段要重跑须先批准阶段失效"
                )
        else:
            invalidation = self._object(
                payload["anchor_ref"], "candidate_delivery_stage_invalidation"
            )
            if (
                invalidation["campaign_id"] != campaign_id
                or invalidation["delivery_id"] != delivery_id
                or invalidation["invalidated_stage"] != stage
            ):
                raise ControlError(
                    "⑧ approved_invalidation 锚点必须是同一 delivery 对 rerun_from_stage 的已批准阶段失效"
                )
        previous = payload["previous_valid_stage_ref"]
        if previous is not None:
            previous_payload = self._fact(previous, "candidate_delivery_recorded")["payload"]
            if (
                previous_payload["delivery_id"] != delivery_id
                or previous_payload["delivery_plan_ref"] != payload["delivery_plan_ref"]
                or previous_payload["result"] != "pass"
                or previous_payload["stage"] != CANDIDATE_DELIVERY_STAGES[index - 1]
            ):
                raise ControlError(
                    "② previous_valid_stage_ref 必须是同一 delivery 在 rerun_from_stage 前一阶段的 pass"
                )
        if not new:
            return
        _require_effective_tool(self.store, campaign)
        finalized = delivery_receipts(self.store, campaign_id, payload["delivery_plan_ref"])
        if finalized:
            raise ControlError(
                f"候选交付 {delivery_id} 已 finalize（终态收据 {finalized[0][0]['sha256']}）：不得预约恢复段"
            )
        recoveries = delivery_recoveries(self.store, campaign_id, delivery_id)
        expected_number = len(recoveries) + 1
        if delivery_recovery_number(payload["recovery_revision"]) != expected_number:
            raise ControlError(f"恢复段编号必须接续：下一段是 dr{expected_number}")
        if payload["run_nonce"] in {item["run_nonce"] for _reference, item in recoveries.values()}:
            raise ControlError("交付恢复段的 run_nonce 不得复用")
        state = analyze_delivery_chain(
            self.store,
            delivery_id,
            delivery_facts(self.store, campaign_id, delivery_id),
            recoveries,
            delivery_stage_invalidations(self.store, campaign_id, delivery_id),
        )
        if not state.latest_closed:
            raise ControlError(
                f"⑦ {'首轮交付' if state.latest == 0 else f'dr{state.latest}'}尚未收口（既没有失败事实，也没有到达"
                " stable_observed）：同一 delivery 同一时刻只允许一个未收口的恢复段"
            )
        if payload["anchor_kind"] == "failure":
            if state.pending is not None:
                raise ControlError(
                    "④ 存在已批准但未消费的阶段失效：下一恢复段必须以 approved_invalidation 锚定它"
                )
            if (
                state.terminal is None
                or state.terminal["payload"]["result"] != "failed"
                or payload["anchor_ref"] != self.store.fact_ref(state.terminal)
            ):
                raise ControlError("failure 锚点必须是最新交付段收口时的失败事实")
        elif state.pending is None or payload["anchor_ref"] != state.pending[0]:
            raise ControlError("⑧ approved_invalidation 锚点必须是本 delivery 当前待消费的已批准阶段失效")
        if index and CANDIDATE_DELIVERY_STAGES[index - 1] not in state.valid:
            raise ControlError(f"rerun_from_stage {stage} 之前的阶段在有效链上没有 pass")
        expected_previous = (
            self.store.fact_ref(state.valid[CANDIDATE_DELIVERY_STAGES[index - 1]]) if index else None
        )
        if previous != expected_previous:
            raise ControlError(
                "② previous_valid_stage_ref 必须是 rerun_from_stage 之前最近一份仍有效的 pass"
            )

    def _validate_rule_migration_ledger(self, payload: dict[str, Any]) -> None:
        package = self._object(payload["evidence_package_ref"], "evidence_package")
        if package["schema_version"] != EVIDENCE_PACKAGE_V3_SCHEMA:
            raise ControlError("RuleMigrationLedger 只能绑定不含迁移结论的 EvidencePackage v3")
        self._same_persona(
            payload["persona"], package["persona"], "RuleMigrationLedger/EvidencePackage"
        )
        if payload["target_version"] != package["version"]:
            raise ControlError("RuleMigrationLedger 与 EvidencePackage 目标版本不一致")
        evidence_ids = {item["evidence_id"] for item in package["evidence_items"]}
        unknown = sorted(
            {
                evidence_id
                for rule in payload["rules"]
                for evidence_id in rule["evidence_item_ids"]
            }
            - evidence_ids
        )
        if unknown:
            raise ControlError(f"RuleMigrationLedger 引用了未知证据项：{unknown}")

    def _validate_atomic_assertion_ledger(self, payload: dict[str, Any]) -> None:
        ledger = self._object(
            payload["rule_migration_ledger_ref"], "rule_migration_ledger"
        )
        for key in ("campaign_id", "target_version"):
            if payload[key] != ledger[key]:
                raise ControlError(f"AtomicAssertionLedger 与 RuleMigrationLedger 的 {key} 不一致")
        self._same_persona(
            payload["persona"], ledger["persona"], "AtomicAssertionLedger/RuleMigrationLedger"
        )
        package = self._object(ledger["evidence_package_ref"], "evidence_package")
        evidence_ids = {item["evidence_id"] for item in package["evidence_items"]}
        unknown_evidence = sorted(
            {
                evidence_id
                for assertion in payload["assertions"]
                for evidence_id in assertion["evidence_item_ids"]
            }
            - evidence_ids
        )
        if unknown_evidence:
            raise ControlError(
                f"AtomicAssertionLedger 引用了未知证据项：{unknown_evidence}"
            )
        required_owners = {
            assertion["owner_id"]
            for assertion in payload["assertions"]
            if assertion["owner_kind"] == "required_rule"
        }
        target_specs = set(ledger["target_spec_ids"])
        if required_owners != target_specs:
            missing = sorted(target_specs - required_owners)
            extra = sorted(required_owners - target_specs)
            raise ControlError(
                "AtomicAssertionLedger 未唯一闭合目标 RequiredRules："
                f"missing={missing}, extra={extra}"
            )

    @staticmethod
    def _require_manifest_pass(payload: dict[str, Any], label: str) -> None:
        """要求通用操作证据的每个登记项都显式通过。"""

        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ControlError(f"{label} 缺少可复算证据项")
        failed = sorted(
            entry.get("id", "unknown")
            for entry in entries
            if not isinstance(entry, dict)
            or not isinstance(entry.get("facts"), dict)
            or entry["facts"].get("result") != "pass"
        )
        if failed:
            raise ControlError(f"{label} 存在未通过证据项：{failed}")

    @staticmethod
    def _manifest_has_failure(payload: dict[str, Any]) -> bool:
        """判断通用操作证据是否至少记录一项显式失败。"""

        entries = payload.get("entries")
        return isinstance(entries, list) and any(
            isinstance(entry, dict)
            and isinstance(entry.get("facts"), dict)
            and entry["facts"].get("result") == "failed"
            for entry in entries
        )

    def _candidate_build_matches(
        self,
        campaign: dict[str, Any],
        candidate_payload: dict[str, Any],
        build_receipt: dict[str, Any],
    ) -> None:
        expected = {
            "campaign_id": campaign["campaign_id"],
            "candidate_id": candidate_payload["candidate_id"],
            "candidate_purpose": candidate_payload["candidate_purpose"],
            "target_version": campaign["target_version"],
            "profile_approval_ref": candidate_payload["profile_approval_ref"],
            "release_artifact_ref": candidate_payload["release_artifact_ref"],
            "support_envelope_ref": candidate_payload["support_envelope_ref"],
        }
        for key, expected_value in expected.items():
            if build_receipt[key] != expected_value:
                raise ControlError(f"CandidateBuildReceipt 与 candidate_frozen 的 {key} 不一致")
        source = build_receipt["source_identity"]
        build = build_receipt["build_identity"]
        image = build_receipt["image"]
        for key in ("source_tree_sha256", "test_tree_sha256", "dependency_lock_sha256"):
            if source[key] != candidate_payload[key]:
                raise ControlError(f"CandidateBuildReceipt 与 candidate_frozen 的 {key} 不一致")
        for receipt_key, candidate_key in (
            ("build_id", "build_id"),
            ("target_architecture", "target_architecture"),
        ):
            if build[receipt_key] != candidate_payload[candidate_key]:
                raise ControlError(
                    f"CandidateBuildReceipt 与 candidate_frozen 的 {candidate_key} 不一致"
                )
        if image["image_digest"] != candidate_payload["image_digest"]:
            raise ControlError("CandidateBuildReceipt 与 candidate_frozen 的镜像摘要不一致")

    def _require_validation_gate_closure(
        self,
        *,
        campaign_id: str,
        candidate_id: str,
        candidate_ref: dict[str, Any],
        candidate_build_receipt_ref: dict[str, Any],
        validation_attempt_ref: dict[str, Any],
        validation_execution_plan_ref: dict[str, Any],
        supplied_refs: list[dict[str, Any]],
        plan: dict[str, Any],
    ) -> None:
        """要求 attempt 的外部门禁收据完整、唯一、通过且可重放。"""

        from .receipts import replay_receipt

        matching: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for reference in self.store.list_receipt_refs("validation_gate"):
            receipt = self.store.load_receipt(reference)
            if receipt["validation_attempt_ref"] == validation_attempt_ref:
                matching.append((reference, receipt))

        supplied_keys = {
            (reference["receipt_kind"], reference["sha256"])
            for reference in supplied_refs
        }
        matching_keys = {
            (reference["receipt_kind"], reference["sha256"])
            for reference, _receipt in matching
        }
        if supplied_keys != matching_keys or len(supplied_refs) != len(matching):
            raise ControlError(
                "Acceptance 外部门禁收据不是该 ValidationAttempt 的完整只写集合"
            )

        planned_by_id = {item["gate_id"]: item for item in plan["external_gates"]}
        actual_gate_ids = [receipt["gate_id"] for _reference, receipt in matching]
        if set(actual_gate_ids) != set(planned_by_id) or len(actual_gate_ids) != len(
            planned_by_id
        ):
            raise ControlError("外部门禁收据未唯一覆盖 ValidationExecutionPlan")

        expected_identity = {
            "campaign_id": campaign_id,
            "candidate_id": candidate_id,
            "candidate_ref": candidate_ref,
            "candidate_build_receipt_ref": candidate_build_receipt_ref,
            "validation_attempt_ref": validation_attempt_ref,
            "validation_execution_plan_ref": validation_execution_plan_ref,
        }
        for reference, receipt in matching:
            for key, expected in expected_identity.items():
                if receipt[key] != expected:
                    raise ControlError(f"外部门禁收据的 {key} 与当前验收链不一致")
            planned = planned_by_id[receipt["gate_id"]]
            for key in (
                "requirement_sha256",
                "source",
                "command",
                "working_directory",
                "host_id",
                "architecture",
                "reused_receipt_ref",
                "previous_failed_receipt_ref",
            ):
                if receipt[key] != planned[key]:
                    raise ControlError(
                        f"外部门禁收据 {receipt['gate_id']} 的 {key} 与冻结计划不一致"
                    )
            if receipt["result"] != "pass":
                raise ControlError("失败的外部门禁收据不得进入 Acceptance")
            replay_receipt(self.store, reference)

    def _require_candidate_delivery_receipt(
        self,
        candidate_ref: dict[str, Any],
        acceptance_ref: dict[str, Any],
    ) -> None:
        """严格 Candidate 进入生产晋升前必须先完成候选交付。"""

        from .receipts import replay_receipt

        matches: list[dict[str, Any]] = []
        for reference in self.store.list_receipt_refs("candidate_delivery"):
            receipt = self.store.load_receipt(reference)
            if (
                receipt["candidate_ref"] == candidate_ref
                and receipt["acceptance_ref"] == acceptance_ref
            ):
                matches.append(reference)
        if len(matches) != 1:
            raise ControlError(
                "严格 Candidate 必须先形成唯一且可重放的候选交付收据"
            )
        replay_receipt(self.store, matches[0])

    def _require_plan_tool_identity(
        self, campaign: dict[str, Any], payload: dict[str, Any], *, new: bool
    ) -> None:
        """v2 执行计划的运行工具摘要（gates 原 L395 的 v2 轨）。

        计划是对象，没有自己的 ``formed_under``：封存时必须等于当前有效身份的 ``tool_files_sha256``（并核当前受管
        代码）；重放时必须是 Campaign 有效身份链上某一身份的摘要。绑定它的 ``validation_attempt_created`` 事实再按
        自身形成期坐标精确核对。
        """

        from .receipts import _require_effective_tool, tool_evolutions

        tool_sha256 = payload["run_conditions"]["tool_sha256"]
        if new:
            if tool_sha256 != _require_effective_tool(self.store, campaign)["tool_sha256"]:
                raise ControlError("ValidationExecutionPlan 工具身份不是 Campaign 当前有效工具身份")
            return
        known = {campaign["tool_identity"]["tool_files_sha256"]} | {
            registration["to_identity"]["tool_files_sha256"]
            for _reference, registration in tool_evolutions(self.store, campaign)
        }
        if tool_sha256 not in known:
            raise ControlError("ValidationExecutionPlan 工具身份不在 Campaign 的有效工具身份链上")

    def _validate_validation_execution_plan(
        self, payload: dict[str, Any], *, new: bool = False
    ) -> None:
        from .receipts import replay_receipt

        campaign = self.store.load_campaign(payload["campaign_id"])
        self._same_persona(
            campaign["persona"], payload["persona"], "Campaign/ValidationExecutionPlan"
        )
        candidate_label = "ValidationExecutionPlan 的 Candidate 跨 Campaign"
        self._precheck_fact_ref(campaign, payload["candidate_ref"], candidate_label)
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        if candidate["campaign_id"] != payload["campaign_id"]:
            self._require_campaign_owned(
                campaign, payload["candidate_ref"], candidate["campaign_id"], candidate_label
            )
        candidate_payload = candidate["payload"]
        if candidate_payload.get("schema_version") is None:
            raise ControlError("ValidationExecutionPlan 只能绑定严格 v2 Candidate")
        if candidate_payload["candidate_id"] != payload["candidate_id"]:
            raise ControlError("ValidationExecutionPlan 与 Candidate ID 不一致")
        if candidate_payload["profile_approval_ref"] != payload["profile_approval_ref"]:
            raise ControlError("ValidationExecutionPlan 与 Candidate 批准引用不一致")
        if candidate_payload["candidate_build_receipt_ref"] != payload[
            "candidate_build_receipt_ref"
        ]:
            raise ControlError("ValidationExecutionPlan 与 CandidateBuildReceipt 引用不一致")
        build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
        if build["campaign_id"] != payload["campaign_id"]:
            self._require_campaign_owned(
                campaign,
                payload["candidate_build_receipt_ref"],
                build["campaign_id"],
                "ValidationExecutionPlan 的 CandidateBuildReceipt 跨 Campaign",
            )
        self._candidate_build_matches(campaign, candidate_payload, build)
        if build["validation_scope_sha256"] != payload["validation_scope_sha256"]:
            raise ControlError("ValidationExecutionPlan 与 VC-4 验证闭集摘要不一致")
        approval = self._fact(payload["profile_approval_ref"], "profile_approved")
        if approval["payload"]["scenario_plan_ref"] != payload["scenario_plan_ref"]:
            raise ControlError("ValidationExecutionPlan 未绑定批准的 ScenarioPlan")
        evidence_approval = self._fact(
            approval["payload"]["evidence_approval_ref"], "evidence_approved"
        )
        classification_ref = evidence_approval["payload"].get("classification_fact_ref")
        if classification_ref is None:
            raise ControlError("严格 ValidationExecutionPlan 要求 VC-2 AtomicAssertionLedger")
        classification = self._fact(classification_ref, "rule_classification_recorded")
        if classification["payload"]["atomic_assertion_ledger_ref"] != payload[
            "atomic_assertion_ledger_ref"
        ]:
            raise ControlError("ValidationExecutionPlan 未绑定 VC-2 AtomicAssertionLedger")
        selector = self._fact(
            payload["production_selector_before_ref"], "selector_observed"
        )
        self._object(selector["payload"]["catalog_snapshot_ref"], "runtime_catalog_snapshot")
        if not campaign_is_v2(campaign):
            if payload["run_conditions"]["tool_sha256"] != campaign["tool_bundle_sha256"]:
                raise ControlError("ValidationExecutionPlan 工具身份与 Campaign 不一致")
        else:
            self._require_plan_tool_identity(campaign, payload, new=new)
            if new:
                # B3-05：作废候选所属 revision 处于 revision_required，不得再为它封存执行计划。
                from .receipts import require_revision_not_invalidated

                require_revision_not_invalidated(
                    self.store,
                    campaign,
                    candidate["formed_under"]["candidate_revision"],
                    "ValidationExecutionPlan 封存",
                )
        for gate in payload["external_gates"]:
            if gate["source"] == "reuse":
                source_receipt = replay_receipt(
                    self.store, gate["reused_receipt_ref"]
                )
                expected_result = "pass"
                label = "复用门禁收据"
                self._require_reuse_within_revision(campaign, candidate, source_receipt, gate)
            elif gate["previous_failed_receipt_ref"] is not None:
                source_receipt = replay_receipt(
                    self.store, gate["previous_failed_receipt_ref"]
                )
                expected_result = "failed"
                label = "前序失败门禁收据"
                crossing = f"{label}跨越了 Campaign／Candidate：{gate['gate_id']}"
                if source_receipt["campaign_id"] != payload["campaign_id"]:
                    self._require_campaign_owned(
                        campaign,
                        gate["previous_failed_receipt_ref"],
                        source_receipt["campaign_id"],
                        crossing,
                    )
                if (
                    source_receipt["candidate_ref"] != payload["candidate_ref"]
                    or source_receipt["candidate_build_receipt_ref"]
                    != payload["candidate_build_receipt_ref"]
                ):
                    raise ControlError(crossing)
            else:
                continue
            for key in (
                "gate_id",
                "requirement_sha256",
                "command",
                "working_directory",
                "host_id",
                "architecture",
            ):
                if source_receipt[key] != gate[key]:
                    raise ControlError(
                        f"{label}的 {key} 与计划不一致：{gate['gate_id']}"
                    )
            if source_receipt["result"] != expected_result:
                raise ControlError(
                    f"{label}的结果必须是 {expected_result}：{gate['gate_id']}"
                )

    @staticmethod
    def _require_reuse_within_revision(
        campaign: dict[str, Any],
        candidate: dict[str, Any],
        source_receipt: dict[str, Any],
        gate: dict[str, Any],
    ) -> None:
        """B3-05：Campaign v2 的执行计划只能复用同一候选 revision 内形成的门禁收据。

        新 revision 的 VC-5 必须从新 attempt 完整执行：旧 revision（被作废或被直接取代）的门禁结果不得经「复用」
        流入新候选。只比较同一 Campaign 内的 v2 收据（跨 Campaign 的收据没有可比的形成期 revision）。
        """

        if not campaign_is_v2(campaign) or source_receipt["campaign_id"] != campaign["campaign_id"]:
            return
        formed = source_receipt.get("formed_under")
        if formed is None:
            return
        source_revision = formed["candidate_revision"]
        candidate_revision = candidate["formed_under"]["candidate_revision"]
        if source_revision != candidate_revision:
            raise ControlError(
                f"复用门禁收据形成于候选 revision r{source_revision}，不是计划候选的 r{candidate_revision}："
                f"新 revision 必须从新 attempt 完整执行，不得复用旧 revision 的门禁结果：{gate['gate_id']}"
            )

    def _validate_candidate_evidence_package(self, payload: dict[str, Any]) -> None:
        campaign = self.store.load_campaign(payload["campaign_id"])
        self._same_persona(
            campaign["persona"], payload["persona"], "Campaign/CandidateEvidencePackage"
        )
        if payload["target_version"] != campaign["target_version"]:
            raise ControlError("CandidateEvidencePackage 目标版本与 Campaign 不一致")
        crossing = "CandidateEvidencePackage 引用了其他 Campaign 的事实"
        for key in ("candidate_ref", "validation_attempt_ref"):
            self._precheck_fact_ref(campaign, payload[key], crossing)
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        attempt = self._fact(payload["validation_attempt_ref"], "validation_attempt_created")
        for key, fact in (("candidate_ref", candidate), ("validation_attempt_ref", attempt)):
            if fact["campaign_id"] != payload["campaign_id"]:
                self._require_campaign_owned(campaign, payload[key], fact["campaign_id"], crossing)
        candidate_payload = candidate["payload"]
        attempt_payload = attempt["payload"]
        for key in ("candidate_id", "candidate_build_receipt_ref"):
            if payload[key] != candidate_payload[key]:
                raise ControlError(f"CandidateEvidencePackage 与 Candidate 的 {key} 不一致")
        if payload["attempt_id"] != attempt_payload["attempt_id"]:
            raise ControlError("CandidateEvidencePackage 与 ValidationAttempt ID 不一致")
        for key in ("candidate_ref", "candidate_build_receipt_ref"):
            if payload[key] != attempt_payload[key]:
                raise ControlError(f"CandidateEvidencePackage 与 ValidationAttempt 的 {key} 不一致")
        plan = self._object(
            attempt_payload["validation_execution_plan_ref"], "validation_execution_plan"
        )
        if payload["atomic_assertion_ledger_ref"] != plan["atomic_assertion_ledger_ref"]:
            raise ControlError("CandidateEvidencePackage 与执行计划断言台账不一致")
        scenario_plan = self._object(plan["scenario_plan_ref"], "scenario_plan")
        approvals = [
            self._fact(reference, "scenario_approved")
            for reference in payload["scenario_approval_refs"]
        ]
        actual_scenarios: list[str] = []
        for approval in approvals:
            item = approval["payload"]
            if (
                item.get("schema_version") is None
                or item["validation_attempt_ref"] != payload["validation_attempt_ref"]
                or item["candidate_id"] != payload["candidate_id"]
                or item["attempt_id"] != payload["attempt_id"]
            ):
                raise ControlError("CandidateEvidencePackage 场景批准跨越 Candidate／attempt")
            actual_scenarios.append(item["scenario_id"])
        expected_scenarios = [item["id"] for item in scenario_plan["scenarios"]]
        if sorted(actual_scenarios) != sorted(expected_scenarios) or len(
            actual_scenarios
        ) != len(set(actual_scenarios)):
            raise ControlError("CandidateEvidencePackage 未唯一覆盖 ScenarioPlan 全集")
        ledger = self._object(
            payload["atomic_assertion_ledger_ref"], "atomic_assertion_ledger"
        )
        expected_scenario_assertions = {
            item["assertion_id"]: item
            for item in ledger["assertions"]
            if item["owner_kind"] == "scenario_only"
        }
        actual_scenario_assertions = {
            item["assertion_id"]: item
            for item in payload["scenario_assertion_results"]
        }
        if set(actual_scenario_assertions) != set(expected_scenario_assertions):
            raise ControlError("CandidateEvidencePackage 未唯一闭合 scenario-only 原子断言")
        for assertion_id, expected in expected_scenario_assertions.items():
            if actual_scenario_assertions[assertion_id]["owner_id"] != expected["owner_id"]:
                raise ControlError(f"scenario-only 断言 owner 不一致：{assertion_id}")
        for key in (
            "environment_before_ref",
            "environment_after_ref",
            "recovery_evidence_ref",
            "secret_scan_evidence_ref",
        ):
            evidence = self._object(payload[key], "operational_evidence")
            self._require_manifest_pass(evidence, f"CandidateEvidencePackage/{key}")

    def _validate_candidate_delivery_plan(self, payload: dict[str, Any]) -> None:
        campaign = self.store.load_campaign(payload["campaign_id"])
        self._same_persona(
            campaign["persona"], payload["persona"], "Campaign/CandidateDeliveryPlan"
        )
        crossing = "CandidateDeliveryPlan 引用了其他 Campaign 的事实"
        for key in ("validation_completed_ref", "acceptance_ref", "candidate_ref"):
            self._precheck_fact_ref(campaign, payload[key], crossing)
        completed = self._fact(payload["validation_completed_ref"], "validation_completed")
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        for key, fact in (
            ("validation_completed_ref", completed),
            ("acceptance_ref", acceptance),
            ("candidate_ref", candidate),
        ):
            if fact["campaign_id"] != payload["campaign_id"]:
                self._require_campaign_owned(campaign, payload[key], fact["campaign_id"], crossing)
        candidate_payload = candidate["payload"]
        completed_payload = completed["payload"]
        for key in ("candidate_id", "candidate_ref", "candidate_build_receipt_ref", "acceptance_ref"):
            expected = completed_payload[key] if key in completed_payload else payload[key]
            if payload[key] != expected:
                raise ControlError(f"CandidateDeliveryPlan 与 VC-5 完成事实的 {key} 不一致")
        if acceptance["payload"]["candidate_ref"] != payload["candidate_ref"]:
            raise ControlError("CandidateDeliveryPlan 与 Acceptance Candidate 不一致")
        if payload["candidate_evidence_package_ref"] != completed_payload[
            "candidate_evidence_package_ref"
        ]:
            raise ControlError("CandidateDeliveryPlan 与 VC-5 CandidateEvidencePackage 不一致")
        if payload["candidate_image_digest"] != candidate_payload["image_digest"]:
            raise ControlError("CandidateDeliveryPlan 未使用 VC-4 固定 Candidate 镜像")
        if payload["architecture"] != candidate_payload["target_architecture"]:
            raise ControlError("CandidateDeliveryPlan 架构与 Candidate 不一致")
        build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
        self._candidate_build_matches(campaign, candidate_payload, build)
        if payload["runtime_profile_sha256"] != build["profile_identity"]["profile_digest"]:
            raise ControlError("CandidateDeliveryPlan 运行 Profile 与 CandidateBuildReceipt 不一致")
        self._require_manifest_pass(
            self._object(payload["environment_freeze_ref"], "operational_evidence"),
            "CandidateDeliveryPlan/environment_freeze_ref",
        )
        for reference in payload["rollback_material_refs"]:
            self._require_manifest_pass(
                self._object(reference), "CandidateDeliveryPlan/rollback_material_refs"
            )

    def _validate_candidate_delivery_package(
        self, payload: dict[str, Any], *, new: bool = False
    ) -> None:
        campaign = self.store.load_campaign(payload["campaign_id"])
        self._same_persona(
            campaign["persona"], payload["persona"], "Campaign/CandidateDeliveryPackage"
        )
        plan = self._object(payload["delivery_plan_ref"], "candidate_delivery_plan")
        if plan["campaign_id"] != payload["campaign_id"]:
            self._require_campaign_owned(
                campaign,
                payload["delivery_plan_ref"],
                plan["campaign_id"],
                "CandidateDeliveryPackage 与交付计划 Campaign 不一致",
            )
        for key in (
            "delivery_id",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_completed_ref",
            "acceptance_ref",
            "candidate_evidence_package_ref",
        ):
            if payload[key] != plan[key]:
                raise ControlError(f"CandidateDeliveryPackage 与交付计划的 {key} 不一致")
        if payload["candidate_image_digest"] != plan["candidate_image_digest"]:
            raise ControlError("CandidateDeliveryPackage 镜像与交付计划不一致")
        facts = [
            self._fact(reference, "candidate_delivery_recorded")
            for reference in payload["delivery_fact_refs"]
        ]
        if [fact["payload"]["stage"] for fact in facts] != list(
            CANDIDATE_DELIVERY_STAGES
        ):
            raise ControlError("CandidateDeliveryPackage 未绑定有序四阶段候选交付事实")
        for fact in facts:
            if fact["payload"]["delivery_plan_ref"] != payload["delivery_plan_ref"]:
                raise ControlError("CandidateDeliveryPackage 混入其他交付计划事实")
            if fact["payload"]["result"] != "pass":
                raise ControlError("CandidateDeliveryPackage 不得封装失败的候选交付阶段")
        if payload["rollback_material_refs"] != plan["rollback_material_refs"]:
            raise ControlError("CandidateDeliveryPackage 回退材料与冻结计划不一致")
        deployment_configuration = self._object(
            payload["deployment_configuration_ref"], "operational_evidence"
        )
        self._require_manifest_pass(
            deployment_configuration,
            "CandidateDeliveryPackage/deployment_configuration_ref",
        )
        for key in ("public_evidence_index_refs", "rollback_material_refs"):
            for reference in payload[key]:
                self._require_manifest_pass(
                    self._object(reference), f"CandidateDeliveryPackage/{key}"
                )
        expected_artifact_count = (
            1
            + len(payload["public_evidence_index_refs"])
            + len(payload["rollback_material_refs"])
        )
        if payload["artifact_count"] != expected_artifact_count:
            raise ControlError(
                "CandidateDeliveryPackage artifact_count 与部署配置、公开索引和回退材料不一致"
            )
        archive = self._object(
            payload["private_archive_manifest_ref"], "private_archive_manifest"
        )
        if archive["campaign_id"] != payload["campaign_id"]:
            self._require_campaign_owned(
                campaign,
                payload["private_archive_manifest_ref"],
                archive["campaign_id"],
                "CandidateDeliveryPackage 私有归档清单跨 Campaign",
            )
        self._same_persona(payload["persona"], archive["persona"], "DeliveryPackage/Archive")
        if campaign_is_v2(campaign):
            self._require_package_valid_chain(payload, facts, new=new)

    def _require_package_valid_chain(
        self, payload: dict[str, Any], facts: list[dict[str, Any]], *, new: bool
    ) -> None:
        """Campaign v2 交付包（B4-07）：四阶段事实必须沿 ``previous_stage_ref`` 衔接成一条链（重放与写入都核）；
        封存时还必须正是唯一有效链四阶段的最终 pass、且没有待消费的阶段失效。

        有效链在封存之后仍可能因阶段失效与恢复段而前进，旧交付包只是不再能签发收据（收据核对有效链），所以重放
        只核链形状，不核「是否仍是当前有效链」。
        """

        for index in range(1, len(facts)):
            if facts[index]["payload"]["previous_stage_ref"] != self.store.fact_ref(facts[index - 1]):
                raise ControlError("CandidateDeliveryPackage 的四阶段事实没有沿 previous_stage_ref 衔接成一条链")
        if not new:
            return
        from .receipts import delivery_chain_state

        state = delivery_chain_state(self.store, payload["campaign_id"], payload["delivery_id"])
        if not state.complete():
            raise ControlError(
                "CandidateDeliveryPackage 只能在有效链四阶段全部通过、且没有待消费的阶段失效时封存"
            )
        if payload["delivery_fact_refs"] != state.valid_refs(self.store):
            raise ControlError("CandidateDeliveryPackage 必须绑定有效链四阶段的最终 pass")

    def _require_release_coordinate(
        self, left: dict[str, Any], right: dict[str, Any], label: str
    ) -> None:
        self._same_persona(left["persona"], right["persona"], label)
        for key in ("version", "profile_digest"):
            if left[key] != right[key]:
                raise ControlError(f"{label} 的 {key} 不一致")

    def _validate_ingress_closure(self, inventory: dict[str, Any]) -> None:
        observation = self._object(inventory["observation_ref"], "ingress_observation")
        self._same_persona(inventory["persona"], observation["persona"], "Ingress Inventory")
        observed = {item["alias_id"]: item for item in observation["aliases"]}
        claimed: dict[str, str] = {}
        for entry in inventory["entries"]:
            union_callers: set[str] = set()
            for alias_id in entry["physical_alias_ids"]:
                if alias_id not in observed:
                    raise ControlError(f"Inventory 声明了未发现的入口别名：{alias_id}")
                if alias_id in claimed:
                    raise ControlError(f"入口物理别名被重复处置：{alias_id}")
                alias = observed[alias_id]
                if alias["logical_ingress_id"] != entry["logical_ingress_id"]:
                    raise ControlError(f"入口别名归属与逻辑入口不一致：{alias_id}")
                claimed[alias_id] = entry["logical_ingress_id"]
                union_callers.update(alias["caller_ids"])
            if entry["caller_ids"] != sorted(union_callers):
                raise ControlError(
                    f"入口 {entry['logical_ingress_id']} 的调用方没有与发现清单闭合"
                )
        missing = sorted(set(observed) - set(claimed))
        if missing:
            raise ControlError(f"ProductionIngressInventory 遗漏物理别名：{missing}")

    def _validate_egress_closure(self, inventory: dict[str, Any]) -> None:
        observation = self._object(inventory["observation_ref"], "egress_observation")
        self._same_persona(inventory["persona"], observation["persona"], "Egress Inventory")
        observed = {item["egress_id"]: item for item in observation["egresses"]}
        claimed = {item["egress_id"]: item for item in inventory["entries"]}
        missing = sorted(set(observed) - set(claimed))
        extra = sorted(set(claimed) - set(observed))
        if missing or extra:
            raise ControlError(
                f"EgressDispositionInventory 未闭合：missing={missing}, extra={extra}"
            )
        for egress_id, observation_item in observed.items():
            entry = claimed[egress_id]
            if observation_item["oauth_related"] and not entry["current_disposition"]:
                raise ControlError(f"OAuth 出站未处置：{egress_id}")

    def _validate_active_support_envelope(self, payload: dict[str, Any]) -> None:
        support = self._object(payload["support_envelope_ref"], "support_envelope")
        approval = self._fact(payload["profile_approval_ref"], "profile_approved")
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        release = self._object(payload["release_artifact_ref"], "release_artifact")
        self._same_persona(payload["persona"], support["persona"], "Active/Support Envelope")
        self._same_persona(payload["persona"], release["persona"], "Active Envelope/Release")
        approval_payload = approval["payload"]
        acceptance_payload = acceptance["payload"]
        if approval_payload["approval_purpose"] != "production_replacement":
            raise ControlError("ActiveSupportEnvelope 不能来自 validation-only 批准")
        if acceptance_payload["result"] != "accepted":
            raise ControlError("ActiveSupportEnvelope 必须来自 production-replacement Acceptance")
        if acceptance_payload.get("schema_version") is not None:
            acceptance_ref = payload["acceptance_ref"]
            completed = [
                fact
                for fact in self.store.list_facts(acceptance["campaign_id"], "validation")
                if fact["fact_kind"] == "validation_completed"
                and fact["payload"]["acceptance_ref"] == acceptance_ref
            ]
            if len(completed) != 1:
                raise ControlError("严格 Acceptance 必须先形成唯一 VC-5 完成事实")
        if approval_payload["support_envelope_ref"] != payload["support_envelope_ref"]:
            raise ControlError("ActiveSupportEnvelope 与 ProfileApproval 的范围不一致")
        if approval_payload["release_artifact_ref"] != payload["release_artifact_ref"]:
            raise ControlError("ActiveSupportEnvelope 与 ProfileApproval 的 Release 不一致")
        if acceptance_payload["profile_approval_ref"] != payload["profile_approval_ref"]:
            raise ControlError("ActiveSupportEnvelope 与 Acceptance 的批准引用不一致")
        support_set = {capability_key(item) for item in support["capabilities"]}
        active_set = {capability_key(item) for item in payload["capabilities"]}
        if active_set != support_set:
            raise ControlError("ActiveSupportEnvelope 必须精确等于已批准且验收的 SupportEnvelope")

    def _validate_deployment_traffic_envelope(self, payload: dict[str, Any]) -> None:
        active = self._object(payload["active_support_envelope_ref"], "active_support_envelope")
        rollback = self._object(
            payload["rollback_operational_envelope_ref"],
            "rollback_operational_envelope",
        )
        inventory = self._object(
            payload["production_ingress_inventory_ref"],
            "production_ingress_inventory",
        )
        self._same_persona(payload["persona"], active["persona"], "Deployment/Active Envelope")
        self._same_persona(payload["persona"], rollback["persona"], "Deployment/Rollback Envelope")
        self._same_persona(payload["persona"], inventory["persona"], "Deployment/Inventory")
        if active["support_envelope_ref"] is None:
            raise ControlError("ActiveSupportEnvelope 缺少来源")
        deployment = {capability_key(item) for item in payload["capabilities"]}
        active_set = {capability_key(item) for item in active["capabilities"]}
        rollback_set = {capability_key(item) for item in rollback["capabilities"]}
        gap = sorted(deployment - (active_set & rollback_set))
        if gap:
            raise ControlError(
                "DeploymentTrafficEnvelope 不属于 Active 与 Rollback 范围交集："
                f"{gap}"
            )
        known_ingresses = {item["logical_ingress_id"] for item in inventory["entries"]}
        unknown = sorted(
            {item["logical_ingress_id"] for item in payload["capabilities"]}
            - known_ingresses
        )
        if unknown:
            raise ControlError(f"DeploymentTrafficEnvelope 引用了未知入口：{unknown}")

    def validate_append(self, campaign: dict[str, Any], document: dict[str, Any]) -> None:
        validate_fact_document(document)
        facts = self.store.list_facts(campaign["campaign_id"])
        facts_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            facts_by_kind[fact["fact_kind"]].append(fact)
        self._validate_fact_semantics(campaign, document, facts_by_kind, new=True)

    def _validate_fact_semantics(
        self,
        campaign: dict[str, Any],
        document: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        context: Any = None,
        *,
        new: bool = False,
    ) -> None:
        """按事实种类执行门禁。

        v2 事实的门禁一律用事实自身的 ``formed_under``：写入时它就是当前坐标（``append_fact`` 已核对当前受管
        代码匹配有效身份），重放时是形成期坐标——同一套判据既是写入时有效性，也是重放时形成期一致性。
        v1 事实没有坐标，工具摘要按 Campaign ``tool_bundle_sha256`` 解释，门禁逐字保持原语义。

        ``new`` 只在写入路径（``validate_append``）为真，另核只属于「当前」的判据：候选 revision 未作废（B3-05）、
        交付未 finalize 且只有最新交付段接受新事实（B4-07）。重放不跑这些判据——作废、finalize 之前形成的事实照常
        按形成期通过。
        """

        kind = document["fact_kind"]
        payload = document["payload"]
        if campaign_is_v2(campaign) != fact_is_v2(document):
            raise ControlError("事实合同版本与 Campaign 版本不一致")
        formed = document.get("formed_under")
        coordinates: dict[str, Any] | None = None
        if formed is not None:
            if context is None:
                from .receipts import FormedContext

                context = FormedContext(self.store, campaign)
            coordinates = context.coordinates(document)
            if new:
                self._require_revision_open(campaign, kind, formed)
            self._require_candidate_scope(kind, payload, formed, context)
        if kind == "discovery_recorded":
            self._gate_discovery(campaign, payload, facts_by_kind, coordinates)
        elif kind == "evidence_recorded":
            self._gate_evidence_recorded(campaign, payload, facts_by_kind, coordinates)
        elif kind == "rule_classification_recorded":
            self._gate_rule_classification(campaign, payload, facts_by_kind)
        elif kind == "evidence_approved":
            self._gate_evidence_approved(payload, facts_by_kind)
        elif kind == "profile_approved":
            self._gate_profile_approved(campaign, payload, facts_by_kind)
        elif kind == "candidate_frozen":
            self._gate_candidate(campaign, payload, facts_by_kind)
        elif kind in SCENARIO_FACT_STAGE:
            self._gate_scenario(kind, payload, facts_by_kind)
        elif kind == "pair_recorded":
            self._gate_pair(payload, facts_by_kind, formed, document)
        elif kind == "acceptance_recorded":
            self._gate_acceptance(payload, facts_by_kind, formed, document)
        elif kind == "validation_attempt_created":
            self._gate_validation_attempt(campaign, payload, facts_by_kind, coordinates)
        elif kind == "validation_completed":
            self._gate_validation_completed(campaign, payload, facts_by_kind, formed)
        elif kind in {"selector_observed", "selector_activated"}:
            self._gate_selector(kind, payload, facts_by_kind)
        elif kind == "release_promoted":
            self._gate_release_promoted(payload, facts_by_kind, formed)
        elif kind in DEPLOYMENT_STAGES:
            self._gate_deployment_stage(kind, payload, facts_by_kind, formed)
        elif kind == "inventory_current_appended":
            self._gate_inventory_current(payload, facts_by_kind)
        elif kind == "candidate_delivery_recorded":
            self._gate_candidate_delivery(
                payload, facts_by_kind, formed, document=document, campaign=campaign, new=new
            )
        elif kind == "candidate_revision_recorded":
            self._gate_candidate_revision(campaign, document, facts_by_kind, coordinates)
        elif kind == "evaluation_baseline_recorded":
            self._gate_evaluation_baseline(campaign, document, facts_by_kind, coordinates)

    def _require_revision_open(
        self, campaign: dict[str, Any], kind: str, formed: dict[str, Any]
    ) -> None:
        """写入时（B3-05）：当前候选 revision 已作废（revision_required）时，候选级事实一律拒绝。

        写入时形成期 revision 就是当前 revision；作废对象封存后，只有以 ``invalidation_ref`` 引用它的新 revision
        事实（不是候选级事实）能让 Campaign 继续前进。
        """

        if kind not in CANDIDATE_SCOPED_FACT_KINDS:
            return
        from .receipts import require_revision_not_invalidated

        require_revision_not_invalidated(
            self.store, campaign, formed["candidate_revision"], f"{kind} 写入"
        )

    def _require_candidate_scope(
        self,
        kind: str,
        payload: dict[str, Any],
        formed: dict[str, Any],
        context: Any,
    ) -> None:
        """v2：绑定候选的事实，其候选必须属于形成期候选 revision（显式 revision 事实指定的候选）。

        写入时形成期 revision 就是当前 revision，所以换 revision 后旧候选的任何新结果都被拒绝；隐含 r1（没有
        revision 事实）不限定候选。「当前 revision 未被作废」（B3-05）只在写入时由 ``_require_revision_open`` 核对。
        """

        if kind not in CANDIDATE_SCOPED_FACT_KINDS:
            return
        from .receipts import require_candidate_in_revision

        if kind == "release_promoted":
            candidate_id = self._fact(payload["candidate_ref"], "candidate_frozen")["payload"][
                "candidate_id"
            ]
        else:
            candidate_id = payload["candidate_id"]
        require_candidate_in_revision(context.revision_facts, candidate_id, formed, kind)

    @staticmethod
    def _expected_tool_sha256(
        campaign: dict[str, Any], coordinates: dict[str, Any] | None
    ) -> str:
        """工具摘要字段的期望值：v1 为 Campaign ``tool_bundle_sha256``；v2 为形成期有效身份的 ``tool_files_sha256``。"""

        if coordinates is None:
            return str(campaign["tool_bundle_sha256"])
        return str(coordinates["tool_sha256"])

    def _gate_discovery(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        coordinates: dict[str, Any] | None = None,
    ) -> None:
        if payload["version"] != campaign["target_version"]:
            raise ControlError("Discovery 版本与不可变 Campaign 目标不一致")
        if payload["tool_sha256"] != self._expected_tool_sha256(campaign, coordinates):
            if coordinates is not None:
                raise ControlError("Discovery 工具身份不是形成期有效工具身份")
            raise ControlError("Discovery 工具身份与 Campaign 不一致")
        if facts_by_kind["discovery_recorded"]:
            raise ControlError("同一 Campaign 不得覆盖 DiscoveryFact")

    def _gate_evidence_recorded(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        coordinates: dict[str, Any] | None = None,
    ) -> None:
        crossing = "EvidenceFact 引用了其他 Campaign 的 Discovery"
        self._precheck_fact_ref(campaign, payload["discovery_fact_ref"], crossing)
        discovery = self._fact(payload["discovery_fact_ref"], "discovery_recorded")
        if discovery["campaign_id"] != campaign["campaign_id"]:
            self._require_campaign_owned(
                campaign, payload["discovery_fact_ref"], discovery["campaign_id"], crossing
            )
        package = self._object(payload["evidence_package_ref"], "evidence_package")
        self._same_persona(campaign["persona"], package["persona"], "Campaign/EvidencePackage")
        if package["version"] != campaign["target_version"]:
            raise ControlError("EvidencePackage 版本与 Campaign 不一致")
        if coordinates is not None and self._listed_by_imports(
            campaign, payload["evidence_package_ref"]
        ):
            self._require_imported_evidence_package(campaign, payload["evidence_package_ref"])
        elif package["producer_tool_sha256"] != self._expected_tool_sha256(campaign, coordinates):
            if coordinates is not None:
                raise ControlError(
                    "EvidencePackage 工具身份不是形成期有效工具身份：工具变化须先登记演进"
                )
            raise ControlError("EvidencePackage 工具身份漂移，必须新建 Campaign")
        if package["official_artifacts"] != campaign["official_artifacts"]:
            raise ControlError("EvidencePackage 官方产物与 Campaign 身份不一致")
        if facts_by_kind["evidence_recorded"]:
            raise ControlError("同一 Campaign 不得原位替换 EvidencePackage")

    def _listed_by_imports(self, campaign: dict[str, Any], reference: dict[str, Any]) -> bool:
        """对象是否被本 Campaign 的某份前序导入登记列出（B1-14）。"""

        from .receipts import predecessor_imports

        return any(
            reference in registration["imported_objects"]
            for _import_ref, registration in predecessor_imports(self.store, campaign["campaign_id"])
        )

    def _require_imported_evidence_package(
        self, campaign: dict[str, Any], reference: dict[str, Any]
    ) -> None:
        """登记为前序导入的 EvidencePackage 一律按消费端核验判定（B1-14），不再看产出工具是否相同。

        EvidencePackage 没有所属 Campaign 字段，「来自其他 Campaign」由导入登记标明：被本 Campaign 的导入登记列出的包
        必须满足消费端核验四项（read_only_reuse 收据授权）才能引用，产出工具由收据核对过的源 Campaign 身份担保；
        即使前序与本 Campaign 工具身份相同、版本与官方产物也相同，回归夹具或未 finalize 的导入照样拒绝（否则平台等
        差异会绕过收据记录的 identity_differences）。没被列出的包仍按原工具身份判据。登记时另核：列入导入的包不得已被
        本 Campaign 的事实当本地证据引用（见 ``_require_unclaimed_evidence``），所以这条判据不会让既有事实的重放失效。
        """

        from .receipts import cross_campaign_denial

        reason = cross_campaign_denial(self.store, campaign, reference)
        if reason is not None:
            raise ControlError(f"EvidencePackage 是登记导入的前序证据，未获授权引用：{reason}")

    def _gate_rule_classification(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        evidence_fact = self._fact(payload["evidence_fact_ref"], "evidence_recorded")
        if evidence_fact["payload"]["evidence_package_ref"] != payload["evidence_package_ref"]:
            raise ControlError("规则分类未绑定 EvidenceFact 中的同一 EvidencePackage")
        package = self._object(payload["evidence_package_ref"], "evidence_package")
        if package["schema_version"] != EVIDENCE_PACKAGE_V3_SCHEMA:
            raise ControlError("rule_classification_recorded 只适用于 EvidencePackage v3")
        ledger = self._object(
            payload["rule_migration_ledger_ref"], "rule_migration_ledger"
        )
        atomic = self._object(
            payload["atomic_assertion_ledger_ref"], "atomic_assertion_ledger"
        )
        if ledger["campaign_id"] != campaign["campaign_id"]:
            self._require_campaign_owned(
                campaign,
                payload["rule_migration_ledger_ref"],
                ledger["campaign_id"],
                "RuleMigrationLedger 与 Campaign 身份不一致",
            )
        if ledger["evidence_package_ref"] != payload["evidence_package_ref"]:
            raise ControlError("RuleMigrationLedger 未绑定 VC-1 的 EvidencePackage")
        if atomic["rule_migration_ledger_ref"] != payload["rule_migration_ledger_ref"]:
            raise ControlError("AtomicAssertionLedger 未绑定同一 RuleMigrationLedger")
        # 断言台账与其规则台账同属一个 Campaign（对象图门禁保证），v1 走到这里时必然与 Campaign 一致；导入的规则台账
        # 获授权后，同样来自前序 Campaign 的断言台账也要单独满足消费端核验（B1-14）。
        if atomic["campaign_id"] != campaign["campaign_id"]:
            self._require_campaign_owned(
                campaign,
                payload["atomic_assertion_ledger_ref"],
                atomic["campaign_id"],
                "AtomicAssertionLedger 与 Campaign 身份不一致",
            )
        for label, value in (("RuleMigrationLedger", ledger), ("AtomicAssertionLedger", atomic)):
            self._same_persona(campaign["persona"], value["persona"], f"Campaign/{label}")
            if value["target_version"] != campaign["target_version"]:
                raise ControlError(f"{label} 目标版本与 Campaign 不一致")
        blocked = sorted(
            rule["spec_id"]
            for rule in ledger["rules"]
            if rule["evidence_level"] == "blocked"
        )
        if blocked:
            raise ControlError(f"VC-2 分类仍包含 blocked 规则：{blocked}")
        self.validate_object_graph(payload["rule_migration_ledger_ref"])
        self.validate_object_graph(payload["atomic_assertion_ledger_ref"])
        if facts_by_kind["rule_classification_recorded"]:
            raise ControlError("同一 Campaign 不得覆盖规则分类事实")

    def _gate_evidence_approved(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        evidence = self._fact(payload["evidence_fact_ref"], "evidence_recorded")
        if evidence["payload"]["evidence_package_ref"] != payload["evidence_package_ref"]:
            raise ControlError("EvidenceApproval 未批准 EvidenceFact 中的同一 EvidencePackage")
        package = self._object(payload["evidence_package_ref"], "evidence_package")
        classification_ref = payload.get("classification_fact_ref")
        if package["schema_version"] == EVIDENCE_PACKAGE_V3_SCHEMA:
            if classification_ref is None:
                raise ControlError("EvidencePackage v3 必须先绑定 VC-2 规则分类事实")
            classification = self._fact(
                classification_ref, "rule_classification_recorded"
            )
            classification_payload = classification["payload"]
            if (
                classification_payload["evidence_fact_ref"]
                != payload["evidence_fact_ref"]
                or classification_payload["evidence_package_ref"]
                != payload["evidence_package_ref"]
            ):
                raise ControlError("EvidenceApproval 与规则分类事实没有绑定同一 VC-1 证据")
        elif classification_ref is not None:
            raise ControlError("历史 EvidencePackage v1/v2 不得伪造 VC-2 分类事实引用")
        if facts_by_kind["evidence_approved"]:
            raise ControlError("Evidence 批准变化必须建立新的 Campaign，禁止覆盖")

    def _gate_profile_approved(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        evidence_approval = self._fact(payload["evidence_approval_ref"], "evidence_approved")
        evidence = self._object(
            evidence_approval["payload"]["evidence_package_ref"], "evidence_package"
        )
        migration_ledger: dict[str, Any] | None = None
        if evidence["schema_version"] == EVIDENCE_PACKAGE_V3_SCHEMA:
            classification = self._fact(
                evidence_approval["payload"]["classification_fact_ref"],
                "rule_classification_recorded",
            )
            migration_ledger = self._object(
                classification["payload"]["rule_migration_ledger_ref"],
                "rule_migration_ledger",
            )
            self.validate_object_graph(
                classification["payload"]["atomic_assertion_ledger_ref"]
            )
        descriptor = self._object(payload["persona_descriptor_ref"], "persona_descriptor")
        profile_schema = self._object(payload["profile_schema_ref"], "profile_schema")
        snapshot = self._object(payload["snapshot_ref"], "snapshot")
        release = self._object(payload["release_artifact_ref"], "release_artifact")
        support = self._object(payload["support_envelope_ref"], "support_envelope")
        ingress = self._object(
            payload["production_ingress_inventory_ref"], "production_ingress_inventory"
        )
        egress = self._object(
            payload["egress_disposition_inventory_ref"], "egress_disposition_inventory"
        )
        deployment_plan = self._object(
            payload["deployment_plan_ref"], "deployment_plan"
        )
        scenario_plan = self._object(payload["scenario_plan_ref"], "scenario_plan")
        for label, value in (
            ("Descriptor", descriptor),
            ("ProfileSchema", profile_schema),
            ("Snapshot", snapshot),
            ("Release", release),
            ("SupportEnvelope", support),
            ("IngressInventory", ingress),
            ("EgressInventory", egress),
            ("DeploymentPlan", deployment_plan),
            ("ScenarioPlan", scenario_plan),
        ):
            self._same_persona(campaign["persona"], value["persona"], f"Campaign/{label}")
        for label, value in (
            ("ProfileSchema", profile_schema),
            ("Snapshot", snapshot),
            ("Release", release),
        ):
            if value["version"] != campaign["target_version"]:
                raise ControlError(f"{label} 版本与 Campaign 不一致")
        self.validate_object_graph(payload["production_ingress_inventory_ref"])
        self.validate_object_graph(payload["egress_disposition_inventory_ref"])
        self.validate_object_graph(payload["support_envelope_ref"])
        self.validate_object_graph(payload["release_artifact_ref"])
        target_specs = set(payload["target_spec_ids"])
        if target_specs != set(support["target_spec_ids"]):
            raise ControlError("ProfileApproval 与 SupportEnvelope 的目标规则全集不一致")
        support_capabilities = {
            capability_key(item) for item in support["capabilities"]
        }
        planned_active = {
            capability_key(item)
            for item in deployment_plan["active_support_capabilities"]
        }
        if planned_active != support_capabilities:
            raise ControlError("DeploymentPlan 的 Active 范围与 SupportEnvelope 不一致")
        if migration_ledger is None:
            evidence_rules = {rule["spec_id"]: rule for rule in evidence["rules"]}
        else:
            if target_specs != set(migration_ledger["target_spec_ids"]):
                raise ControlError(
                    "ProfileApproval 与 RuleMigrationLedger 的目标规则全集不一致"
                )
            evidence_rules = {
                rule["spec_id"]: rule
                for rule in migration_ledger["rules"]
                if rule["spec_id"] in set(migration_ledger["target_spec_ids"])
            }
        missing_rules = sorted(target_specs - set(evidence_rules))
        if missing_rules:
            source = "RuleMigrationLedger" if migration_ledger is not None else "EvidencePackage"
            raise ControlError(f"ProfileApproval 引用了 {source} 不存在的规则：{missing_rules}")
        for spec_id in sorted(target_specs):
            rule = evidence_rules[spec_id]
            if rule["compatibility_class"] != "request_egress":
                raise ControlError(f"SupportEnvelope 目标规则不是 request_egress：{spec_id}")
            if (
                payload["approval_purpose"] == "production_replacement"
                and rule["evidence_level"] != "verified"
            ):
                raise ControlError(
                    f"production_replacement 规则没有达到 verified：{spec_id}={rule['evidence_level']}"
                )
        ingress_ids = {item["logical_ingress_id"] for item in ingress["entries"]}
        ingress_targets = {
            item["logical_ingress_id"]: item
            for item in payload["ingress_target_dispositions"]
        }
        if set(ingress_targets) != ingress_ids:
            raise ControlError("入口 target_disposition 没有覆盖完整当前 Inventory")
        migrated_ids = {
            ingress_id
            for ingress_id, item in ingress_targets.items()
            if item["target_disposition"] == "migrated_strict"
        }
        support_ids = {item["logical_ingress_id"] for item in support["capabilities"]}
        if support_ids != migrated_ids:
            raise ControlError(
                "SupportEnvelope 只能且必须覆盖 target_disposition=migrated_strict 的入口"
            )
        planned_specs = {
            spec_id
            for scenario in scenario_plan["scenarios"]
            for spec_id in scenario["spec_ids"]
        }
        if planned_specs != target_specs:
            raise ControlError("ScenarioPlan 没有精确覆盖批准的目标规则全集")
        migrated_protocols = {
            item["protocol_class"]
            for item in ingress["entries"]
            if item["logical_ingress_id"] in migrated_ids
        }
        planned_protocols = {
            protocol
            for scenario in scenario_plan["scenarios"]
            for protocol in scenario["ingress_protocol_classes"]
        }
        if not migrated_protocols.issubset(planned_protocols):
            raise ControlError("ScenarioPlan 遗漏 migrated_strict 入口协议类别")
        egress_ids = {item["egress_id"] for item in egress["entries"]}
        egress_targets = {item["egress_id"]: item for item in payload["egress_target_dispositions"]}
        if set(egress_targets) != egress_ids:
            raise ControlError("出站 target_disposition 没有覆盖完整当前 Inventory")
        for item in egress_targets.values():
            if item["target_disposition"] == "persona_strict":
                unknown_specs = sorted(set(item["spec_ids"]) - target_specs)
                if unknown_specs:
                    raise ControlError(f"strict 出站绑定了范围外 SPEC：{unknown_specs}")
        identities = {
            fact["payload"]["identity_sha256"]
            for fact in facts_by_kind["profile_approved"]
        }
        if payload["identity_sha256"] in identities:
            raise ControlError("相同 ProfileApprovalFact 不得重复追加")

    def _gate_candidate(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        approval = self._fact(payload["profile_approval_ref"], "profile_approved")
        approval_payload = approval["payload"]
        if payload["release_artifact_ref"] != approval_payload["release_artifact_ref"]:
            raise ControlError("Candidate 没有引用批准的 ReleaseArtifact")
        if payload["support_envelope_ref"] != approval_payload["support_envelope_ref"]:
            raise ControlError("Candidate 没有冻结批准的 SupportEnvelope")
        if payload["candidate_purpose"] != approval_payload["approval_purpose"]:
            raise ControlError("Candidate 用途与 ProfileApproval 不一致")
        release = self._object(payload["release_artifact_ref"], "release_artifact")
        self._same_persona(campaign["persona"], release["persona"], "Campaign/Candidate Release")
        if payload.get("schema_version") is not None:
            build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
            self._candidate_build_matches(campaign, payload, build)
        existing_ids = {
            fact["payload"]["candidate_id"] for fact in facts_by_kind["candidate_frozen"]
        }
        existing_identities = {
            fact["payload"]["identity_sha256"] for fact in facts_by_kind["candidate_frozen"]
        }
        if payload["candidate_id"] in existing_ids or payload["identity_sha256"] in existing_identities:
            raise ControlError("Candidate ID 或不可变身份重复，禁止覆盖")

    def _candidate_by_id(
        self, candidate_id: str, facts_by_kind: dict[str, list[dict[str, Any]]]
    ) -> dict[str, Any]:
        matches = [
            fact
            for fact in facts_by_kind["candidate_frozen"]
            if fact["payload"]["candidate_id"] == candidate_id
        ]
        if len(matches) != 1:
            raise ControlError(f"candidate_id 没有唯一冻结事实：{candidate_id}")
        return matches[0]

    def _gate_scenario(
        self,
        kind: str,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        candidate = self._candidate_by_id(payload["candidate_id"], facts_by_kind)
        if payload.get("schema_version") is not None:
            attempt = self._fact(
                payload["validation_attempt_ref"], "validation_attempt_created"
            )
            if (
                attempt["payload"]["candidate_ref"] != self.store.fact_ref(candidate)
                or attempt["payload"]["candidate_id"] != payload["candidate_id"]
                or attempt["payload"]["attempt_id"] != payload["attempt_id"]
            ):
                raise ControlError("严格场景事实未绑定同一 ValidationAttempt／Candidate")
        stage = SCENARIO_FACT_STAGE[kind]
        stages = ("prepare", "capture", "seal", "approve")
        identity = (
            payload["candidate_id"],
            payload["scenario_id"],
            payload["attempt_id"],
        )
        fact_kind_by_stage = {value: key for key, value in SCENARIO_FACT_STAGE.items()}
        for existing_kind in (fact_kind_by_stage[item] for item in stages):
            for fact in facts_by_kind[existing_kind]:
                existing = fact["payload"]
                if (
                    existing["candidate_id"],
                    existing["scenario_id"],
                    existing["attempt_id"],
                ) == identity and existing_kind == kind:
                    raise ControlError(f"场景阶段不得覆盖：{identity}/{stage}")
        if stage == "prepare":
            return
        previous_stage = stages[stages.index(stage) - 1]
        previous = self._fact(
            payload["previous_stage_ref"], fact_kind_by_stage[previous_stage]
        )
        previous_payload = previous["payload"]
        previous_identity = (
            previous_payload["candidate_id"],
            previous_payload["scenario_id"],
            previous_payload["attempt_id"],
        )
        if previous_identity != identity:
            raise ControlError("场景四阶段跨越了 candidate/scenario/attempt 边界")
        required_result = "prepared" if previous_stage == "prepare" else "pass"
        if previous_payload["result"] != required_result:
            raise ControlError("失败 attempt 不得继续推进，必须建立新 attempt")

    @staticmethod
    def _require_result_baseline(
        payload: dict[str, Any], formed: dict[str, Any], label: str
    ) -> None:
        """B4-04：评估层结果（PAIR／Acceptance）记录的评估基线必须是其形成期评估基线。

        评估基线 b<K>（K≥1）下形成的结果必须用 v3 合同并逐字记录 ``{evaluation_baseline, baseline_commit_sha256}``；
        没有评估基线（b0）时只接受 v2（及历史）形状。写入时形成期基线就是当前基线，所以这同时是「绑定当前基线」。
        """

        baseline = formed["evaluation_baseline"]
        if payload_evaluation_baseline(payload):
            if baseline is None:
                raise ControlError(
                    f"{label} v3 只能在评估基线 b<K>（K≥1）下形成：形成期没有评估基线（b0）时使用 v2 合同"
                )
            recorded = {
                "baseline": payload["evaluation_baseline"],
                "baseline_commit_sha256": payload["baseline_commit_sha256"],
            }
            if recorded != baseline:
                raise ControlError(
                    f"{label} 记录的评估基线 b{recorded['baseline']} 不是形成期评估基线 b{baseline['baseline']}"
                    "（编号或 COMMIT 摘要不一致）"
                )
        elif baseline is not None:
            raise ControlError(
                f"{label} 形成于评估基线 b{baseline['baseline']}：基线下的结果必须使用 v3 合同并记录 evaluation_baseline"
            )

    def _require_baseline_open_for_pairs(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        document: dict[str, Any],
    ) -> None:
        """B4-04：同一候选在某评估基线下的 Acceptance 形成之后，该基线的 PAIR 集合即封闭，不得再追加 PAIR。

        b0 下 Acceptance 覆盖全部目标规则、PAIR 按 (candidate, pair_id) 唯一，本来就追加不了；b<K> 下未受影响的规则
        沿用旧基线的 PAIR、在 b<K> 没有自己的 PAIR，这里补上同样的封闭——要改评估结论只能再开新基线。按序号判定，
        写入与重放同一判据。
        """

        baseline = payload_evaluation_baseline(payload)
        if any(
            fact["payload"]["candidate_id"] == payload["candidate_id"]
            and payload_evaluation_baseline(fact["payload"]) == baseline
            and fact["sequence"] < document["sequence"]
            for fact in facts_by_kind["acceptance_recorded"]
        ):
            raise ControlError(
                f"候选 {payload['candidate_id']} 在评估基线 b{baseline} 的 Acceptance 已形成：同一基线不得再追加 PAIR，"
                "要改评估结论须开新的评估基线"
            )

    def _gate_pair(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        formed: dict[str, Any] | None = None,
        document: dict[str, Any] | None = None,
    ) -> None:
        candidate = self._candidate_by_id(payload["candidate_id"], facts_by_kind)
        if payload["release_artifact_ref"] != candidate["payload"]["release_artifact_ref"]:
            raise ControlError("PAIR 与 Candidate Release 不一致")
        if payload.get("schema_version") is not None:
            if payload["profile_approval_ref"] != candidate["payload"]["profile_approval_ref"]:
                raise ControlError("严格 PAIR 与 Candidate 的 ProfileApproval 不一致")
            package = self._object(
                payload["candidate_evidence_package_ref"],
                "candidate_evidence_package",
            )
            if (
                package["candidate_id"] != payload["candidate_id"]
                or package["candidate_ref"] != self.store.fact_ref(candidate)
            ):
                raise ControlError("严格 PAIR 未绑定同一 CandidateEvidencePackage")
            if payload["atomic_assertion_ledger_ref"] != package[
                "atomic_assertion_ledger_ref"
            ]:
                raise ControlError("严格 PAIR 与 CandidateEvidencePackage 的断言台账不一致")
            ledger = self._object(
                payload["atomic_assertion_ledger_ref"], "atomic_assertion_ledger"
            )
            expected_assertions = {
                item["assertion_id"]
                for item in ledger["assertions"]
                if item["owner_kind"] == "required_rule"
                and item["owner_id"] == payload["spec_id"]
            }
            actual_assertions = {
                item["assertion_id"] for item in payload["assertion_results"]
            }
            if actual_assertions != expected_assertions:
                raise ControlError("严格 PAIR 未唯一闭合本规则的原子断言")
            migration = self._object(
                ledger["rule_migration_ledger_ref"], "rule_migration_ledger"
            )
            expected_source = (
                "execute" if payload["spec_id"] in migration["affected_rules"] else "reuse"
            )
            if payload["result_source"] != expected_source or any(
                item["source"] != expected_source for item in payload["assertion_results"]
            ):
                raise ControlError("严格 PAIR 的 execute／reuse 来源与 RuleMigrationLedger 不一致")
        if formed is not None:
            self._require_result_baseline(payload, formed, "PAIR")
            if document is not None:
                self._require_baseline_open_for_pairs(payload, facts_by_kind, document)
        for reference in payload["scenario_approval_refs"]:
            scenario = self._fact(reference, "scenario_approved")
            if scenario["payload"]["candidate_id"] != payload["candidate_id"]:
                raise ControlError("PAIR 引用了其他 Candidate 的场景批准")
            if payload.get("schema_version") is not None and reference not in package[
                "scenario_approval_refs"
            ]:
                raise ControlError("严格 PAIR 引用了 CandidateEvidencePackage 范围外场景")
        # B4-04：唯一性是 (candidate, pair_id, 评估基线)；v1 与 b0 的基线编号都是 0，判据与原来相同。
        baseline = payload_evaluation_baseline(payload)
        if any(
            fact["payload"]["pair_id"] == payload["pair_id"]
            and fact["payload"]["candidate_id"] == payload["candidate_id"]
            and payload_evaluation_baseline(fact["payload"]) == baseline
            for fact in facts_by_kind["pair_recorded"]
        ):
            if baseline:
                raise ControlError(f"同一 Candidate 的 PAIR 不得重复或覆盖（评估基线 b{baseline}）")
            raise ControlError("同一 Candidate 的 PAIR 不得重复或覆盖")
        approval = self._fact(candidate["payload"]["profile_approval_ref"], "profile_approved")
        approval_payload = approval["payload"]
        if payload["spec_id"] not in approval_payload["target_spec_ids"]:
            raise ControlError("PAIR 不属于已批准目标规则全集")
        ingress = self._object(
            approval_payload["production_ingress_inventory_ref"],
            "production_ingress_inventory",
        )
        target = {
            item["logical_ingress_id"]: item["target_disposition"]
            for item in approval_payload["ingress_target_dispositions"]
        }
        migrated = [
            item
            for item in ingress["entries"]
            if target[item["logical_ingress_id"]] == "migrated_strict"
        ]
        official_ids = {
            item["logical_ingress_id"] for item in migrated if item["ingress_kind"] == "official"
        }
        if payload["official_result"]["ingress_id"] not in official_ids:
            raise ControlError("PAIR 官方结果没有命中 migrated_strict 官方入口")
        third_party_by_protocol: dict[str, set[str]] = defaultdict(set)
        for item in migrated:
            if item["ingress_kind"] == "third_party":
                third_party_by_protocol[item["protocol_class"]].add(item["logical_ingress_id"])
        actual_protocols = {
            item["protocol_class"]: item["ingress_id"]
            for item in payload["third_party_results"]
        }
        if set(actual_protocols) != set(third_party_by_protocol):
            raise ControlError("PAIR 未覆盖每类 lossless 第三方标准 API 入口")
        for protocol, ingress_id in actual_protocols.items():
            if ingress_id not in third_party_by_protocol[protocol]:
                raise ControlError(f"PAIR 第三方入口与协议类别不一致：{protocol}/{ingress_id}")

    def _require_effective_pairs(
        self,
        payload: dict[str, Any],
        pair_facts: list[dict[str, Any]],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        document: dict[str, Any],
    ) -> None:
        """B4-04：评估基线 b<K> 的 Acceptance 只能消费该基线下的有效 PAIR。

        每条规则的有效 PAIR＝本 Acceptance 之前、同一候选同一 pair_id、基线编号不大于 K 的 PAIR 中基线最新的一份：
        重评时受影响的规则在 b<K> 重跑写出 v3 PAIR，必须引用它；未受影响的规则沿用上一基线的 PAIR，不必重跑。
        引用更晚基线的 PAIR、或在新基线已重跑后仍引用旧基线的结果，都拒绝。PAIR 与 Acceptance 同在 validation
        维度，按序号取「之前」，写入与重放同一判据。
        """

        own = payload_evaluation_baseline(payload)
        for pair in pair_facts:
            pair_payload = pair["payload"]
            theirs = payload_evaluation_baseline(pair_payload)
            if theirs > own:
                raise ControlError(f"Acceptance（b{own}）不得引用更晚评估基线 b{theirs} 的 PAIR")
            latest = max(
                (
                    payload_evaluation_baseline(fact["payload"])
                    for fact in facts_by_kind["pair_recorded"]
                    if fact["payload"]["candidate_id"] == pair_payload["candidate_id"]
                    and fact["payload"]["pair_id"] == pair_payload["pair_id"]
                    and fact["sequence"] < document["sequence"]
                    and payload_evaluation_baseline(fact["payload"]) <= own
                ),
                default=theirs,
            )
            if theirs != latest:
                raise ControlError(
                    f"Acceptance（b{own}）引用的 {pair_payload['pair_id']} 不是该评估基线下的有效结果："
                    f"b{latest} 已重跑该规则，b{theirs} 的旧结果不得冒充"
                )

    def _gate_acceptance(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        formed: dict[str, Any] | None = None,
        document: dict[str, Any] | None = None,
    ) -> None:
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        if candidate["payload"]["candidate_id"] != payload["candidate_id"]:
            raise ControlError("Acceptance 的 Candidate ID 与引用不一致")
        if candidate["payload"]["profile_approval_ref"] != payload["profile_approval_ref"]:
            raise ControlError("Acceptance 与 Candidate 的 ProfileApproval 不一致")
        approval = self._fact(payload["profile_approval_ref"], "profile_approved")
        if approval["payload"]["approval_purpose"] != payload["acceptance_purpose"]:
            raise ControlError("Acceptance 用途与 ProfileApproval 不一致")
        pair_facts = [self._fact(reference, "pair_recorded") for reference in payload["pair_refs"]]
        if payload.get("schema_version") is not None:
            if candidate["payload"].get("schema_version") is None:
                raise ControlError("严格 Acceptance 只能绑定严格 v2 Candidate")
            if payload["candidate_build_receipt_ref"] != candidate["payload"][
                "candidate_build_receipt_ref"
            ]:
                raise ControlError("Acceptance 与 CandidateBuildReceipt 不一致")
            attempt = self._fact(
                payload["validation_attempt_ref"], "validation_attempt_created"
            )
            if attempt["payload"]["candidate_ref"] != payload["candidate_ref"]:
                raise ControlError("Acceptance 与 ValidationAttempt Candidate 不一致")
            package = self._object(
                payload["candidate_evidence_package_ref"],
                "candidate_evidence_package",
            )
            if (
                package["validation_attempt_ref"] != payload["validation_attempt_ref"]
                or package["candidate_ref"] != payload["candidate_ref"]
                or package["candidate_build_receipt_ref"]
                != payload["candidate_build_receipt_ref"]
            ):
                raise ControlError("Acceptance 与 CandidateEvidencePackage 身份不一致")
            if package["atomic_assertion_ledger_ref"] != payload[
                "atomic_assertion_ledger_ref"
            ]:
                raise ControlError("Acceptance 与 AtomicAssertionLedger 不一致")
            for pair in pair_facts:
                pair_payload = pair["payload"]
                if pair_payload.get("schema_version") is None:
                    raise ControlError("严格 Acceptance 不得消费历史 PAIR")
                if (
                    pair_payload["profile_approval_ref"] != payload["profile_approval_ref"]
                    or pair_payload["candidate_evidence_package_ref"]
                    != payload["candidate_evidence_package_ref"]
                    or pair_payload["atomic_assertion_ledger_ref"]
                    != payload["atomic_assertion_ledger_ref"]
                ):
                    raise ControlError("严格 Acceptance 混入其他 Profile／证据／断言台账的 PAIR")
            plan = self._object(
                attempt["payload"]["validation_execution_plan_ref"],
                "validation_execution_plan",
            )
            expected_gates = {item["gate_id"] for item in plan["external_gates"]}
            gate_receipts = [
                self.store.load_receipt(reference)
                for reference in payload["external_gate_receipt_refs"]
            ]
            actual_gates = [item["gate_id"] for item in gate_receipts]
            if set(actual_gates) != expected_gates or len(actual_gates) != len(expected_gates):
                raise ControlError("Acceptance 外部门禁收据未唯一覆盖执行计划")
            for gate in gate_receipts:
                if (
                    gate["result"] != "pass"
                    or gate["validation_attempt_ref"] != payload["validation_attempt_ref"]
                    or gate["candidate_build_receipt_ref"]
                    != payload["candidate_build_receipt_ref"]
                ):
                    raise ControlError("Acceptance 存在失败或身份漂移的外部门禁收据")
            self._require_validation_gate_closure(
                campaign_id=candidate["campaign_id"],
                candidate_id=payload["candidate_id"],
                candidate_ref=payload["candidate_ref"],
                candidate_build_receipt_ref=payload[
                    "candidate_build_receipt_ref"
                ],
                validation_attempt_ref=payload["validation_attempt_ref"],
                validation_execution_plan_ref=attempt["payload"][
                    "validation_execution_plan_ref"
                ],
                supplied_refs=payload["external_gate_receipt_refs"],
                plan=plan,
            )
        for pair in pair_facts:
            if pair["payload"]["candidate_id"] != payload["candidate_id"]:
                raise ControlError("Acceptance 引用了其他 Candidate 的 PAIR")
        if formed is not None and document is not None:
            self._require_result_baseline(payload, formed, "Acceptance")
            self._require_effective_pairs(payload, pair_facts, facts_by_kind, document)
        expected_specs = set(approval["payload"]["target_spec_ids"])
        actual_specs = [pair["payload"]["spec_id"] for pair in pair_facts]
        if set(actual_specs) != expected_specs or len(actual_specs) != len(expected_specs):
            raise ControlError("Acceptance 的逐规则 PAIR 未唯一覆盖目标全集")
        support = self._object(approval["payload"]["support_envelope_ref"], "support_envelope")
        expected_boundaries = {
            canonical_sha256(item) for item in support["boundary_assertion_refs"]
        }
        actual_boundaries = {
            canonical_sha256(item) for item in payload["boundary_assertion_refs"]
        }
        if actual_boundaries != expected_boundaries:
            raise ControlError("Acceptance 未精确绑定 SupportEnvelope 的范围外 fail-close 断言")
        if payload.get("schema_version") is not None:
            for reference in payload["inventory_assertion_refs"]:
                evidence = self._object(reference)
                self._require_manifest_pass(evidence, "Acceptance/inventory_assertion_refs")
        # B4-04：唯一性是 (candidate, 评估基线)；v1 与 b0 的基线编号都是 0，判据与原来相同。
        baseline = payload_evaluation_baseline(payload)
        if any(
            fact["payload"]["candidate_id"] == payload["candidate_id"]
            and payload_evaluation_baseline(fact["payload"]) == baseline
            for fact in facts_by_kind["acceptance_recorded"]
        ):
            if baseline:
                raise ControlError(f"同一 Candidate 的 AcceptanceFact 不得覆盖（评估基线 b{baseline}）")
            raise ControlError("同一 Candidate 的 AcceptanceFact 不得覆盖")

    def _gate_validation_attempt(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        coordinates: dict[str, Any] | None = None,
    ) -> None:
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        candidate_payload = candidate["payload"]
        if candidate_payload.get("schema_version") is None:
            raise ControlError("ValidationAttempt 只能绑定严格 v2 Candidate")
        if (
            coordinates is not None
            and payload["run_conditions"]["tool_sha256"] != coordinates["tool_sha256"]
        ):
            raise ControlError("ValidationAttempt 的运行工具身份不是形成期有效工具身份")
        if (
            candidate_payload["candidate_id"] != payload["candidate_id"]
            or candidate_payload["candidate_build_receipt_ref"]
            != payload["candidate_build_receipt_ref"]
        ):
            raise ControlError("ValidationAttempt 与 Candidate／构建收据不一致")
        build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
        self._candidate_build_matches(campaign, candidate_payload, build)
        plan = self._object(
            payload["validation_execution_plan_ref"], "validation_execution_plan"
        )
        expected = {
            "candidate_id": payload["candidate_id"],
            "candidate_ref": payload["candidate_ref"],
            "candidate_build_receipt_ref": payload["candidate_build_receipt_ref"],
            "run_conditions": payload["run_conditions"],
            "production_selector_before_ref": payload["production_selector_before_ref"],
            "vircs_state": payload["vircs_state"],
        }
        for key, expected_value in expected.items():
            if plan[key] != expected_value:
                raise ControlError(f"ValidationAttempt 与 ValidationExecutionPlan 的 {key} 不一致")
        if any(
            fact["payload"]["attempt_id"] == payload["attempt_id"]
            or fact["payload"]["validation_execution_plan_ref"]
            == payload["validation_execution_plan_ref"]
            for fact in facts_by_kind["validation_attempt_created"]
        ):
            raise ControlError("ValidationAttempt ID 或执行计划不得重复使用")

    def _gate_validation_completed(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        formed: dict[str, Any] | None = None,
    ) -> None:
        from .receipts import require_acceptance_formed_under

        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        acceptance_payload = acceptance["payload"]
        if acceptance_payload.get("schema_version") is None:
            raise ControlError("validation_completed 只能绑定严格 v2 Acceptance")
        require_acceptance_formed_under(acceptance, formed, "validation_completed")
        attempt = self._fact(
            payload["validation_attempt_ref"], "validation_attempt_created"
        )
        attempt_payload = attempt["payload"]
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        candidate_payload = candidate["payload"]
        expected_from_acceptance = {
            "candidate_id": acceptance_payload["candidate_id"],
            "candidate_ref": acceptance_payload["candidate_ref"],
            "candidate_build_receipt_ref": acceptance_payload[
                "candidate_build_receipt_ref"
            ],
            "validation_attempt_ref": acceptance_payload["validation_attempt_ref"],
            "candidate_evidence_package_ref": acceptance_payload[
                "candidate_evidence_package_ref"
            ],
            "pair_refs": acceptance_payload["pair_refs"],
            "external_gate_receipt_refs": acceptance_payload[
                "external_gate_receipt_refs"
            ],
        }
        for key, expected_value in expected_from_acceptance.items():
            if payload[key] != expected_value:
                raise ControlError(f"validation_completed 与 Acceptance 的 {key} 不一致")
        if payload["attempt_id"] != attempt_payload["attempt_id"]:
            raise ControlError("validation_completed 与 ValidationAttempt ID 不一致")
        if payload["validation_execution_plan_ref"] != attempt_payload[
            "validation_execution_plan_ref"
        ]:
            raise ControlError("validation_completed 与 ValidationExecutionPlan 不一致")
        if attempt_payload["candidate_ref"] != payload["candidate_ref"]:
            raise ControlError("validation_completed 与 ValidationAttempt Candidate 不一致")
        build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
        self._candidate_build_matches(campaign, candidate_payload, build)
        self.validate_object_graph(payload["candidate_evidence_package_ref"])
        ledger = self._object(
            acceptance_payload["atomic_assertion_ledger_ref"],
            "atomic_assertion_ledger",
        )
        expected_required = {
            item["assertion_id"]
            for item in ledger["assertions"]
            if item["owner_kind"] == "required_rule"
        }
        actual_required: list[str] = []
        for reference in payload["pair_refs"]:
            pair = self._fact(reference, "pair_recorded")
            actual_required.extend(
                item["assertion_id"] for item in pair["payload"]["assertion_results"]
            )
        if set(actual_required) != expected_required or len(actual_required) != len(
            expected_required
        ):
            raise ControlError("validation_completed 未唯一闭合 RequiredRules 原子断言")
        plan = self._object(
            payload["validation_execution_plan_ref"], "validation_execution_plan"
        )
        expected_gate_ids = {item["gate_id"] for item in plan["external_gates"]}
        gate_receipts = [
            self.store.load_receipt(reference)
            for reference in payload["external_gate_receipt_refs"]
        ]
        actual_gate_ids = [item["gate_id"] for item in gate_receipts]
        if set(actual_gate_ids) != expected_gate_ids or len(actual_gate_ids) != len(
            expected_gate_ids
        ):
            raise ControlError("validation_completed 外部门禁未唯一覆盖执行计划")
        if any(item["result"] != "pass" for item in gate_receipts):
            raise ControlError("validation_completed 不得消费失败外部门禁")
        self._require_validation_gate_closure(
            campaign_id=campaign["campaign_id"],
            candidate_id=payload["candidate_id"],
            candidate_ref=payload["candidate_ref"],
            candidate_build_receipt_ref=payload["candidate_build_receipt_ref"],
            validation_attempt_ref=payload["validation_attempt_ref"],
            validation_execution_plan_ref=payload[
                "validation_execution_plan_ref"
            ],
            supplied_refs=payload["external_gate_receipt_refs"],
            plan=plan,
        )
        before = self._fact(
            attempt_payload["production_selector_before_ref"], "selector_observed"
        )
        after = self._fact(
            payload["production_selector_after_ref"], "selector_observed"
        )
        before_snapshot = self._object(
            before["payload"]["catalog_snapshot_ref"], "runtime_catalog_snapshot"
        )
        after_snapshot = self._object(
            after["payload"]["catalog_snapshot_ref"], "runtime_catalog_snapshot"
        )
        for key in ("catalog_digest", "production_active_ref", "production_rollback_ref"):
            if before_snapshot[key] != after_snapshot[key]:
                raise ControlError("VC-5 期间 production Active／Rollback／selector 发生变化")
        # B4-04：VC-5 完成按评估基线计唯一（重开评估基线后新基线可再完成一次）；v1 与 b0 都是 0，判据不变。
        from .receipts import baseline_number, fact_baseline_number

        own = baseline_number(formed["evaluation_baseline"]) if formed is not None else 0
        if any(
            (
                fact["payload"]["attempt_id"] == payload["attempt_id"]
                or fact["payload"]["candidate_id"] == payload["candidate_id"]
            )
            and fact_baseline_number(fact) == own
            for fact in facts_by_kind["validation_completed"]
        ):
            raise ControlError("同一 Candidate／attempt 的 VC-5 完成事实不得覆盖")

    def _gate_candidate_delivery(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        formed: dict[str, Any] | None = None,
        *,
        document: dict[str, Any] | None = None,
        campaign: dict[str, Any] | None = None,
        new: bool = False,
    ) -> None:
        """候选交付阶段事实。

        首轮交付（v1 事实、或 v2 事实 ``recovery_revision`` 为 null）沿用原四阶段判据，v1 Campaign 判定与文案逐字
        不变。恢复段 ``dr<k>`` 的事实（B4-07）另按段预约核对门禁 ①～③、⑤（``_gate_delivery_segment_fact``）；阶段
        「不得覆盖」改为按 (delivery, 段, 阶段) 判定。写入时（``new``）再核 delivery 未 finalize、只有最新一段接受新
        事实，并以「现有内容＋本事实」重算唯一有效链（``_require_delivery_open``）。
        """

        from .receipts import require_acceptance_formed_under

        plan = self._object(payload["delivery_plan_ref"], "candidate_delivery_plan")
        completed = self._fact(payload["validation_completed_ref"], "validation_completed")
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        require_acceptance_formed_under(acceptance, formed, "候选交付阶段")
        number = delivery_recovery_number(payload.get("recovery_revision"))
        expected = {
            "delivery_id": plan["delivery_id"],
            "candidate_id": plan["candidate_id"],
            "candidate_ref": plan["candidate_ref"],
            "candidate_build_receipt_ref": plan["candidate_build_receipt_ref"],
            "validation_completed_ref": plan["validation_completed_ref"],
            "acceptance_ref": plan["acceptance_ref"],
            "host_id": plan["host_id"],
            "architecture": plan["architecture"],
            "compose_sha256": plan["compose_sha256"],
            "environment_sha256": plan["environment_sha256"],
            "configuration_sha256": plan["configuration_sha256"],
            "network_sha256": plan["network_sha256"],
        }
        for key, expected_value in expected.items():
            if payload[key] != expected_value:
                if number:
                    raise ControlError(
                        f"⑤ 交付恢复段 dr{number} 的 {key} 与冻结计划不一致：环境身份变化不能在同一 delivery 内"
                        "恢复，须开新的 delivery_id"
                    )
                raise ControlError(f"候选交付阶段的 {key} 与冻结计划不一致")
        if completed["payload"]["acceptance_ref"] != payload["acceptance_ref"]:
            raise ControlError("候选交付未绑定 VC-5 完成的 Acceptance")
        if acceptance["payload"]["candidate_ref"] != payload["candidate_ref"]:
            raise ControlError("候选交付与 Acceptance Candidate 不一致")
        if candidate["payload"]["candidate_build_receipt_ref"] != payload[
            "candidate_build_receipt_ref"
        ]:
            raise ControlError("候选交付与 CandidateBuildReceipt 不一致")
        stage = payload["stage"]
        expected_image = (
            plan["rollback_image_digest"]
            if stage == "rollback_verified"
            else plan["candidate_image_digest"]
        )
        if payload["image_digest"] != expected_image:
            raise ControlError(f"{stage} 未使用冻结的正确镜像摘要")
        expected_profile = (
            plan["rollback_runtime_profile_sha256"]
            if stage == "rollback_verified"
            else plan["runtime_profile_sha256"]
        )
        if payload["runtime_profile_sha256"] != expected_profile:
            raise ControlError(f"{stage} 运行 Profile 与冻结计划不一致")
        if payload["check_ids"] != plan["stage_checks"][stage]:
            raise ControlError(f"{stage} 未执行冻结的交付检查闭集")
        evidence_payloads = [
            self._object(reference) for reference in payload["evidence_refs"]
        ]
        if payload["result"] == "pass":
            for evidence in evidence_payloads:
                self._require_manifest_pass(
                    evidence, f"CandidateDelivery/{stage}/evidence_refs"
                )
        elif not any(
            self._manifest_has_failure(evidence) for evidence in evidence_payloads
        ):
            raise ControlError(f"{stage} 失败事实缺少显式失败证据")
        # ⑥ 观察窗取冻结计划：恢复段的 stable_observed 同样要观察满整窗，不得缩短。
        if (
            stage == "stable_observed"
            and payload["result"] == "pass"
            and payload["observed_seconds"] < plan["observation_window_seconds"]
        ):
            raise ControlError("stable_observed 未达到冻结观察窗")
        existing = [
            fact
            for fact in facts_by_kind["candidate_delivery_recorded"]
            if fact["payload"]["delivery_id"] == payload["delivery_id"]
        ]
        if any(
            fact["payload"]["stage"] == stage
            and delivery_recovery_number(fact["payload"].get("recovery_revision")) == number
            for fact in existing
        ):
            raise ControlError("候选交付阶段不得覆盖")
        if number:
            self._gate_delivery_segment_fact(payload, number)
        else:
            self._gate_delivery_first_round_previous(payload, stage)
        if new and formed is not None and campaign is not None and document is not None:
            self._require_delivery_open(campaign, payload, document, number)

    def _gate_delivery_first_round_previous(self, payload: dict[str, Any], stage: str) -> None:
        """首轮交付的前序阶段：直接承接前一阶段的 pass，13 个不漂移字段逐字相等（原判据与文案）。"""

        stage_index = CANDIDATE_DELIVERY_STAGES.index(stage)
        if stage_index == 0:
            return
        previous = self._fact(
            payload["previous_stage_ref"], "candidate_delivery_recorded"
        )
        expected_previous_stage = CANDIDATE_DELIVERY_STAGES[stage_index - 1]
        if previous["payload"]["stage"] != expected_previous_stage:
            raise ControlError(f"{stage} 必须直接承接 {expected_previous_stage}")
        if previous["payload"]["result"] != "pass":
            raise ControlError("失败的候选交付阶段不得继续推进")
        for key in CANDIDATE_DELIVERY_IDENTITY_FIELDS:
            if payload[key] != previous["payload"][key]:
                raise ControlError(f"候选交付四阶段的 {key} 发生漂移")

    def _gate_delivery_segment_fact(self, payload: dict[str, Any], number: int) -> None:
        """恢复段 dr<k> 的交付事实（B4-07 门禁 ①②③⑤）。

        ① ``recovery_ref`` 是同一 delivery、同一编号、同一交付计划的段预约，阶段属于 ``stages_to_rerun``；② 段内首个
        重跑阶段的 ``previous_stage_ref`` 等于预约的 ``previous_valid_stage_ref``；③ 其后各阶段衔接本段直接前一阶段
        的 pass；⑤ 与前序事实的 13 个不漂移字段逐字相等。
        """

        recovery = self._object(payload["recovery_ref"], "candidate_delivery_recovery")
        if (
            recovery["delivery_id"] != payload["delivery_id"]
            or delivery_recovery_number(recovery["recovery_revision"]) != number
            or recovery["delivery_plan_ref"] != payload["delivery_plan_ref"]
        ):
            raise ControlError(f"① dr{number} 的 recovery_ref 与 delivery、段编号或交付计划不一致")
        stage = payload["stage"]
        if stage not in recovery["stages_to_rerun"]:
            raise ControlError(f"① dr{number} 的事实阶段 {stage} 不在段预约的 stages_to_rerun 内")
        index = CANDIDATE_DELIVERY_STAGES.index(stage)
        if stage == recovery["rerun_from_stage"]:
            if payload["previous_stage_ref"] != recovery["previous_valid_stage_ref"]:
                raise ControlError(
                    f"② dr{number} 首个重跑阶段 {stage} 的 previous_stage_ref 必须等于段预约的 previous_valid_stage_ref"
                )
            if payload["previous_stage_ref"] is None:
                return
            previous = self._fact(payload["previous_stage_ref"], "candidate_delivery_recorded")
        else:
            previous = self._fact(payload["previous_stage_ref"], "candidate_delivery_recorded")
            if delivery_recovery_number(previous["payload"].get("recovery_revision")) != number:
                raise ControlError(f"③ dr{number} 的 {stage} 必须衔接本段直接前一阶段的 pass")
        expected_previous_stage = CANDIDATE_DELIVERY_STAGES[index - 1]
        if previous["payload"]["stage"] != expected_previous_stage:
            raise ControlError(f"{stage} 必须直接承接 {expected_previous_stage}")
        if previous["payload"]["result"] != "pass":
            raise ControlError("失败的候选交付阶段不得继续推进")
        for key in CANDIDATE_DELIVERY_IDENTITY_FIELDS:
            if payload[key] != previous["payload"][key]:
                raise ControlError(
                    f"⑤ 交付恢复段 dr{number} 的 {key} 发生漂移：环境身份变化须开新的 delivery_id"
                )

    def _require_delivery_open(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        document: dict[str, Any],
        number: int,
    ) -> None:
        """写入时（B4-07）：delivery 未 finalize；只有最新交付段接受新事实（⑦）；「现有内容＋本事实」仍是一条
        合法的唯一有效链（段不交错、逐阶段推进、失败即收口）。"""

        from .receipts import (
            analyze_delivery_chain,
            delivery_facts,
            delivery_receipts,
            delivery_recoveries,
            delivery_segment_label,
            delivery_stage_invalidations,
        )

        campaign_id = campaign["campaign_id"]
        delivery_id = payload["delivery_id"]
        finalized = delivery_receipts(self.store, campaign_id, payload["delivery_plan_ref"])
        if finalized:
            raise ControlError(
                f"候选交付 {delivery_id} 已 finalize（终态收据 {finalized[0][0]['sha256']}）：不得再追加交付事实"
            )
        recoveries = delivery_recoveries(self.store, campaign_id, delivery_id)
        latest = max(recoveries) if recoveries else 0
        if number != latest:
            raise ControlError(
                f"⑦ 只有最新交付段（{delivery_segment_label(latest)}）接受新事实："
                f"{delivery_segment_label(number)}已被后续恢复段取代"
            )
        pending = dict(document)
        pending["_sha256"] = sha256_bytes(canonical_json_bytes(document))
        analyze_delivery_chain(
            self.store,
            delivery_id,
            [*delivery_facts(self.store, campaign_id, delivery_id), pending],
            recoveries,
            delivery_stage_invalidations(self.store, campaign_id, delivery_id),
        )

    def _gate_candidate_revision(
        self,
        campaign: dict[str, Any],
        document: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        coordinates: dict[str, Any] | None,
    ) -> None:
        """候选 revision 事实（B1-12 立形；B3-05 补作废对象门禁）。

        只看同一 validation 链里序号更小的事实，写入与重放判据一致：首条只能显式确认 r1（必须先于任何候选级
        事实）或以 r2 取代隐含 r1，都不声明 supersedes；其后逐一递增并 supersedes 上一条；本事实的形成期 revision
        必须是写入前的当前 revision；新 revision（r≥2）必须对应新的 Candidate ID。

        B3-05：被取代的 revision 若已作废（``candidate_invalidation``），新 revision 必须以 ``invalidation_ref``
        引用该作废对象（作废对象的 ``revision_ref`` 等于 ``supersedes``，新候选不得是被作废的候选）；未作废时
        ``invalidation_ref`` 为 null（B1-12 的直接取代）。重放时「被取代 revision 有作废对象而新 revision 不引用」
        同样拒绝：作废只能针对当前 revision，所以作废对象必然先于取代它的 revision 事实。
        """

        if coordinates is None:
            raise ControlError("v1 Campaign 不支持候选 revision 事实")
        payload = document["payload"]
        sequence = document["sequence"]
        earlier = sorted(
            (
                fact
                for fact in facts_by_kind["candidate_revision_recorded"]
                if fact["sequence"] < sequence
            ),
            key=lambda fact: fact["sequence"],
        )
        previous = earlier[-1] if earlier else None
        current_revision = previous["payload"]["revision"] if previous is not None else 1
        if coordinates["candidate_revision"] != current_revision:
            raise ControlError("候选 revision 事实的形成期 revision 不是写入前的当前 revision")
        if previous is None:
            if payload["supersedes"] is not None or payload["revision"] not in (1, 2):
                raise ControlError(
                    "首条候选 revision 事实只能显式确认 r1 或以 r2 取代隐含 r1，且不得声明 supersedes"
                )
            if payload["revision"] == 1 and any(
                fact["sequence"] < sequence
                for kind in CANDIDATE_SCOPED_FACT_KINDS
                for fact in facts_by_kind[kind]
                if fact["dimension"] == document["dimension"]
            ):
                raise ControlError("显式确认 r1 必须先于任何候选级事实")
        elif payload["revision"] != current_revision + 1 or payload[
            "supersedes"
        ] != self.store.fact_ref(previous):
            raise ControlError("候选 revision 必须逐一递增并 supersedes 上一条 revision 事实")
        if payload["revision"] >= 2:
            used = {fact["payload"]["candidate_id"] for fact in earlier} | {
                fact["payload"]["candidate_id"]
                for fact in facts_by_kind["candidate_frozen"]
                if fact["sequence"] < sequence
            }
            if payload["candidate_id"] in used:
                raise ControlError("新候选 revision 必须对应新的 Candidate ID")
        if any(
            fact["payload"]["revision"] == payload["revision"]
            for fact in facts_by_kind["candidate_revision_recorded"]
        ):
            raise ControlError("同一候选 revision 不得重复登记")
        if payload["revision"] >= 2:
            self._require_revision_supersedes_invalidation(campaign, payload, current_revision)

    def _require_revision_supersedes_invalidation(
        self, campaign: dict[str, Any], payload: dict[str, Any], superseded: int
    ) -> None:
        """B3-05：新 revision 与被取代 revision 的作废对象一一对应（见 ``_gate_candidate_revision``）。"""

        from .receipts import revision_invalidation

        found = revision_invalidation(self.store, campaign, superseded)
        reference = payload["invalidation_ref"]
        if reference is None:
            if found is not None:
                raise ControlError(
                    f"被取代的 r{superseded} 已作废（作废对象 {found[0]['sha256']}）："
                    "新 revision 必须以 invalidation_ref 引用该作废对象"
                )
            return
        if found is None or reference != found[0]:
            raise ControlError(f"invalidation_ref 必须引用被取代 revision r{superseded} 的作废对象")
        invalidation = found[1]
        if invalidation["revision_ref"] != payload["supersedes"]:
            raise ControlError("作废对象的 revision_ref 与新 revision 的 supersedes 不一致")
        if invalidation["candidate_id"] == payload["candidate_id"]:
            raise ControlError("新 revision 的候选不得是被作废的候选")

    def _gate_evaluation_baseline(
        self,
        campaign: dict[str, Any],
        document: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        coordinates: dict[str, Any] | None,
    ) -> None:
        """评估基线事实（设计 B4-04）：在当前候选 revision 内为已进入 VC-5 的候选开新的评估基线 b<K>。

        只看序号更小的事实，写入与重放判据一致：候选是本 revision 内冻结的严格 v2 Candidate；本事实的形成期基线是
        写入前的当前基线（同一 revision 内上一条基线事实给出的值，首条为 b0），``supersedes`` 引用那条事实；K 大于
        当前基线编号（未 COMMIT 而放弃的编号不复用，不要求连续）；同一 revision 内的基线属于同一候选；COMMIT 摘要
        不复用；候选已有 ValidationAttempt（VC-5 已开始）；候选没有形成于更早基线的交付事实（VC-6 开始后不得再开
        基线——开基线之后的写入都形成于新基线，所以重放时出现即说明顺序违规）。
        """

        from .receipts import fact_baseline_number

        if coordinates is None:
            raise ControlError("v1 Campaign 不支持评估基线事实")
        payload = document["payload"]
        sequence = document["sequence"]
        formed = document["formed_under"]
        revision = formed["candidate_revision"]
        label = "评估基线的候选跨 Campaign"
        self._precheck_fact_ref(campaign, payload["candidate_ref"], label)
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        if candidate["campaign_id"] != campaign["campaign_id"]:
            self._require_campaign_owned(
                campaign, payload["candidate_ref"], candidate["campaign_id"], label
            )
        candidate_payload = candidate["payload"]
        if candidate_payload.get("schema_version") is None:
            raise ControlError("评估基线只适用于严格 v2 Candidate")
        if candidate_payload["candidate_id"] != payload["candidate_id"]:
            raise ControlError("评估基线的 candidate_ref 与 candidate_id 不一致")
        if candidate["formed_under"]["candidate_revision"] != revision:
            raise ControlError(f"评估基线的候选不是在形成期候选 revision r{revision} 内冻结的")
        earlier = [
            fact
            for fact in facts_by_kind["evaluation_baseline_recorded"]
            if fact["sequence"] < sequence and fact["formed_under"]["candidate_revision"] == revision
        ]
        previous = max(earlier, key=lambda fact: fact["sequence"]) if earlier else None
        expected_formed = (
            None
            if previous is None
            else {
                "baseline": previous["payload"]["baseline"],
                "baseline_commit_sha256": previous["payload"]["baseline_commit_sha256"],
            }
        )
        if formed["evaluation_baseline"] != expected_formed:
            raise ControlError("评估基线事实的形成期基线不是写入前的当前基线")
        if payload["supersedes"] != (None if previous is None else self.store.fact_ref(previous)):
            raise ControlError("评估基线必须 supersedes 同一候选 revision 内的上一条基线事实（首条为 null）")
        current = 0 if previous is None else int(previous["payload"]["baseline"])
        if payload["baseline"] <= current:
            raise ControlError(f"新评估基线编号必须大于当前基线 b{current}")
        if previous is not None and previous["payload"]["candidate_id"] != payload["candidate_id"]:
            raise ControlError("同一候选 revision 内的评估基线必须属于同一候选")
        if any(
            fact["payload"]["baseline_commit_sha256"] == payload["baseline_commit_sha256"]
            for fact in facts_by_kind["evaluation_baseline_recorded"]
        ):
            raise ControlError("评估基线 COMMIT 摘要不得复用")
        if not any(
            fact["sequence"] < sequence and fact["payload"]["candidate_id"] == payload["candidate_id"]
            for fact in facts_by_kind["validation_attempt_created"]
        ):
            raise ControlError("候选尚未进入 VC-5（没有 ValidationAttempt），没有可重评的评估")
        if any(
            fact["payload"]["candidate_id"] == payload["candidate_id"]
            and fact["formed_under"]["candidate_revision"] == revision
            and fact_baseline_number(fact) < payload["baseline"]
            for fact in facts_by_kind["candidate_delivery_recorded"]
        ):
            raise ControlError(
                f"候选 {payload['candidate_id']} 已开始 VC-6 交付：交付开始后不得再开评估基线"
            )

    def _gate_selector(
        self,
        kind: str,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Runtime Selector 事实（唯一性补齐，Claude 升级 B1-13）。

        ``selector_observed`` 是只读观察：同一快照前后观察两次是合法证据（VC-5 的 before／after），不判重复；
        它的幂等由幂等键承担——动作体重试带同一幂等键时 Store 返回原事实、不新增，同键异内容被拒。
        ``selector_activated`` 是一次性切换：同一 Deployment（``deployment_ref`` 指向的 active 阶段）只能激活一次。
        """

        snapshot = self._object(payload["catalog_snapshot_ref"], "runtime_catalog_snapshot")
        if kind == "selector_activated":
            deployment = self._fact(payload["deployment_ref"], "active")
            candidate = self._candidate_by_id(deployment["payload"]["candidate_id"], facts_by_kind)
            if snapshot["production_active_ref"] != candidate["payload"]["release_artifact_ref"]:
                raise ControlError("selector 激活没有指向已晋升 Candidate Release")
            if any(
                fact["payload"]["deployment_ref"] == payload["deployment_ref"]
                for fact in facts_by_kind["selector_activated"]
            ):
                raise ControlError("同一 Deployment 的 selector_activated 不得重复")

    def _gate_release_promoted(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        formed: dict[str, Any] | None = None,
    ) -> None:
        from .receipts import require_acceptance_formed_under

        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        if acceptance["payload"]["result"] != "accepted":
            raise ControlError("validation-only Candidate 不得晋升生产")
        require_acceptance_formed_under(acceptance, formed, "晋升")
        if acceptance["payload"].get("schema_version") is not None:
            completed = [
                fact
                for fact in facts_by_kind["validation_completed"]
                if fact["payload"]["acceptance_ref"] == payload["acceptance_ref"]
            ]
            if len(completed) != 1:
                raise ControlError("严格 Acceptance 未完成 VC-5，不得晋升生产")
            self._require_candidate_delivery_receipt(
                payload["candidate_ref"], payload["acceptance_ref"]
            )
        if acceptance["payload"]["candidate_ref"] != payload["candidate_ref"]:
            raise ControlError("晋升引用的 Candidate 与 Acceptance 不一致")
        if candidate["payload"]["release_artifact_ref"] != payload["release_artifact_ref"]:
            raise ControlError("晋升 Release 与 Candidate 不一致")
        if facts_by_kind["release_promoted"]:
            candidate_ids = {
                self._fact(fact["payload"]["candidate_ref"], "candidate_frozen")["payload"][
                    "candidate_id"
                ]
                for fact in facts_by_kind["release_promoted"]
            }
            if candidate["payload"]["candidate_id"] in candidate_ids:
                raise ControlError("同一 Candidate 不得重复晋升")

    def _gate_deployment_stage(
        self,
        stage: str,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
        formed: dict[str, Any] | None = None,
    ) -> None:
        from .receipts import require_acceptance_formed_under

        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        require_acceptance_formed_under(acceptance, formed, f"Deployment {stage}")
        promotion_receipt = self.store.load_receipt(payload["promotion_receipt_ref"])
        if acceptance["payload"]["candidate_id"] != payload["candidate_id"]:
            raise ControlError("Deployment 与 Acceptance Candidate 不一致")
        if promotion_receipt["candidate_ref"] != acceptance["payload"]["candidate_ref"]:
            raise ControlError("Deployment 的 PromotionReceipt 与 Acceptance 不一致")
        candidate = self._fact(acceptance["payload"]["candidate_ref"], "candidate_frozen")
        active_envelope = self._object(
            payload["active_support_envelope_ref"], "active_support_envelope"
        )
        rollback_envelope = self._object(
            payload["rollback_operational_envelope_ref"],
            "rollback_operational_envelope",
        )
        runtime_snapshot = self._object(
            payload["runtime_catalog_snapshot_ref"], "runtime_catalog_snapshot"
        )
        candidate_release_ref = candidate["payload"]["release_artifact_ref"]
        if active_envelope["release_artifact_ref"] != candidate_release_ref:
            raise ControlError("Deployment ActiveSupportEnvelope 未指向 Candidate Release")
        expected_image = (
            rollback_envelope["operational_bindings"]["image_digest"]
            if stage == "rollback_verified"
            else candidate["payload"]["image_digest"]
        )
        if payload["image_digest"] != expected_image:
            raise ControlError(f"Deployment {stage} 的实际镜像与冻结身份不一致")
        if stage in {"active", "restored_active"}:
            if runtime_snapshot["production_active_ref"] != candidate_release_ref:
                raise ControlError(f"Deployment {stage} RuntimeCatalogSnapshot 未指向 Candidate Release")
        if stage == "rollback_verified":
            frozen_rollback_snapshot_ref = rollback_envelope["operational_bindings"][
                "selector_snapshot_ref"
            ]
            if payload["runtime_catalog_snapshot_ref"] != frozen_rollback_snapshot_ref:
                raise ControlError("rollback_verified 未恢复冻结的 RuntimeCatalogSnapshot")
        self.validate_object_graph(payload["active_support_envelope_ref"])
        self.validate_object_graph(payload["rollback_operational_envelope_ref"])
        self.validate_object_graph(payload["deployment_traffic_envelope_ref"])
        existing = [
            fact
            for current_stage in DEPLOYMENT_STAGES
            for fact in facts_by_kind[current_stage]
            if fact["payload"]["deployment_id"] == payload["deployment_id"]
        ]
        if any(fact["fact_kind"] == stage for fact in existing):
            raise ControlError("Deployment 阶段不得覆盖")
        stage_index = DEPLOYMENT_STAGES.index(stage)
        if stage_index == 0:
            return
        previous_stage = DEPLOYMENT_STAGES[stage_index - 1]
        previous = self._fact(payload["previous_stage_ref"], previous_stage)
        previous_payload = previous["payload"]
        for key in (
            "deployment_id",
            "candidate_id",
            "acceptance_ref",
            "promotion_receipt_ref",
            "active_support_envelope_ref",
            "rollback_operational_envelope_ref",
            "deployment_traffic_envelope_ref",
        ):
            if payload[key] != previous_payload[key]:
                raise ControlError(f"Deployment 五阶段的 {key} 发生漂移")
        if stage == "restored_active":
            active_matches = [
                fact
                for fact in facts_by_kind["active"]
                if fact["payload"]["deployment_id"] == payload["deployment_id"]
            ]
            if len(active_matches) != 1:
                raise ControlError("restored_active 无法唯一关联 active 阶段")
            active_payload = active_matches[0]["payload"]
            if (
                payload["image_digest"] != active_payload["image_digest"]
                or payload["runtime_catalog_snapshot_ref"]
                != active_payload["runtime_catalog_snapshot_ref"]
            ):
                raise ControlError("restored_active 未闭合 active 的镜像与运行目录身份")

    def _gate_inventory_current(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        deployment = self._fact(payload["deployment_ref"], "restored_active")
        acceptance = self._fact(deployment["payload"]["acceptance_ref"], "acceptance_recorded")
        approval = self._fact(acceptance["payload"]["profile_approval_ref"], "profile_approved")
        ingress = self._object(
            payload["production_ingress_inventory_ref"], "production_ingress_inventory"
        )
        egress = self._object(
            payload["egress_disposition_inventory_ref"], "egress_disposition_inventory"
        )
        ingress_target = {
            item["logical_ingress_id"]: item["target_disposition"]
            for item in approval["payload"]["ingress_target_dispositions"]
        }
        ingress_current = {
            item["logical_ingress_id"]: item["current_disposition"] for item in ingress["entries"]
        }
        if ingress_current != ingress_target:
            raise ControlError("Deployment 后入口 current_disposition 未精确实现批准目标")
        egress_target = {
            item["egress_id"]: item["target_disposition"]
            for item in approval["payload"]["egress_target_dispositions"]
        }
        egress_current = {
            item["egress_id"]: item["current_disposition"] for item in egress["entries"]
        }
        if egress_current != egress_target:
            raise ControlError("Deployment 后出站 current_disposition 未精确实现批准目标")
        stale_enforcement = sorted(
            item["egress_id"]
            for item in egress["entries"]
            if item["current_guard_state"] != "enforced"
            and not (
                item["current_disposition"] == "denied"
                and item["current_guard_state"] == "source_absent"
            )
        )
        if stale_enforcement:
            raise ControlError(
                "Deployment 后出站仍未进入 enforced：" + ", ".join(stale_enforcement)
            )
        if facts_by_kind["inventory_current_appended"]:
            raise ControlError("同一 Campaign 的实际 current Inventory 不得覆盖")

    def replay_campaign(
        self, campaign: dict[str, Any], facts: list[dict[str, Any]]
    ) -> None:
        """在完整事实集上复核引用、唯一性和最终状态，不依赖可变状态文件。

        v2 Campaign 的每条事实按自身 ``formed_under`` 重跑门禁（形成期一致性，与当前受管代码无关），并另核：
        工具演进登记链完整；每条事实的坐标（含评估基线，B4-04）在本 Campaign 内可达；同一维度链内坐标只增不减；
        引用的事实不晚于引用者；候选 revision 事实构成严格的取代链；每个 delivery 沿唯一有效链重放（B4-07）。
        """

        context: Any = None
        if campaign_is_v2(campaign):
            from .receipts import FormedContext

            context = FormedContext(self.store, campaign)
        crossing = "事实链重放发现跨 Campaign 引用"
        facts_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            validate_fact_document({key: value for key, value in fact.items() if key != "_sha256"})
            for reference in self._all_fact_refs(fact["payload"]):
                # 消费端核验（B1-14）：v2 在解引用前核验跨 Campaign 引用（事实永远不在官方证据白名单内，因而一律拒绝）；
                # v1 保持原顺序与原文案。
                if context is not None and reference["campaign_id"] != campaign["campaign_id"]:
                    self._require_campaign_owned(campaign, reference, reference["campaign_id"], crossing)
                    continue
                referenced = self.store.load_fact(reference)
                if referenced["campaign_id"] != campaign["campaign_id"]:
                    self._require_campaign_owned(
                        campaign, reference, referenced["campaign_id"], crossing
                    )
            facts_by_kind[fact["fact_kind"]].append(fact)
        if context is not None:
            self._replay_predecessor_imports(campaign)
            self._replay_formed_under(facts, context)
        for fact in facts:
            without_current: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for kind, items in facts_by_kind.items():
                without_current[kind] = [item for item in items if item is not fact]
            self._validate_fact_semantics(campaign, fact, without_current, context)
        if len(facts_by_kind["discovery_recorded"]) > 1:
            raise ControlError("Campaign 存在多个 DiscoveryFact")
        if len(facts_by_kind["evidence_recorded"]) > 1:
            raise ControlError("Campaign 存在多个 EvidencePackage")
        if len(facts_by_kind["rule_classification_recorded"]) > 1:
            raise ControlError("Campaign 存在多个规则分类事实")
        if len(facts_by_kind["evidence_approved"]) > 1:
            raise ControlError("Campaign 存在多个 EvidenceApprovalFact")
        idempotency_keys = [
            fact["idempotency_key"] for fact in facts if fact.get("idempotency_key") is not None
        ]
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ControlError("Campaign 存在重复的事实幂等键：同一逻辑写入只能落一条事实")
        candidate_ids = [fact["payload"]["candidate_id"] for fact in facts_by_kind["candidate_frozen"]]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ControlError("Campaign 存在重复 Candidate ID")
        # B4-04：PAIR 唯一性是 (candidate, pair_id, 评估基线)；v1 与 b0 的基线编号都是 0，判据与原来相同。
        pair_keys = [
            (
                fact["payload"]["candidate_id"],
                fact["payload"]["pair_id"],
                payload_evaluation_baseline(fact["payload"]),
            )
            for fact in facts_by_kind["pair_recorded"]
        ]
        if len(pair_keys) != len(set(pair_keys)):
            raise ControlError("Campaign 存在重复 PAIR")
        for deployment_id in {
            fact["payload"]["deployment_id"]
            for stage in DEPLOYMENT_STAGES
            for fact in facts_by_kind[stage]
        }:
            present = [
                stage
                for stage in DEPLOYMENT_STAGES
                if any(
                    fact["payload"]["deployment_id"] == deployment_id
                    for fact in facts_by_kind[stage]
                )
            ]
            expected = list(DEPLOYMENT_STAGES[: len(present)])
            if present != expected:
                raise ControlError(f"Deployment 阶段链存在越权或缺口：{deployment_id}/{present}")
        for delivery_id in {
            fact["payload"]["delivery_id"]
            for fact in facts_by_kind["candidate_delivery_recorded"]
        }:
            present = [
                stage
                for stage in CANDIDATE_DELIVERY_STAGES
                if any(
                    fact["payload"]["delivery_id"] == delivery_id
                    and fact["payload"]["stage"] == stage
                    for fact in facts_by_kind["candidate_delivery_recorded"]
                )
            ]
            expected = list(CANDIDATE_DELIVERY_STAGES[: len(present)])
            if present != expected:
                raise ControlError(f"候选交付阶段链存在越权或缺口：{delivery_id}/{present}")
        if context is not None:
            self._replay_delivery_chains(campaign, facts_by_kind)
        validation_gate_keys: list[tuple[str, str]] = []
        for reference in self.store.list_receipt_refs("validation_gate"):
            receipt = self.store.load_receipt(reference)
            if receipt["campaign_id"] == campaign["campaign_id"]:
                validation_gate_keys.append(
                    (
                        receipt["validation_attempt_ref"]["sha256"],
                        receipt["gate_id"],
                    )
                )
        if len(validation_gate_keys) != len(set(validation_gate_keys)):
            raise ControlError("同一 ValidationAttempt 存在重复外部门禁收据")
        delivery_receipt_keys: list[tuple[str, str]] = []
        for reference in self.store.list_receipt_refs("candidate_delivery"):
            receipt = self.store.load_receipt(reference)
            if receipt["campaign_id"] == campaign["campaign_id"]:
                delivery_receipt_keys.append(
                    (
                        receipt["candidate_ref"]["sha256"],
                        receipt["acceptance_ref"]["sha256"],
                    )
                )
        if len(delivery_receipt_keys) != len(set(delivery_receipt_keys)):
            raise ControlError("同一 Candidate／Acceptance 存在重复候选交付收据")

    def _replay_predecessor_imports(self, campaign: dict[str, Any]) -> None:
        """v2：本 Campaign 的每份前序导入登记至多一份导入收据（finalize 幂等、按登记串行的结果）。

        收据自身的重建在 Store 重放的收据环节完成；这里只核「一份登记一份收据」，多份即 Store 已不一致。
        """

        from .receipts import predecessor_import_receipts, predecessor_imports

        for import_ref, _payload in predecessor_imports(self.store, campaign["campaign_id"]):
            if len(predecessor_import_receipts(self.store, import_ref)) > 1:
                raise ControlError(f"同一前序导入登记存在多份导入收据：{import_ref['sha256']}")

    def _replay_delivery_chains(
        self, campaign: dict[str, Any], facts_by_kind: dict[str, list[dict[str, Any]]]
    ) -> None:
        """v2：每个 delivery 沿唯一有效链重放（B4-07）——dr0 前缀＋每个 dr<k> 以预约的 previous_valid_stage_ref
        接续并覆盖到末尾；段不交错、锚点与失效对象一致、至多一份待消费的阶段失效。还没有事实、只有段预约或阶段
        失效的 delivery 同样覆盖。"""

        from .receipts import (
            analyze_delivery_chain,
            campaign_delivery_object_ids,
            delivery_recoveries,
            delivery_stage_invalidations,
        )

        campaign_id = campaign["campaign_id"]
        by_delivery: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in facts_by_kind["candidate_delivery_recorded"]:
            by_delivery[fact["payload"]["delivery_id"]].append(fact)
        for delivery_id in sorted(set(by_delivery) | campaign_delivery_object_ids(self.store, campaign_id)):
            analyze_delivery_chain(
                self.store,
                delivery_id,
                by_delivery.get(delivery_id, []),
                delivery_recoveries(self.store, campaign_id, delivery_id),
                delivery_stage_invalidations(self.store, campaign_id, delivery_id),
            )

    def _replay_formed_under(self, facts: list[dict[str, Any]], context: Any) -> None:
        """v2 形成期坐标的整体一致性（逐条门禁之外）。

        同一维度链内：工具演进序号与候选 revision 各自只增不减；同一 revision 内评估基线编号只增不减（B4-04），
        换 revision 后回到 b0。
        """

        last_by_dimension: dict[str, tuple[int, int, int]] = {}
        for fact in facts:
            coordinates = context.coordinates(fact)
            key = (
                coordinates["tool_index"],
                coordinates["candidate_revision"],
                coordinates["baseline_number"],
            )
            previous = last_by_dimension.get(fact["dimension"])
            if previous is not None and (
                key[0] < previous[0]
                or key[1] < previous[1]
                or (key[1] == previous[1] and key[2] < previous[2])
            ):
                raise ControlError(
                    f"同一维度链内形成期坐标回退：{fact['dimension']}#{fact['sequence']}"
                )
            last_by_dimension[fact["dimension"]] = key
            context.require_causality(
                self.store,
                coordinates,
                iter_fact_refs(fact["payload"]),
                f"{fact['fact_kind']}#{fact['sequence']}",
            )
        revisions = context.revision_facts
        for position, fact in enumerate(revisions):
            expected = None if position == 0 else self.store.fact_ref(revisions[position - 1])
            if fact["payload"]["supersedes"] != expected:
                raise ControlError("候选 revision 事实没有构成严格的取代链")

    @staticmethod
    def _all_fact_refs(value: Any) -> list[dict[str, Any]]:
        return list(iter_fact_refs(value))

    def status(self, campaign_id: str) -> dict[str, Any]:
        """由正交事实推导 Campaign 检查点。

        v2 的交付判定只认当前候选 revision 与当前评估基线下形成的候选级事实与收据：旧 revision 的 PAIR、
        Acceptance、VC-5 完成与交付收据重放仍通过（形成期一致），但不计入当前检查点。评估基线只影响评估层事实
        （PAIR、Acceptance、VC-5 完成与其后的交付、晋升、部署，B4-04）：候选冻结、场景与 attempt 属于采集层，换基线
        后仍算当前候选的事实，只看 revision。工具演进不改变判定——登记过的演进下形成的结果仍是当前有效结果。
        v1 推导逐字不变。
        """

        campaign = self.store.load_campaign(campaign_id)
        facts = self.store.list_facts(campaign_id)
        self.replay_campaign(campaign, facts)
        from .receipts import (
            current_candidate_revision,
            current_evaluation_baseline,
            replay_receipt,
            revision_invalidation,
            tool_evolutions,
        )

        v2 = campaign_is_v2(campaign)
        current_revision: int = 1
        current_candidate: str | None = None
        current_baseline: dict[str, Any] | None = None
        if v2:
            current_revision, current_candidate = current_candidate_revision(self.store, campaign)
            current_baseline = current_evaluation_baseline(self.store, campaign)

        def is_current(document: dict[str, Any], *, baseline_scoped: bool = True) -> bool:
            if not v2:
                return True
            formed = document["formed_under"]
            if formed["candidate_revision"] != current_revision:
                return False
            return not baseline_scoped or formed["evaluation_baseline"] == current_baseline

        considered = [
            fact
            for fact in facts
            if fact["fact_kind"] not in CANDIDATE_SCOPED_FACT_KINDS
            or is_current(fact, baseline_scoped=fact["fact_kind"] in BASELINE_SCOPED_FACT_KINDS)
        ]
        kinds = {fact["fact_kind"] for fact in considered}

        delivery_receipts: list[dict[str, Any]] = []
        delivery_verified = False
        for reference in self.store.list_receipt_refs("candidate_delivery"):
            receipt = self.store.load_receipt(reference)
            if receipt["campaign_id"] != campaign_id:
                continue
            delivery_receipts.append(reference)
            if not is_current(receipt):
                continue
            try:
                replay_receipt(self.store, reference)
            except ControlError:
                continue
            delivery_verified = True
        if "restored_active" in kinds:
            checkpoint = "restored_active"
        elif "active" in kinds:
            checkpoint = "active"
        elif "accepted_not_activated" in kinds:
            checkpoint = "accepted_not_activated"
        elif delivery_verified:
            checkpoint = "ready_for_operator_release"
        elif "validation_completed" in kinds:
            completed = next(
                fact
                for fact in reversed(considered)
                if fact["fact_kind"] == "validation_completed"
            )
            acceptance = self._fact(
                completed["payload"]["acceptance_ref"], "acceptance_recorded"
            )
            checkpoint = (
                "ready"
                if acceptance["payload"]["result"] == "accepted"
                else "validation_only"
            )
        elif "acceptance_recorded" in kinds:
            strict_pending = any(
                fact["payload"].get("schema_version") is not None
                for fact in considered
                if fact["fact_kind"] == "acceptance_recorded"
            )
            if strict_pending:
                checkpoint = "validation_pending"
            else:
                accepted = any(
                    fact["payload"]["result"] == "accepted"
                    for fact in considered
                    if fact["fact_kind"] == "acceptance_recorded"
                )
                checkpoint = "ready" if accepted else "validation_only"
        elif "candidate_frozen" in kinds:
            checkpoint = "candidate_sealed"
        elif "profile_approved" in kinds:
            checkpoint = "profile_approved"
        elif "evidence_approved" in kinds:
            checkpoint = "official_sealed"
        elif "evidence_recorded" in kinds:
            checkpoint = "evidence_recorded"
        elif "discovery_recorded" in kinds:
            checkpoint = "discovered"
        else:
            checkpoint = "campaign_created"
        activation_verified = False
        activation_receipts = []
        if "restored_active" in kinds:
            for reference in self.store.list_receipt_refs("activation"):
                receipt = self.store.load_receipt(reference)
                if receipt["campaign_id"] != campaign_id:
                    continue
                activation_receipts.append(reference)
                if not is_current(receipt):
                    continue
                try:
                    replay_receipt(self.store, reference)
                except ControlError:
                    continue
                activation_verified = True
        if "active" in kinds and not activation_verified:
            production_state = "production_unverified"
        elif activation_verified:
            production_state = "verified_active"
        else:
            production_state = "not_activated"
        result = {
            "schema_version": "official-client-control-status/v1",
            "campaign_id": campaign_id,
            "checkpoint": checkpoint,
            "fact_counts": {
                dimension: len(self.store.list_facts(campaign_id, dimension))
                for dimension in FACT_DIMENSION.values()
            },
            "activation_receipt_count": len(activation_receipts),
            "candidate_delivery_receipt_count": len(delivery_receipts),
            "delivery_state": (
                "ready_for_operator_release"
                if delivery_verified
                else "not_ready_for_operator_release"
            ),
            "production_state": production_state,
            "production_active_proven": activation_verified,
        }
        if v2:
            # status/v2 多给出判定所依据的当前坐标：当前候选 revision 与候选、当前评估基线、有效工具身份序号；
            # B3-05 起另给当前 revision 的作废对象（非 null 即 revision_required：候选级写入被拒，等待新 revision）。
            result["schema_version"] = "official-client-control-status/v2"
            result["candidate_revision"] = current_revision
            result["current_candidate_id"] = current_candidate
            result["evaluation_baseline"] = current_baseline
            result["tool_identity_index"] = len(tool_evolutions(self.store, campaign))
            invalidation = revision_invalidation(self.store, campaign, current_revision)
            result["candidate_revision_invalidation"] = (
                None if invalidation is None else invalidation[0]
            )
        return result
