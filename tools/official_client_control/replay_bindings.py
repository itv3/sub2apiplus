"""通用 Store 重放的客户端归属索引；只使用本次完整重放已经核验的原件。

这个索引不签发权限，也不从 Persona、路径或构建参数推测客户端。只有
Campaign v2 的冻结 client 字段与完整对象／事实／收据引用图能授予 Claude
两个已登记父域的读集投影。未知来源、孤立消费者、跨客户端及前序导入
来源都保守保留原严格扫描；正式导入授权过滤仍由 Store 原函数决定。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from .canonical import canonical_sha256
from .contracts import campaign_is_v2, iter_fact_refs, iter_object_refs, iter_receipt_refs

REPLAY_PROJECTION_RESULT_SCHEMA = "official-client-control-replay-result/v2"

# 这些完整对象合同把 payload.campaign_id 定义为所属 Campaign；自由 JSON
# 元数据里的同名字段没有这项权利。门禁已在建索引之前核对完整对象语义。
CAMPAIGN_OWNED_OBJECTS = frozenset({
    "rule_migration_ledger", "atomic_assertion_ledger", "validation_execution_plan",
    "candidate_evidence_package", "candidate_delivery_plan", "private_archive_manifest",
    "candidate_delivery_package", "tool_evolution_registration", "predecessor_import",
    "candidate_invalidation", "candidate_delivery_stage_invalidation", "candidate_delivery_recovery",
})


def _references(document: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield from iter_object_refs(document)
    yield from iter_fact_refs(document)
    yield from iter_receipt_refs(document)


class ReplayBindingOwnership:
    """一次完整重放的有界引用索引，未知来源沿真实引用向被消费原件传播。

    调用方必须先完成原 Store 对象图、Campaign／事实链和收据重建的全部
    校验。这里接收同一轮的完整父文档与原引用；不接受报告自报归属。
    """

    def __init__(
        self,
        campaigns: list[dict[str, Any]],
        objects: list[tuple[dict[str, Any], dict[str, Any]]],
        facts: list[tuple[dict[str, Any], dict[str, Any]]],
        receipts: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> None:
        self._campaigns = {row["campaign_id"]: row for row in campaigns}
        nodes = {canonical_sha256(reference): document
                 for reference, document in [*objects, *facts, *receipts]}
        owners: dict[str, set[str | None]] = {key: set() for key in nodes}
        edges = {key: {canonical_sha256(reference) for reference in _references(document)}
                 for key, document in nodes.items()}

        def declared_owner(campaign_id: str) -> str | None:
            return campaign_id if campaign_id in self._campaigns else None

        for campaign in campaigns:
            for reference in _references(campaign):
                key = canonical_sha256(reference)
                if key in owners:
                    owners[key].add(campaign["campaign_id"])
        for reference, document in [*facts, *receipts]:
            owners[canonical_sha256(reference)].add(declared_owner(document["campaign_id"]))
        for reference, document in objects:
            key = canonical_sha256(reference)
            if reference["object_kind"] in CAMPAIGN_OWNED_OBJECTS:
                owners[key].add(declared_owner(document["payload"]["campaign_id"]))
            if reference["object_kind"] == "predecessor_import":
                # 源身份摘要／导入许可不等于源 Campaign 在本轮完成完整重放。
                # 不据此给跨 Store 的父域升级读集权限，已有外部核验过滤不变。
                for imported in document["payload"]["imported_objects"]:
                    owners[canonical_sha256(imported)].add(None)
        for key, references in edges.items():
            if not references.issubset(nodes):
                # 不能靠没有本轮原件的引用取得投影权利；严格路径保留原拒绝行为。
                owners[key].add(None)

        def propagate() -> None:
            pending = deque(key for key, values in owners.items() if values)
            queued = set(pending)
            while pending:
                key = pending.popleft()
                queued.remove(key)
                for target in edges[key] & nodes.keys():
                    previous = len(owners[target])
                    owners[target].update(owners[key])
                    if len(owners[target]) != previous and target not in queued:
                        pending.append(target)
                        queued.add(target)

        propagate()
        # 无所属根的对象即使只引用 Claude 原件，也是未知消费者，不能被忽略。
        for values in owners.values():
            if not values:
                values.add(None)
        propagate()
        self._owners = owners

    def is_claude_parent(self, reference: dict[str, Any]) -> bool:
        """每个已验来源都明确是 Claude v2 才投影；v1／未知／混合客户端均不投影。"""
        owners = self._owners.get(canonical_sha256(reference), set())
        return bool(owners) and all(
            owner is not None and campaign_is_v2(self._campaigns[owner])
            and self._campaigns[owner]["client"] == "claude"
            for owner in owners
        )
