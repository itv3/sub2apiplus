"""构建、验证、交付、晋升与激活收据的确定性生成和独立重放。

Claude 升级控制面 B1-12 在这里集中实现 Campaign v2 的三件事，供 store、gates、cli 共用：

* **Store 全包摘要与受管清单**：``control_tool_bundle_sha256()`` 的口径不变；Campaign v2 建立时用 Claude
  身份策略现算多根受管清单，核对清单指纹与 ``tool_identity.tool_files_sha256`` 一致，再从清单里取 Store
  口径内的条目按同一口径复算 ``tool_bundle_sha256``（``verify_campaign_tool_manifest``）。
* **有效工具身份**：``tool_evolution_registration`` 按 index 成单链，``effective_tool_identity`` 取 Campaign
  初始身份与最新登记的 ``to_identity``。
* **两类校验**：写入时 ``_require_effective_tool`` 要求当前受管代码匹配当前有效身份，并给出当前形成期坐标
  （有效身份引用、当前候选 revision、当前评估基线）；重放时按事实／收据自身 ``formed_under`` 解释坐标
  （``formed_coordinates``），与当前代码无关。v1 Campaign 保持原语义：没有 ``formed_under``，按 Campaign
  的 ``tool_bundle_sha256`` 解释；写入与收据重建都要求当前 Store 代码摘要等于它，原报错文案不变。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    expect_exact_keys,
    expect_object,
    sha256_bytes,
    sha256_file,
)
from .contracts import (
    DEPLOYMENT_STAGES,
    RECEIPT_SCHEMA_VERSIONS,
    VALIDATION_COMPLETED_SCHEMA,
    campaign_is_v2,
    iter_fact_refs,
    receipt_is_v2,
    validate_receipt_document,
)
from .errors import ControlError


TOOL_IDENTITY_V1_SCHEMA = "official-client-control-tool-identity/v1"
TOOL_IDENTITY_V2_SCHEMA = "official-client-control-tool-identity/v2"
# 写入时有效性失败的统一文案（设计 B1-12）：当前受管代码与 Campaign 当前有效工具身份不一致。
TOOL_EVOLUTION_REQUIRED = "工具身份未登记演进：先执行 tool-evolution"
# 写入时可现算、用来判定「当前代码＝有效身份」的四项。wire／evidence／control 三层摘要要从编排器出发
# 计算闭包（B1-09 之前编排器不存在），这里不现算：tool_files_sha256 覆盖两个受管根的全部受管文件（含编排器、
# 身份引擎与策略文件），policy_sha256 覆盖策略，二者相等即三层摘要的全部输入相同，三层摘要必然相同。
_COMPUTABLE_IDENTITY_FIELDS = ("client", "policy_version", "policy_sha256", "tool_files_sha256")
_STORE_ROOT_NAME = "official_client_control"


def _claude_identity_engine() -> Any:
    """Claude v3 身份引擎（B1-07）；延迟导入，v1 路径从不加载受管树的身份策略。"""

    from tools.official_client_capture import claude_tool_identity_policy

    return claude_tool_identity_policy


# Campaign v2 的 client → 能覆盖两个受管根（含 official_client_control）的身份引擎。
_IDENTITY_ENGINES = {"claude": _claude_identity_engine}


def _control_tool_bundle_paths(root: Path) -> list[Path]:
    """``control_tool_bundle_sha256`` 的口径：根下 ``*.py``（除 ``__init__.py``）与 ``schemas/*.json``。"""

    return sorted(
        [
            path
            for path in root.glob("*.py")
            if path.name != "__init__.py" and not path.is_symlink()
        ]
        + [
            path
            for path in (root / "schemas").glob("*.json")
            if not path.is_symlink()
        ]
    )


def control_tool_bundle_sha256(root: Path | None = None) -> str:
    """绑定实际参与合同、Store、门禁、CLI 和 Schema 的执行源。

    ``root`` 缺省为本模块所在目录（原行为）；给定时按同一口径计算另一份 Store 目录。
    """

    root = Path(__file__).resolve().parent if root is None else root
    paths = _control_tool_bundle_paths(root)
    if not paths:
        raise ControlError("FW-D 工具源码清单为空")
    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in paths
    ]
    return canonical_sha256(entries)


# ---------------------------------------------------------------------------
# 受管清单（Campaign v2）
# ---------------------------------------------------------------------------


def _tool_repository_root() -> Path:
    """受管工具所在仓库根（``tools/`` 的父目录）；测试替换为受管树副本以隔离并发改动。"""

    return Path(__file__).resolve().parents[2]


def _identity_engine(client: str) -> Any:
    loader = _IDENTITY_ENGINES.get(client)
    if loader is None:
        raise ControlError(
            f"client={client} 没有覆盖 {_STORE_ROOT_NAME} 的多根身份策略，Campaign v2 无法核对 Store 全包摘要"
        )
    return loader()


def current_tool_manifest(client: str) -> dict[str, Any]:
    """按 client 的身份策略现算多根受管清单（``<根目录名>/<相对路径>`` → sha256）与可现算的身份项。

    ``tool_files_sha256`` 与 v3 引擎 ``compute_identity`` 同一指纹公式；不调用 ``compute_identity``，因为它要求
    编排器存在（B1-09 才出现）。策略文件从同一仓库根读取，保证清单与策略来自同一棵树。
    """

    engine = _identity_engine(client)
    root = _tool_repository_root()
    policy_path = root / Path(engine.DEFAULT_POLICY_PATH).relative_to(engine.REPOSITORY_ROOT)
    try:
        policy = engine.load_policy(policy_path)
        digests = engine.managed_tree_digests(root, policy)
    except (engine.ToolIdentityPolicyError, OSError) as error:
        raise ControlError(f"受管工具清单无法现算：{error}") from error
    if policy["client"] != client:
        raise ControlError(f"身份策略 client={policy['client']} 与请求的 client={client} 不一致")
    entries = [{"path": key, "sha256": digests[key]} for key in sorted(digests)]
    return {
        "client": policy["client"],
        "policy_version": policy["policy_version"],
        "policy_sha256": policy["policy_sha256"],
        "tool_files_sha256": engine.codex_identity._fingerprint({"entries": entries}),
        "digests": digests,
        "repository_root": root,
        "policy": policy,
    }


def _in_store_bundle_scope(relative: str) -> bool:
    """清单条目（相对 Store 根）是否落在 ``control_tool_bundle_sha256()`` 口径内。"""

    parts = relative.split("/")
    if len(parts) == 1:
        return parts[0].endswith(".py") and parts[0] != "__init__.py"
    return len(parts) == 2 and parts[0] == "schemas" and parts[1].endswith(".json")


def store_bundle_from_manifest(manifest: dict[str, Any]) -> str:
    """从受管清单取 Store 口径内的全部条目，按 ``control_tool_bundle_sha256()`` 同一口径复算。

    口径条目是 ``{path, bytes, sha256}``，受管清单只有 sha256：对这些文件现读一次内容，核对读到的 sha256 与
    清单一致后取其字节数，bytes 与 sha256 因而来自同一份已被清单证明的内容。还要求口径内的磁盘文件与清单
    条目一一对应——Store 有文件没进受管清单（策略不再覆盖该根或后缀），或文件内容与清单不一致，都拒绝。
    各文件仍按策略分别进入 control 或 evidence 层，这里不改分层。
    """

    digests = manifest["digests"]
    prefix = f"{_STORE_ROOT_NAME}/"
    store_root = Path(manifest["repository_root"]) / "tools" / _STORE_ROOT_NAME
    paths = _control_tool_bundle_paths(store_root)
    if not paths:
        raise ControlError("FW-D 工具源码清单为空")
    on_disk = {prefix + path.relative_to(store_root).as_posix() for path in paths}
    listed = {
        key
        for key in digests
        if key.startswith(prefix) and _in_store_bundle_scope(key[len(prefix):])
    }
    if on_disk != listed:
        raise ControlError(
            "Store 工具文件与受管清单不对应："
            f"未进清单={sorted(on_disk - listed)}，清单多出={sorted(listed - on_disk)}"
        )
    entries: list[dict[str, Any]] = []
    for path in paths:
        relative = path.relative_to(store_root).as_posix()
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"Store 工具文件不是可信普通文件：{relative}")
        content = path.read_bytes()
        if sha256_bytes(content) != digests[prefix + relative]:
            raise ControlError(f"Store 工具文件与受管清单不一致（文件被改而清单未跟上）：{relative}")
        entries.append({"path": relative, "bytes": len(content), "sha256": digests[prefix + relative]})
    return canonical_sha256(entries)


def verify_campaign_tool_manifest(campaign: dict[str, Any]) -> dict[str, Any]:
    """Campaign v2 建立时核对冻结的工具身份与当前受管代码一致（设计 B1-12「Store 全包摘要的核对方式」）。

    ① 身份策略的 client、policy_version、policy_sha256 与冻结值相同；② 现算多根受管清单的指纹等于
    ``tool_identity.tool_files_sha256``；③ 清单内 Store 口径条目按同一口径复算等于 ``tool_bundle_sha256``。
    wire／evidence／control 三层摘要不在这里现算（见 ``_COMPUTABLE_IDENTITY_FIELDS`` 的说明）。
    """

    identity = campaign["tool_identity"]
    manifest = current_tool_manifest(identity["client"])
    for key in ("client", "policy_version", "policy_sha256"):
        if identity[key] != manifest[key]:
            raise ControlError(f"campaign.tool_identity.{key} 与当前身份策略不一致")
    if identity["tool_files_sha256"] != manifest["tool_files_sha256"]:
        raise ControlError(
            "campaign.tool_identity.tool_files_sha256 与现算受管文件清单不一致：受管文件已变化，须按当前代码重算身份"
        )
    if campaign["tool_bundle_sha256"] != store_bundle_from_manifest(manifest):
        raise ControlError(
            "campaign.tool_bundle_sha256 与受管清单内 Store 文件按同一口径复算的摘要不一致"
        )
    return manifest


def _layer_digests(manifest: dict[str, Any]) -> dict[str, str] | None:
    """三层摘要：编排器已在受管树内时用 v3 引擎 ``compute_identity`` 计算，否则返回 None（B1-09 之前）。"""

    policy = manifest["policy"]
    if str(policy["orchestrator"]["file"]) not in manifest["digests"]:
        return None
    engine = _identity_engine(manifest["client"])
    try:
        identity = engine.compute_identity(policy, manifest["repository_root"])
    except (engine.ToolIdentityPolicyError, OSError) as error:
        raise ControlError(f"三层工具身份摘要无法计算：{error}") from error
    if identity["tool_files_sha256"] != manifest["tool_files_sha256"]:
        raise ControlError("计算三层摘要期间受管文件发生变化，请重试")
    return {
        key: identity[key]
        for key in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256")
    }


def tool_digest_v2(client: str = "claude") -> dict[str, Any]:
    """``tool-digest`` 的 v2 输出：Store 自身摘要与七项工具身份（三层摘要暂不可算时为 null）。"""

    manifest = current_tool_manifest(client)
    layers = _layer_digests(manifest)
    identity: dict[str, Any] = {
        "client": manifest["client"],
        "policy_version": manifest["policy_version"],
        "policy_sha256": manifest["policy_sha256"],
        "wire_producer_sha256": None,
        "evidence_semantics_sha256": None,
        "control_sha256": None,
        "tool_files_sha256": manifest["tool_files_sha256"],
    }
    if layers is not None:
        identity.update(layers)
    return {
        "schema_version": TOOL_IDENTITY_V2_SCHEMA,
        "tool_bundle_sha256": store_bundle_from_manifest(manifest),
        "tool_identity": identity,
        "layer_digests_available": layers is not None,
    }


# ---------------------------------------------------------------------------
# 有效工具身份、候选 revision 与评估基线（Campaign v2）
# ---------------------------------------------------------------------------


def tool_evolutions(store: Any, campaign: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Campaign 的工具演进登记链 ``[(引用, payload)]``，按 index 排序并逐环核对衔接；v1 恒为空。

    链必须是 1..n 的单链：每份登记的 ``previous_ref`` 指向上一份（首份为 null），``from_identity`` 等于上一份的
    ``to_identity``（首份等于 Campaign 冻结的 ``tool_identity``）。缺号、分叉或衔接不上即失败关闭。
    """

    if not campaign_is_v2(campaign):
        return []
    campaign_id = campaign["campaign_id"]
    entries: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for reference in store.list_object_refs("tool_evolution_registration"):
        payload = store.load_object(reference)["payload"]
        if payload["campaign_id"] == campaign_id:
            entries.append((reference, payload))
    entries.sort(key=lambda item: item[1]["index"])
    previous_ref: dict[str, Any] | None = None
    previous_identity = campaign["tool_identity"]
    for position, (reference, payload) in enumerate(entries, start=1):
        if payload["index"] != position:
            raise ControlError(f"工具演进登记链缺号或分叉：{campaign_id} 第 {position} 份")
        if payload["previous_ref"] != previous_ref:
            raise ControlError(f"工具演进登记 index={position} 未引用上一份登记")
        if payload["from_identity"] != previous_identity:
            raise ControlError(f"工具演进登记 index={position} 的 from_identity 未承接上一份有效身份")
        if payload["client"] != campaign["client"]:
            raise ControlError(f"工具演进登记 index={position} 的 client 与 Campaign 不一致")
        previous_ref = reference
        previous_identity = payload["to_identity"]
    return entries


def effective_tool_identity(store: Any, campaign: dict[str, Any]) -> dict[str, Any] | None:
    """当前有效工具身份：最新登记的 ``to_identity``，没有登记时为 Campaign 初始身份；v1 没有七项身份返回 None。"""

    if not campaign_is_v2(campaign):
        return None
    chain = tool_evolutions(store, campaign)
    return dict(chain[-1][1]["to_identity"] if chain else campaign["tool_identity"])


def resolve_tool_identity(
    store: Any, campaign: dict[str, Any], tool_identity_ref: dict[str, Any] | None
) -> tuple[dict[str, Any], int]:
    """形成期工具身份引用 → ``(七项身份, 序号)``：null 为 Campaign 初始身份（序号 0），否则为该登记的 to_identity。

    引用必须是本 Campaign 合法登记链上的一环。
    """

    return FormedContext(store, campaign).resolve(tool_identity_ref)


def candidate_revision_facts(store: Any, campaign: dict[str, Any]) -> list[dict[str, Any]]:
    """Campaign 的 ``candidate_revision_recorded`` 事实（按写入顺序）；v1 恒为空。"""

    if not campaign_is_v2(campaign):
        return []
    return [
        fact
        for fact in store.list_facts(campaign["campaign_id"], "validation")
        if fact["fact_kind"] == "candidate_revision_recorded"
    ]


def current_candidate_revision(store: Any, campaign: dict[str, Any]) -> tuple[int, str | None]:
    """当前候选 revision 与当前有效候选：最后一条 revision 事实决定；没有时隐含 r1、不限定候选。

    B1-12 的最小形态；B3-05 在此基础上加作废对象门禁与 ``revision-open``／``invalidate-candidate`` 入口。
    """

    revisions = candidate_revision_facts(store, campaign)
    if not revisions:
        return 1, None
    last = revisions[-1]["payload"]
    return int(last["revision"]), str(last["candidate_id"])


def revision_candidate(revision_facts: list[dict[str, Any]], revision: int) -> str | None:
    """候选 revision r 对应的候选：有显式 revision 事实时为其 candidate_id；隐含 r1 不限定候选（None）。"""

    for fact in revision_facts:
        if fact["payload"]["revision"] == revision:
            return str(fact["payload"]["candidate_id"])
    if revision == 1:
        return None
    raise ControlError(f"候选 revision r{revision} 没有对应的 candidate_revision_recorded 事实")


def current_evaluation_baseline(store: Any, campaign: dict[str, Any]) -> dict[str, Any] | None:
    """当前评估基线：B1-12 阶段没有基线事实，恒为 None（b0）。B4-04 加入基线事实后改由最后一条基线事实决定。"""

    return None


class FormedContext:
    """一次读入 Campaign 的工具演进登记链与候选 revision 事实，批量解释形成期坐标。

    重放一个 Campaign 时逐条事实解释坐标，共用一个上下文避免重复扫描登记目录与 validation 链。
    """

    def __init__(self, store: Any, campaign: dict[str, Any]) -> None:
        self.campaign = campaign
        self.v2 = campaign_is_v2(campaign)
        self.chain = tool_evolutions(store, campaign)
        self.revision_facts = candidate_revision_facts(store, campaign)

    def resolve(self, tool_identity_ref: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
        if tool_identity_ref is None:
            return dict(self.campaign["tool_identity"]), 0
        for reference, payload in self.chain:
            if reference == tool_identity_ref:
                return dict(payload["to_identity"]), int(payload["index"])
        raise ControlError("形成期工具身份引用不在本 Campaign 的工具演进登记链上")

    def coordinates(self, document: dict[str, Any]) -> dict[str, Any]:
        """见 ``formed_coordinates``。"""

        if not self.v2:
            return {
                "tool_sha256": self.campaign["tool_bundle_sha256"],
                "tool_identity": None,
                "tool_index": 0,
                "candidate_revision": 1,
                "evaluation_baseline": None,
            }
        formed = document["formed_under"]
        identity, index = self.resolve(formed["tool_identity_ref"])
        revision = formed["candidate_revision"]
        revision_candidate(self.revision_facts, revision)
        if formed["evaluation_baseline"] is not None:
            raise ControlError("形成期评估基线无法核验：评估基线事实由 B4-04 定义，当前只接受 null")
        return {
            "tool_sha256": identity["tool_files_sha256"],
            "tool_identity": identity,
            "tool_index": index,
            "candidate_revision": revision,
            "evaluation_baseline": None,
        }

    def require_causality(
        self,
        store: Any,
        own: dict[str, Any],
        fact_refs: Any,
        label: str,
    ) -> None:
        """形成期因果序：引用的本 Campaign 事实不得晚于引用者自身的坐标。

        工具演进序号与候选 revision 都只增不减，被引用的事实先于引用者形成，所以两项都不得更大。评估基线按
        候选重新计数（新 revision 回到 b0），不参与比较；同一 revision 内的基线顺序由 B4-04 定义。
        """

        if not self.v2:
            return
        for reference in fact_refs:
            if reference["campaign_id"] != self.campaign["campaign_id"]:
                continue
            theirs = self.coordinates(store.load_fact(reference))
            if (
                theirs["tool_index"] > own["tool_index"]
                or theirs["candidate_revision"] > own["candidate_revision"]
            ):
                raise ControlError(
                    f"{label} 的形成期坐标早于其引用的事实"
                    f"（{reference['dimension']}#{reference['sequence']}）：形成期坐标不可信"
                )


def formed_coordinates(
    store: Any, campaign: dict[str, Any], document: dict[str, Any]
) -> dict[str, Any]:
    """事实或收据的形成期坐标，并核对坐标在本 Campaign 内可达。

    v2 读文档自身的 ``formed_under``：工具身份引用必须在登记链上，候选 revision 必须是 r1 或某条 revision 事实的
    revision，评估基线在 B4-04 之前只接受 null。v1 文档没有该字段，按 Campaign ``tool_bundle_sha256`` 解释（隐含
    r1、无基线）。返回 ``{tool_sha256, tool_identity, tool_index, candidate_revision, evaluation_baseline}``，其中
    ``tool_sha256`` 是该坐标下工具摘要字段应取的值（v1 为 Store 摘要，v2 为有效身份的 ``tool_files_sha256``）。
    """

    return FormedContext(store, campaign).coordinates(document)


def _require_effective_tool(store: Any, campaign: dict[str, Any]) -> dict[str, Any]:
    """写入时有效性：当前受管代码必须匹配 Campaign 当前有效工具身份。

    v1：当前 Store 代码摘要必须等于 Campaign 的 ``tool_bundle_sha256``（原语义、原文案），返回
    ``{"tool_sha256": 当前 Store 摘要, "formed_under": None}``。

    v2：现算受管清单，可现算四项（见 ``_COMPUTABLE_IDENTITY_FIELDS``）必须等于有效身份，否则
    「工具身份未登记演进：先执行 tool-evolution」；返回有效身份的 ``tool_files_sha256`` 与当前形成期坐标
    ``{tool_identity_ref, candidate_revision, evaluation_baseline}``——写入的事实与收据都以它为 ``formed_under``。
    """

    if not campaign_is_v2(campaign):
        current = control_tool_bundle_sha256()
        if campaign["tool_bundle_sha256"] != current:
            raise ControlError("控制面工具身份已变化，必须建立新的 Campaign")
        return {"tool_sha256": current, "formed_under": None}
    chain = tool_evolutions(store, campaign)
    effective = chain[-1][1]["to_identity"] if chain else campaign["tool_identity"]
    manifest = current_tool_manifest(effective["client"])
    drift = [key for key in _COMPUTABLE_IDENTITY_FIELDS if manifest[key] != effective[key]]
    if drift:
        raise ControlError(
            f"{TOOL_EVOLUTION_REQUIRED}（当前受管代码与有效工具身份不一致：{'、'.join(drift)}）"
        )
    revision, _candidate = current_candidate_revision(store, campaign)
    return {
        "tool_sha256": effective["tool_files_sha256"],
        "formed_under": {
            "tool_identity_ref": chain[-1][0] if chain else None,
            "candidate_revision": revision,
            "evaluation_baseline": current_evaluation_baseline(store, campaign),
        },
        "tool_identity": dict(effective),
    }


def current_formed_under(store: Any, campaign: dict[str, Any]) -> dict[str, Any] | None:
    """当前形成期坐标（含写入时有效性核对）；v1 返回 None。"""

    return _require_effective_tool(store, campaign)["formed_under"]


def require_acceptance_formed_under(
    acceptance: dict[str, Any], formed_under: dict[str, Any] | None, label: str
) -> None:
    """交付判定：v2 下只认与本次写入（或被重放文档）同一候选 revision、同一评估基线下形成的 Acceptance。

    ``formed_under`` 为 None（v1）时不检查。写入时 ``formed_under`` 是当前坐标，所以旧 revision／旧基线的
    Acceptance 不能冒充当前结果推进 VC-5 完成、候选交付或晋升；重放时是被重放文档的形成期坐标。
    """

    if formed_under is None:
        return
    formed = acceptance.get("formed_under")
    if not isinstance(formed, dict):
        raise ControlError(f"{label} 引用的 Acceptance 没有形成期坐标")
    if (formed["candidate_revision"], formed["evaluation_baseline"]) != (
        formed_under["candidate_revision"],
        formed_under["evaluation_baseline"],
    ):
        raise ControlError(
            f"{label} 引用的 Acceptance 形成于候选 revision r{formed['candidate_revision']}、评估基线 "
            f"{formed['evaluation_baseline']}，不是 r{formed_under['candidate_revision']}、"
            f"{formed_under['evaluation_baseline']}：旧结果不得冒充当前写入"
        )


def require_candidate_in_revision(
    revision_facts: list[dict[str, Any]],
    candidate_id: str,
    formed_under: dict[str, Any] | None,
    label: str,
) -> None:
    """候选必须属于形成期候选 revision；该 revision 有显式事实时候选必须等于其 candidate_id。"""

    if formed_under is None:
        return
    revision = formed_under["candidate_revision"]
    expected = revision_candidate(revision_facts, revision)
    if expected is not None and candidate_id != expected:
        raise ControlError(
            f"{label} 的候选 {candidate_id} 不属于候选 revision r{revision}（该 revision 的候选是 {expected}）"
        )


def _receipt_producer(
    store: Any, campaign: dict[str, Any], formed_under: dict[str, Any] | None
) -> dict[str, Any]:
    """收据的生产坐标：写入时（``formed_under`` 为 None）核当前有效身份；v2 重放时按收据自身形成期坐标。

    v1 重放沿用原语义（收据重建要求当前 Store 代码摘要等于 Campaign 冻结值），因此 v1 收据从不携带坐标。
    """

    if not campaign_is_v2(campaign):
        if formed_under is not None:
            raise ControlError("v1 Campaign 的收据不得携带 formed_under")
        current = _require_effective_tool(store, campaign)
        return {"v2": False, "tool_sha256": current["tool_sha256"], "formed_under": None}
    if formed_under is None:
        current = _require_effective_tool(store, campaign)
        return {"v2": True, "tool_sha256": current["tool_sha256"], "formed_under": current["formed_under"]}
    coordinates = formed_coordinates(store, campaign, {"formed_under": formed_under})
    return {"v2": True, "tool_sha256": coordinates["tool_sha256"], "formed_under": formed_under}


def _receipt_schema(kind: str, producer: dict[str, Any]) -> str:
    return RECEIPT_SCHEMA_VERSIONS[kind][1 if producer["v2"] else 0]


def _attach_formed_under(receipt: dict[str, Any], producer: dict[str, Any]) -> None:
    """v2 收据记录形成期坐标；必须在计算 ``identity_sha256`` 之前加入。"""

    if producer["v2"]:
        receipt["formed_under"] = producer["formed_under"]


def require_receipt_current(store: Any, receipt: dict[str, Any]) -> None:
    """收据写入时有效性：合同版本与 Campaign 一致；v2 收据的坐标与工具摘要必须是当前值。

    finalizer 产出的收据天然满足；把旧收据（旧工具身份、旧 revision 或旧基线下形成的）原样或改字段后写入，
    即「旧结果冒充当前写入」，在这里拒绝。
    """

    campaign = store.load_campaign(receipt["campaign_id"])
    if campaign_is_v2(campaign) != receipt_is_v2(receipt):
        raise ControlError("收据合同版本与 Campaign 版本不一致")
    if not receipt_is_v2(receipt):
        return
    current = _require_effective_tool(store, campaign)
    if (
        receipt["formed_under"] != current["formed_under"]
        or receipt["producer_tool_sha256"] != current["tool_sha256"]
    ):
        raise ControlError("收据的形成期坐标或工具摘要不是当前有效值：旧结果不得冒充当前写入")


def build_candidate_build_receipt(
    store: Any,
    campaign_id: str,
    build_input: dict[str, Any],
    *,
    formed_under: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """由 VC-4 已封存构建输入生成不可覆盖 CandidateBuildReceipt。

    ``formed_under`` 只在重放 v2 收据时传入（收据自身记录的形成期坐标）；写入时为 None。
    """

    campaign = store.load_campaign(campaign_id)
    producer = _receipt_producer(store, campaign, formed_under)
    value = expect_object(build_input, "candidate_build_input")
    expected_keys = {
        "candidate_id",
        "candidate_purpose",
        "profile_approval_ref",
        "release_artifact_ref",
        "support_envelope_ref",
        "source_identity",
        "profile_identity",
        "validation_scope_sha256",
        "build_identity",
        "artifact_inventory",
        "image",
        "completed_at_utc",
    }
    expect_exact_keys(value, expected_keys, "candidate_build_input")
    require_candidate_in_revision(
        candidate_revision_facts(store, campaign),
        value["candidate_id"],
        producer["formed_under"],
        "CandidateBuildReceipt",
    )
    approval = store.load_fact(value["profile_approval_ref"])
    if approval["fact_kind"] != "profile_approved":
        raise ControlError("CandidateBuildReceipt 必须绑定 ProfileApprovalFact")
    if approval["campaign_id"] != campaign_id:
        raise ControlError("CandidateBuildReceipt 不得跨 Campaign 引用批准事实")
    approval_payload = approval["payload"]
    if (
        value["candidate_purpose"] != approval_payload["approval_purpose"]
        or value["release_artifact_ref"] != approval_payload["release_artifact_ref"]
        or value["support_envelope_ref"] != approval_payload["support_envelope_ref"]
    ):
        raise ControlError("CandidateBuildReceipt 与 ProfileApprovalFact 身份不一致")
    profile_identity = value["profile_identity"]
    expected_profile_refs = {
        "profile_schema_ref": approval_payload["profile_schema_ref"],
        "snapshot_ref": approval_payload["snapshot_ref"],
        "release_artifact_ref": approval_payload["release_artifact_ref"],
        "support_envelope_ref": approval_payload["support_envelope_ref"],
    }
    for key, expected in expected_profile_refs.items():
        if profile_identity.get(key) != expected:
            raise ControlError(f"CandidateBuildReceipt profile_identity.{key} 与批准事实不一致")
    release = store.load_object(value["release_artifact_ref"])["payload"]
    if (
        release["version"] != campaign["target_version"]
        or profile_identity.get("profile_digest") != release["profile_digest"]
    ):
        raise ControlError("CandidateBuildReceipt 的版本或 Profile digest 与 Release 不一致")
    store.load_object(profile_identity["model_capability_catalog_ref"])
    if value["build_identity"].get("deployed_version") != campaign["target_version"]:
        raise ControlError("CandidateBuildReceipt deployed_version 与 Campaign 目标不一致")
    inputs = [
        value["profile_approval_ref"],
        value["release_artifact_ref"],
        value["support_envelope_ref"],
        profile_identity["model_capability_catalog_ref"],
    ]
    receipt = {
        "schema_version": _receipt_schema("candidate_build", producer),
        "campaign_id": campaign_id,
        "candidate_id": value["candidate_id"],
        "candidate_purpose": value["candidate_purpose"],
        "target_version": campaign["target_version"],
        "profile_approval_ref": value["profile_approval_ref"],
        "release_artifact_ref": value["release_artifact_ref"],
        "support_envelope_ref": value["support_envelope_ref"],
        "source_identity": value["source_identity"],
        "profile_identity": profile_identity,
        "validation_scope_sha256": value["validation_scope_sha256"],
        "build_identity": value["build_identity"],
        "artifact_inventory": value["artifact_inventory"],
        "image": value["image"],
        "input_facts_sha256": canonical_sha256(inputs),
        "completed_at_utc": value["completed_at_utc"],
        "producer_tool_sha256": producer["tool_sha256"],
        "identity_sha256": "",
    }
    _attach_formed_under(receipt, producer)
    receipt["identity_sha256"] = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    validate_receipt_document(receipt, "candidate_build")
    return receipt


def finalize_candidate_build(
    store: Any, campaign_id: str, build_input: dict[str, Any]
) -> dict[str, Any]:
    return store.write_receipt(
        "candidate_build",
        build_candidate_build_receipt(store, campaign_id, build_input),
    )


def _candidate_build_input(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: receipt[key]
        for key in (
            "candidate_id",
            "candidate_purpose",
            "profile_approval_ref",
            "release_artifact_ref",
            "support_envelope_ref",
            "source_identity",
            "profile_identity",
            "validation_scope_sha256",
            "build_identity",
            "artifact_inventory",
            "image",
            "completed_at_utc",
        )
    }


def build_validation_gate_receipt(
    store: Any,
    campaign_id: str,
    validation_attempt_ref: dict[str, Any],
    result_input: dict[str, Any],
    *,
    formed_under: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一次执行或复用的外部门禁结果绑定到冻结 attempt 与计划。"""

    campaign = store.load_campaign(campaign_id)
    producer = _receipt_producer(store, campaign, formed_under)
    value = expect_object(result_input, "validation_gate_result")
    expect_exact_keys(
        value,
        {
            "gate_id",
            "started_at_utc",
            "completed_at_utc",
            "exit_code",
            "output_sha256",
            "result",
        },
        "validation_gate_result",
    )
    attempt = store.load_fact(validation_attempt_ref)
    if attempt["fact_kind"] != "validation_attempt_created":
        raise ControlError("ValidationGateReceipt 必须绑定 ValidationAttempt")
    if attempt["campaign_id"] != campaign_id:
        raise ControlError("ValidationGateReceipt 不得跨 Campaign 引用 attempt")
    attempt_payload = attempt["payload"]
    require_candidate_in_revision(
        candidate_revision_facts(store, campaign),
        attempt_payload["candidate_id"],
        producer["formed_under"],
        "ValidationGateReceipt",
    )
    plan_ref = attempt_payload["validation_execution_plan_ref"]
    plan = store.load_object(plan_ref)["payload"]
    matches = [item for item in plan["external_gates"] if item["gate_id"] == value["gate_id"]]
    if len(matches) != 1:
        raise ControlError("外部门禁不属于冻结的 ValidationExecutionPlan")
    planned = matches[0]
    if planned["source"] == "reuse":
        reused = store.load_receipt(planned["reused_receipt_ref"])
        if reused["result"] != "pass":
            raise ControlError("不得复用失败的外部门禁收据")
        for key in (
            "started_at_utc",
            "completed_at_utc",
            "exit_code",
            "output_sha256",
            "result",
        ):
            if value[key] != reused[key]:
                raise ControlError(f"复用门禁不得改写原收据字段：{key}")
    inputs = [
        validation_attempt_ref,
        plan_ref,
        attempt_payload["candidate_build_receipt_ref"],
        planned["reused_receipt_ref"],
        planned["previous_failed_receipt_ref"],
    ]
    receipt = {
        "schema_version": _receipt_schema("validation_gate", producer),
        "campaign_id": campaign_id,
        "candidate_id": attempt_payload["candidate_id"],
        "attempt_id": attempt_payload["attempt_id"],
        "candidate_ref": attempt_payload["candidate_ref"],
        "candidate_build_receipt_ref": attempt_payload["candidate_build_receipt_ref"],
        "validation_attempt_ref": validation_attempt_ref,
        "validation_execution_plan_ref": plan_ref,
        "gate_id": planned["gate_id"],
        "requirement_sha256": planned["requirement_sha256"],
        "source": planned["source"],
        "command": planned["command"],
        "working_directory": planned["working_directory"],
        "host_id": planned["host_id"],
        "architecture": planned["architecture"],
        "started_at_utc": value["started_at_utc"],
        "completed_at_utc": value["completed_at_utc"],
        "exit_code": value["exit_code"],
        "output_sha256": value["output_sha256"],
        "result": value["result"],
        "reused_receipt_ref": planned["reused_receipt_ref"],
        "previous_failed_receipt_ref": planned[
            "previous_failed_receipt_ref"
        ],
        "input_facts_sha256": canonical_sha256(inputs),
        "producer_tool_sha256": producer["tool_sha256"],
        "identity_sha256": "",
    }
    _attach_formed_under(receipt, producer)
    receipt["identity_sha256"] = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    validate_receipt_document(receipt, "validation_gate")
    return receipt


def finalize_validation_gate(
    store: Any,
    campaign_id: str,
    validation_attempt_ref: dict[str, Any],
    result_input: dict[str, Any],
) -> dict[str, Any]:
    receipt = build_validation_gate_receipt(
        store, campaign_id, validation_attempt_ref, result_input
    )
    prior_failures: list[tuple[dict[str, Any], dict[str, Any]]] = []
    prior_passes: list[dict[str, Any]] = []
    for reference in store.list_receipt_refs("validation_gate"):
        existing = store.load_receipt(reference)
        if (
            existing["validation_attempt_ref"] == validation_attempt_ref
            and existing["gate_id"] == receipt["gate_id"]
        ):
            raise ControlError(
                "同一 ValidationAttempt 的外部门禁槽位不得覆盖；失败后必须建立新 attempt"
            )
        if (
            existing["campaign_id"] == campaign_id
            and existing["candidate_ref"] == receipt["candidate_ref"]
            and existing["gate_id"] == receipt["gate_id"]
        ):
            if existing["result"] == "failed":
                prior_failures.append((reference, existing))
            else:
                prior_passes.append(existing)
    previous_ref = receipt["previous_failed_receipt_ref"]
    if receipt["source"] == "execute" and prior_passes:
        raise ControlError("已有通过门禁必须重放收据，不得在新 attempt 重复执行")
    if receipt["source"] == "execute" and prior_failures:
        prior_failures.sort(
            key=lambda item: datetime.fromisoformat(
                item[1]["completed_at_utc"].replace("Z", "+00:00")
            )
        )
        if previous_ref != prior_failures[-1][0]:
            raise ControlError("重试外部门禁必须绑定最近一份前序失败收据")
    elif receipt["source"] == "execute" and previous_ref is not None:
        raise ControlError("外部门禁声明了不存在的前序失败收据")
    return store.write_receipt("validation_gate", receipt)


def _validation_gate_input(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: receipt[key]
        for key in (
            "gate_id",
            "started_at_utc",
            "completed_at_utc",
            "exit_code",
            "output_sha256",
            "result",
        )
    }


def finalize_validation(
    store: Any,
    campaign_id: str,
    acceptance_ref: dict[str, Any],
    production_selector_after_ref: dict[str, Any],
    issued_at_utc: str,
) -> dict[str, Any]:
    """从严格 Acceptance 派生 VC-5 完成事实；门禁在追加前统一执行。

    v2 下 Acceptance 必须形成于当前候选 revision 与当前评估基线（交付判定由 validation_completed 门禁执行）。
    """

    _require_effective_tool(store, store.load_campaign(campaign_id))
    acceptance = store.load_fact(acceptance_ref)
    if acceptance["fact_kind"] != "acceptance_recorded":
        raise ControlError("VC-5 finalizer 必须绑定 AcceptanceFact")
    if acceptance["campaign_id"] != campaign_id:
        raise ControlError("VC-5 finalizer 不得跨 Campaign 引用 AcceptanceFact")
    value = acceptance["payload"]
    if value.get("schema_version") is None:
        raise ControlError("VC-5 finalizer 不接受历史 AcceptanceFact")
    attempt = store.load_fact(value["validation_attempt_ref"])["payload"]
    payload = {
        "schema_version": VALIDATION_COMPLETED_SCHEMA,
        "attempt_id": attempt["attempt_id"],
        "candidate_id": value["candidate_id"],
        "candidate_ref": value["candidate_ref"],
        "candidate_build_receipt_ref": value["candidate_build_receipt_ref"],
        "validation_attempt_ref": value["validation_attempt_ref"],
        "validation_execution_plan_ref": attempt["validation_execution_plan_ref"],
        "candidate_evidence_package_ref": value["candidate_evidence_package_ref"],
        "acceptance_ref": acceptance_ref,
        "pair_refs": value["pair_refs"],
        "external_gate_receipt_refs": value["external_gate_receipt_refs"],
        "production_selector_after_ref": production_selector_after_ref,
        "vircs_state": attempt["vircs_state"],
        "result": "passed",
        "identity_sha256": "",
    }
    payload["identity_sha256"] = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    return store.append_fact(
        campaign_id, "validation_completed", payload, issued_at_utc
    )


def build_promotion_receipt(
    store: Any,
    campaign_id: str,
    promotion_fact_ref: dict[str, Any],
    *,
    formed_under: dict[str, Any] | None = None,
) -> dict[str, Any]:
    campaign = store.load_campaign(campaign_id)
    producer = _receipt_producer(store, campaign, formed_under)
    promotion = store.load_fact(promotion_fact_ref)
    if promotion["fact_kind"] != "release_promoted":
        raise ControlError("PromotionReceipt 必须由 release_promoted 事实生成")
    if promotion["campaign_id"] != campaign_id:
        raise ControlError("PromotionReceipt 不得跨 Campaign 引用晋升事实")
    payload = promotion["payload"]
    candidate = store.load_fact(payload["candidate_ref"])
    acceptance = store.load_fact(payload["acceptance_ref"])
    if candidate["fact_kind"] != "candidate_frozen" or acceptance["fact_kind"] != "acceptance_recorded":
        raise ControlError("晋升事实的 Candidate／Acceptance 引用类型错误")
    require_candidate_in_revision(
        candidate_revision_facts(store, campaign),
        candidate["payload"]["candidate_id"],
        producer["formed_under"],
        "PromotionReceipt",
    )
    require_acceptance_formed_under(acceptance, producer["formed_under"], "PromotionReceipt")
    approval_ref = candidate["payload"]["profile_approval_ref"]
    inputs = [
        payload["candidate_ref"],
        payload["acceptance_ref"],
        promotion_fact_ref,
        approval_ref,
    ]
    receipt = {
        "schema_version": _receipt_schema("promotion", producer),
        "campaign_id": campaign_id,
        "candidate_ref": payload["candidate_ref"],
        "acceptance_ref": payload["acceptance_ref"],
        "promotion_fact_ref": promotion_fact_ref,
        "release_artifact_ref": payload["release_artifact_ref"],
        "promotion_diff_ref": payload["promotion_diff_ref"],
        "profile_approval_ref": approval_ref,
        "input_facts_sha256": canonical_sha256(inputs),
        "completed_at_utc": promotion["issued_at_utc"],
        "producer_tool_sha256": producer["tool_sha256"],
    }
    _attach_formed_under(receipt, producer)
    validate_receipt_document(receipt, "promotion")
    return receipt


def finalize_promotion(
    store: Any, campaign_id: str, promotion_fact_ref: dict[str, Any]
) -> dict[str, Any]:
    return store.write_receipt(
        "promotion", build_promotion_receipt(store, campaign_id, promotion_fact_ref)
    )


def _deployment_chain(
    store: Any, restored_active_ref: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    restored = store.load_fact(restored_active_ref)
    if restored["fact_kind"] != "restored_active":
        raise ControlError("激活收据必须从 restored_active DeploymentFact 生成")
    reversed_chain: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (restored_active_ref, restored)
    ]
    current = restored
    while current["fact_kind"] != "accepted_not_activated":
        previous_ref = current["payload"]["previous_stage_ref"]
        previous = store.load_fact(previous_ref)
        reversed_chain.append((previous_ref, previous))
        current = previous
    chain = list(reversed(reversed_chain))
    if [fact["fact_kind"] for _reference, fact in chain] != list(DEPLOYMENT_STAGES):
        raise ControlError("DeploymentFact 没有形成完整五阶段链")
    return chain


def build_activation_receipt(
    store: Any,
    campaign_id: str,
    restored_active_ref: dict[str, Any],
    selector_before_ref: dict[str, Any],
    selector_after_ref: dict[str, Any],
    inventory_current_ref: dict[str, Any],
    *,
    formed_under: dict[str, Any] | None = None,
) -> dict[str, Any]:
    campaign = store.load_campaign(campaign_id)
    producer = _receipt_producer(store, campaign, formed_under)
    chain = _deployment_chain(store, restored_active_ref)
    if any(fact["campaign_id"] != campaign_id for _reference, fact in chain):
        raise ControlError("ActivationReceipt 不得跨 Campaign 引用 DeploymentFact")
    first = chain[0][1]["payload"]
    final = chain[-1][1]
    final_payload = final["payload"]
    selector_before = store.load_fact(selector_before_ref)
    selector_after = store.load_fact(selector_after_ref)
    inventory_current = store.load_fact(inventory_current_ref)
    if selector_before["fact_kind"] != "selector_observed":
        raise ControlError("ActivationReceipt 的 selector_before 必须是只读观察事实")
    if selector_after["fact_kind"] != "selector_activated":
        raise ControlError("ActivationReceipt 的 selector_after 必须是激活事实")
    if inventory_current["fact_kind"] != "inventory_current_appended":
        raise ControlError("ActivationReceipt 缺少 Deployment 后追加的当前 Inventory")
    if any(
        fact["campaign_id"] != campaign_id
        for fact in (selector_before, selector_after, inventory_current)
    ):
        raise ControlError("ActivationReceipt 的 selector／Inventory 不得跨 Campaign")
    if inventory_current["payload"]["deployment_ref"] != restored_active_ref:
        raise ControlError("ActivationReceipt 的最终 Inventory 未绑定 restored_active")
    if selector_after["payload"]["catalog_snapshot_ref"] != final_payload[
        "runtime_catalog_snapshot_ref"
    ]:
        raise ControlError("恢复后的 Runtime Selector 与最终 DeploymentFact 不一致")
    promotion_ref = first["promotion_receipt_ref"]
    promotion = store.load_receipt(promotion_ref)
    if promotion["campaign_id"] != campaign_id:
        raise ControlError("ActivationReceipt 的 PromotionReceipt 跨 Campaign")
    acceptance = store.load_fact(first["acceptance_ref"])
    candidate_ref = acceptance["payload"]["candidate_ref"]
    if promotion["candidate_ref"] != candidate_ref:
        raise ControlError("ActivationReceipt 的 Promotion 与 Candidate 不一致")
    require_candidate_in_revision(
        candidate_revision_facts(store, campaign),
        acceptance["payload"]["candidate_id"],
        producer["formed_under"],
        "ActivationReceipt",
    )
    require_acceptance_formed_under(acceptance, producer["formed_under"], "ActivationReceipt")
    deployment_refs = [reference for reference, _fact in chain]
    inputs = [
        candidate_ref,
        first["acceptance_ref"],
        selector_before_ref,
        selector_after_ref,
        inventory_current_ref,
        *deployment_refs,
    ]
    receipt = {
        "schema_version": _receipt_schema("activation", producer),
        "campaign_id": campaign_id,
        "candidate_ref": candidate_ref,
        "acceptance_ref": first["acceptance_ref"],
        "promotion_receipt_ref": promotion_ref,
        "deployment_fact_refs": deployment_refs,
        "selector_before_ref": selector_before_ref,
        "selector_after_ref": selector_after_ref,
        "active_support_envelope_ref": final_payload["active_support_envelope_ref"],
        "rollback_operational_envelope_ref": final_payload[
            "rollback_operational_envelope_ref"
        ],
        "deployment_traffic_envelope_ref": final_payload[
            "deployment_traffic_envelope_ref"
        ],
        "final_ingress_inventory_ref": inventory_current["payload"][
            "production_ingress_inventory_ref"
        ],
        "final_egress_inventory_ref": inventory_current["payload"][
            "egress_disposition_inventory_ref"
        ],
        "formal_image_digest": final_payload["image_digest"],
        "final_state": "restored_active",
        "input_facts_sha256": canonical_sha256(inputs),
        "completed_at_utc": inventory_current["issued_at_utc"],
        "producer_tool_sha256": producer["tool_sha256"],
    }
    _attach_formed_under(receipt, producer)
    validate_receipt_document(receipt, "activation")
    return receipt


def finalize_activation(
    store: Any,
    campaign_id: str,
    restored_active_ref: dict[str, Any],
    selector_before_ref: dict[str, Any],
    selector_after_ref: dict[str, Any],
    inventory_current_ref: dict[str, Any],
) -> dict[str, Any]:
    receipt = build_activation_receipt(
        store,
        campaign_id,
        restored_active_ref,
        selector_before_ref,
        selector_after_ref,
        inventory_current_ref,
    )
    return store.write_receipt("activation", receipt)


def build_candidate_delivery_receipt(
    store: Any,
    campaign_id: str,
    candidate_delivery_package_ref: dict[str, Any],
    *,
    formed_under: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """由完整 DMIT 四阶段事实和交付包生成候选交付终态收据。

    v2 下只认当前候选 revision 与当前评估基线下形成的 Acceptance（重放时为收据形成期坐标）。
    """

    campaign = store.load_campaign(campaign_id)
    producer = _receipt_producer(store, campaign, formed_under)
    package_document = store.load_object(candidate_delivery_package_ref)
    if package_document["object_kind"] != "candidate_delivery_package":
        raise ControlError("CandidateDeliveryReceipt 必须绑定 CandidateDeliveryPackage")
    package = package_document["payload"]
    if package["campaign_id"] != campaign_id:
        raise ControlError("CandidateDeliveryPackage 与 Campaign 不一致")
    facts = [store.load_fact(reference) for reference in package["delivery_fact_refs"]]
    stages = [fact["payload"]["stage"] for fact in facts]
    if stages != [
        "candidate_active",
        "rollback_verified",
        "candidate_restored",
        "stable_observed",
    ]:
        raise ControlError("CandidateDeliveryReceipt 要求完整有序四阶段事实")
    if any(fact["payload"]["result"] != "pass" for fact in facts):
        raise ControlError("CandidateDeliveryReceipt 不得消费失败的候选交付阶段")
    stable = facts[-1]
    candidate = store.load_fact(package["candidate_ref"])
    acceptance = store.load_fact(package["acceptance_ref"])
    validation = store.load_fact(package["validation_completed_ref"])
    if (
        candidate["fact_kind"] != "candidate_frozen"
        or acceptance["fact_kind"] != "acceptance_recorded"
        or validation["fact_kind"] != "validation_completed"
    ):
        raise ControlError("CandidateDeliveryReceipt 的 Candidate／Acceptance／VC-5 引用错误")
    candidate_payload = candidate["payload"]
    acceptance_payload = acceptance["payload"]
    if validation["payload"]["acceptance_ref"] != package["acceptance_ref"]:
        raise ControlError("CandidateDeliveryReceipt 未绑定同一 VC-5 Acceptance 链")
    require_candidate_in_revision(
        candidate_revision_facts(store, campaign),
        candidate_payload["candidate_id"],
        producer["formed_under"],
        "CandidateDeliveryReceipt",
    )
    require_acceptance_formed_under(
        acceptance, producer["formed_under"], "CandidateDeliveryReceipt"
    )
    plan = store.load_object(package["delivery_plan_ref"])["payload"]
    archive_ref = package["private_archive_manifest_ref"]
    inputs = [
        package["candidate_ref"],
        package["candidate_build_receipt_ref"],
        package["validation_completed_ref"],
        package["acceptance_ref"],
        package["candidate_evidence_package_ref"],
        package["delivery_plan_ref"],
        *package["delivery_fact_refs"],
        candidate_delivery_package_ref,
        archive_ref,
    ]
    receipt = {
        "schema_version": _receipt_schema("candidate_delivery", producer),
        "campaign_id": campaign_id,
        "campaign_purpose": acceptance_payload["acceptance_purpose"],
        "target_version": campaign["target_version"],
        "profile_approval_ref": acceptance_payload["profile_approval_ref"],
        "candidate_id": candidate_payload["candidate_id"],
        "candidate_ref": package["candidate_ref"],
        "candidate_build_receipt_ref": package["candidate_build_receipt_ref"],
        "validation_completed_ref": package["validation_completed_ref"],
        "acceptance_ref": package["acceptance_ref"],
        "candidate_evidence_package_ref": package[
            "candidate_evidence_package_ref"
        ],
        "delivery_plan_ref": package["delivery_plan_ref"],
        "delivery_fact_refs": package["delivery_fact_refs"],
        "candidate_delivery_package_ref": candidate_delivery_package_ref,
        "private_archive_manifest_ref": archive_ref,
        "candidate_image_digest": plan["candidate_image_digest"],
        "host_id": plan["host_id"],
        "architecture": plan["architecture"],
        "release_state": "ready_for_operator_release",
        "vircs_state": plan["vircs_state"],
        "input_facts_sha256": canonical_sha256(inputs),
        "completed_at_utc": stable["issued_at_utc"],
        "producer_tool_sha256": producer["tool_sha256"],
        "identity_sha256": "",
    }
    _attach_formed_under(receipt, producer)
    receipt["identity_sha256"] = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    validate_receipt_document(receipt, "candidate_delivery")
    return receipt


def finalize_candidate_delivery(
    store: Any,
    campaign_id: str,
    candidate_delivery_package_ref: dict[str, Any],
) -> dict[str, Any]:
    receipt = build_candidate_delivery_receipt(
        store, campaign_id, candidate_delivery_package_ref
    )
    for reference in store.list_receipt_refs("candidate_delivery"):
        existing = store.load_receipt(reference)
        if (
            existing["candidate_ref"] == receipt["candidate_ref"]
            and existing["acceptance_ref"] == receipt["acceptance_ref"]
        ):
            raise ControlError("同一 Candidate／Acceptance 的候选交付收据不得覆盖")
    return store.write_receipt("candidate_delivery", receipt)


def replay_receipt(store: Any, reference: dict[str, Any]) -> dict[str, Any]:
    """从所引用事实重建收据，拒绝任一输入或收据字段不匹配。

    v2 收据按自身 ``formed_under`` 的坐标重建：工具摘要取形成期有效身份，交付判定比较形成期 revision 与基线，
    与当前受管代码无关——重放只证明形成时一致，当前有效性另由写入门禁与 status 判定。还核对收据引用的事实
    不晚于收据自身的形成期坐标。v1 收据沿用原语义。
    """

    existing = store.load_receipt(reference)
    kind = reference["receipt_kind"]
    campaign = store.load_campaign(existing["campaign_id"])
    if campaign_is_v2(campaign) != receipt_is_v2(existing):
        raise ControlError(f"{kind} 收据合同版本与 Campaign 版本不一致")
    formed_under = existing["formed_under"] if receipt_is_v2(existing) else None
    if formed_under is not None:
        context = FormedContext(store, campaign)
        context.require_causality(
            store, context.coordinates(existing), iter_fact_refs(existing), f"{kind} 收据"
        )
    if kind == "promotion":
        rebuilt = build_promotion_receipt(
            store,
            existing["campaign_id"],
            existing["promotion_fact_ref"],
            formed_under=formed_under,
        )
    elif kind == "activation":
        rebuilt = build_activation_receipt(
            store,
            existing["campaign_id"],
            existing["deployment_fact_refs"][-1],
            existing["selector_before_ref"],
            existing["selector_after_ref"],
            _find_inventory_current_ref(store, existing),
            formed_under=formed_under,
        )
    elif kind == "candidate_build":
        rebuilt = build_candidate_build_receipt(
            store,
            existing["campaign_id"],
            _candidate_build_input(existing),
            formed_under=formed_under,
        )
    elif kind == "validation_gate":
        rebuilt = build_validation_gate_receipt(
            store,
            existing["campaign_id"],
            existing["validation_attempt_ref"],
            _validation_gate_input(existing),
            formed_under=formed_under,
        )
    elif kind == "candidate_delivery":
        rebuilt = build_candidate_delivery_receipt(
            store,
            existing["campaign_id"],
            existing["candidate_delivery_package_ref"],
            formed_under=formed_under,
        )
    else:
        raise ControlError(f"未知收据类型：{kind}")
    if canonical_json_bytes(rebuilt) != canonical_json_bytes(existing):
        raise ControlError(f"{kind} 收据与事实独立复算结果不匹配")
    return existing


def _find_inventory_current_ref(store: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    campaign_id = receipt["campaign_id"]
    restored_ref = receipt["deployment_fact_refs"][-1]
    matches: list[dict[str, Any]] = []
    for fact in store.list_facts(campaign_id, "deployment"):
        if (
            fact["fact_kind"] == "inventory_current_appended"
            and fact["payload"]["deployment_ref"] == restored_ref
            and fact["payload"]["production_ingress_inventory_ref"]
            == receipt["final_ingress_inventory_ref"]
            and fact["payload"]["egress_disposition_inventory_ref"]
            == receipt["final_egress_inventory_ref"]
        ):
            matches.append(store.fact_ref(fact))
    if len(matches) != 1:
        raise ControlError("ActivationReceipt 无法唯一关联最终 Inventory 事实")
    return matches[0]
