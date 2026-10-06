"""构建、验证、交付、晋升与激活收据的确定性生成和独立重放。

B1-14 在文件末尾加入前序证据导入：登记（复制对象闭包）、导入收据 finalizer、只读复核与消费端授权
（``authorize_cross_campaign_reference``），说明见该节开头。

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
    expect_rfc3339,
    parse_json_bytes,
    sha256_bytes,
    sha256_file,
)
from .contracts import (
    DEPLOYMENT_STAGES,
    IDENTITY_CONDITION_ITEMS,
    IDENTITY_CONDITION_SOURCE_FIELDS,
    OBJECT_KINDS,
    OBJECT_SCHEMA,
    OFFICIAL_EVIDENCE_OBJECT_KINDS,
    PREDECESSOR_IMPORT_RECEIPT_SCHEMA,
    RECEIPT_KINDS,
    RECEIPT_SCHEMA_VERSIONS,
    VALIDATION_COMPLETED_SCHEMA,
    campaign_identity_document,
    campaign_identity_sha256,
    campaign_is_v2,
    derive_identity_conditions,
    identity_differences,
    iter_fact_refs,
    iter_object_refs,
    parse_identity_document_text,
    receipt_is_v2,
    require_importable_object,
    validate_object_document,
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


_LAYER_FIELDS = ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256")


def tool_identity_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """由现算清单得到完整七项工具身份：三层摘要用 v3 引擎 ``compute_identity`` 在同一仓库根上计算。

    编排器不在受管树里（三层摘要无从计算）时失败关闭；``compute_identity`` 自己复算的整树指纹必须与清单一致，
    否则说明计算期间受管文件变了。
    """

    layers = _layer_digests(manifest)
    if layers is None:
        raise ControlError(
            f"编排器 {manifest['policy']['orchestrator']['file']} 不在受管树内，无法计算三层工具身份摘要"
        )
    return {
        "client": manifest["client"],
        "policy_version": manifest["policy_version"],
        "policy_sha256": manifest["policy_sha256"],
        **layers,
        "tool_files_sha256": manifest["tool_files_sha256"],
    }


def current_tool_identity(client: str) -> dict[str, Any]:
    """当前受管代码的完整七项工具身份（建 Campaign v2 与登记工具演进时用它核对声明值）。"""

    return tool_identity_from_manifest(current_tool_manifest(client))


def identity_drift(declared: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """声明的七项身份与现算身份不相等的字段：先列可现算四项，再列三层摘要（报错文案按此顺序）。"""

    return [
        key
        for key in (*_COMPUTABLE_IDENTITY_FIELDS, *_LAYER_FIELDS)
        if declared.get(key) != current.get(key)
    ]


def verify_campaign_tool_manifest(campaign: dict[str, Any]) -> dict[str, Any]:
    """Campaign v2 建立时核对冻结的工具身份与当前受管代码一致（设计 B1-12「Store 全包摘要的核对方式」）。

    ① 身份策略的 client、policy_version、policy_sha256 与冻结值相同；② 现算多根受管清单的指纹等于
    ``tool_identity.tool_files_sha256``；③ 三层摘要（wire／evidence／control）等于 v3 引擎 ``compute_identity``
    在同一仓库根上的现算值（B1-13 补齐）；④ 清单内 Store 口径条目按同一口径复算等于 ``tool_bundle_sha256``。
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
    drift = identity_drift(identity, tool_identity_from_manifest(manifest))
    if drift:
        raise ControlError(
            f"campaign.tool_identity 与 v3 身份引擎现算结果不一致：{'、'.join(drift)}"
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
    return {key: identity[key] for key in _LAYER_FIELDS}


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

        其他 Campaign 的事实没有本 Campaign 的形成期坐标可比：先过消费端核验（B1-14，未经授权即拒绝），
        获授权才跳过比较。
        """

        if not self.v2:
            return
        for reference in fact_refs:
            if reference["campaign_id"] != self.campaign["campaign_id"]:
                authorize_cross_campaign_reference(
                    store,
                    self.campaign,
                    reference,
                    owner_campaign_id=reference["campaign_id"],
                    label=f"{label} 引用了其他 Campaign 的事实",
                )
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
    *,
    idempotency_key: str | None = None,
    producer_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从严格 Acceptance 派生 VC-5 完成事实；门禁在追加前统一执行。

    v2 下 Acceptance 必须形成于当前候选 revision 与当前评估基线（交付判定由 validation_completed 门禁执行）。
    带幂等键时语义同 ``ControlStore.append_fact_outcome``（重试返回原事实引用）。
    """

    return finalize_validation_outcome(
        store,
        campaign_id,
        acceptance_ref,
        production_selector_after_ref,
        issued_at_utc,
        idempotency_key=idempotency_key,
        producer_run=producer_run,
    )["fact_ref"]


def finalize_validation_outcome(
    store: Any,
    campaign_id: str,
    acceptance_ref: dict[str, Any],
    production_selector_after_ref: dict[str, Any],
    issued_at_utc: str,
    *,
    idempotency_key: str | None = None,
    producer_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """同 ``finalize_validation``，返回 ``{"fact_ref", "reused"}``。"""

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
    return store.append_fact_outcome(
        campaign_id,
        "validation_completed",
        payload,
        issued_at_utc,
        idempotency_key=idempotency_key,
        producer_run=producer_run,
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

    前序导入收据（B1-14）同样按形成期重放：只用本 Store 内的导入登记、复制来的对象、目标 Campaign 与收据记录
    的源身份文档重建，从不打开前序 Store，因此前序 Store 搬走或删除都不影响重放；需要对照前序 Store 现状时用
    ``verify_predecessor_import``。
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
    elif kind == "predecessor_import":
        # 形成期重放：前序材料取收据自身记录的（源身份文档、导入事实记录），不解引用前序 Store。
        rebuilt = build_predecessor_import_receipt(
            store,
            existing["import_ref"],
            captured=_captured_from_receipt(existing),
            completed_at_utc=existing["completed_at_utc"],
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


# ---------------------------------------------------------------------------
# 前序证据导入（Claude 升级控制面 B1-14）
# ---------------------------------------------------------------------------
#
# 模型：复制、自含、只读。
# * 登记（``register_predecessor_import``）：只读打开前序 Store，核对前序 Campaign 身份摘要与两组七项声明，把
#   ``imported_objects``（必须对对象引用闭合）按依赖顺序逐字节复制进本 Store，再封存 ``predecessor_import`` 对象。
#   复制来的对象在本 Store 里是「休眠」的：没有成功的 read_only_reuse 导入收据，消费端一律拒绝跨 Campaign 引用。
# * 收据（``finalize_predecessor_import``）：再次只读打开前序 Store，从双方 Campaign 的冻结字段独立导出七项，与两组
#   声明逐项核对；核对每个导入对象与前序 Store 中的字节完全相同、每条导入事实在前序 Campaign 链上且摘要一致。
#   收据记录源 Campaign 身份文档（规范 JSON 文本）、两组导出七项、来源字段、逐项差异与导入事实记录，因而自含：
#   重放不打开前序 Store（形成期重放）。一份登记只签发一份收据，重复 finalize 返回原收据。
# * 前序 Store 一律只读打开（``ControlStore.open_read_only``）：任何写入路径（取锁）都直接拒绝。
# * ``predecessor.store_root`` 是登记时前序 Store 的绝对路径，只作出处：finalize 缺省从这里读；前序 Store 搬走后
#   finalize／verify 可显式给新位置（按内容核对，与路径无关）；重放从不解引用它。它不得指向本 Store。

PREDECESSOR_IMPORT_VERIFICATION_SCHEMA = "official-client-predecessor-import-verification/v1"
_FACT_REF_KEYS = ("campaign_id", "dimension", "sequence", "sha256")


def _object_key(reference: dict[str, Any]) -> tuple[str, str]:
    return str(reference["object_kind"]), str(reference["sha256"])


def _show(value: Any) -> str:
    """报错里展示七项取值：字符串带引号，不可证显示为 null。"""

    return canonical_json_bytes(value).decode("utf-8").strip()


def _reference_shape(reference: dict[str, Any]) -> str:
    keys = set(reference)
    if keys == {"object_kind", "sha256"}:
        return "object"
    if keys == set(_FACT_REF_KEYS):
        return "fact"
    if keys == {"receipt_kind", "sha256"}:
        return "receipt"
    raise ControlError(f"无法识别的引用：{sorted(keys)}")


def _import_payload(store: Any, import_ref: dict[str, Any]) -> dict[str, Any]:
    document = store.load_object(import_ref)
    if document["object_kind"] != "predecessor_import":
        raise ControlError("import_ref 必须引用 predecessor_import 对象")
    return document["payload"]


def predecessor_imports(store: Any, campaign_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """本 Store 里属于 ``campaign_id`` 的前序导入登记 ``[(引用, payload)]``，按引用摘要排序。"""

    entries: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for reference in store.list_object_refs("predecessor_import"):
        payload = store.load_object(reference)["payload"]
        if payload["campaign_id"] == campaign_id:
            entries.append((reference, payload))
    return entries


def predecessor_import_receipts(
    store: Any, import_ref: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """引用某份导入登记的全部导入收据 ``[(收据引用, 收据)]``。"""

    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for reference in store.list_receipt_refs("predecessor_import"):
        receipt = store.load_receipt(reference)
        if receipt["import_ref"] == import_ref:
            matches.append((reference, receipt))
    return matches


def find_predecessor_import_receipt(
    store: Any, import_ref: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """某份导入登记的导入收据；没有返回 None。一份登记只允许一份收据（finalize 幂等保证、重放复核），多份即失败关闭。"""

    matches = predecessor_import_receipts(store, import_ref)
    if len(matches) > 1:
        raise ControlError(f"同一前序导入登记存在多份导入收据，Store 已不一致：{import_ref['sha256']}")
    return matches[0] if matches else None


# ------------------------------- 消费端核验 -------------------------------


def _import_grant_denial(store: Any, import_ref: dict[str, Any]) -> str | None:
    """②③：登记有导入收据、收据重放通过、且 mode 为 read_only_reuse；满足返回 None，否则返回理由。"""

    found = find_predecessor_import_receipt(store, import_ref)
    if found is None:
        return f"② 导入登记 {import_ref['sha256']} 尚未 finalize，没有导入收据"
    receipt_ref, receipt = found
    try:
        replay_receipt(store, receipt_ref)
    except ControlError as error:
        return f"② 导入收据 {receipt_ref['sha256']} 重放失败：{error}"
    if receipt["mode"] != "read_only_reuse":
        return f"③ 导入收据 mode={receipt['mode']}，只有 read_only_reuse 授予正式引用权限"
    return None


def cross_campaign_denial(
    store: Any,
    campaign: dict[str, Any],
    reference: dict[str, Any],
    *,
    owner_campaign_id: str | None = None,
) -> str | None:
    """跨 Campaign 引用的消费端核验（设计 B1-14），获授权返回 None，否则返回拒绝理由。

    v2 Campaign 引用其他 Campaign 的证据，必须同时满足四项：
    ① 引用登记在本 Campaign 的某份 ``predecessor_import`` 里——对象引用在 ``imported_objects``、事实引用在
       ``imported_fact_refs``；被引用者自带所属 Campaign（``owner_campaign_id``）时必须就是该登记的前序 Campaign；
       收据不能导入，收据引用永不满足；
    ② 该登记有导入收据，且 ``replay_receipt`` 重放通过；
    ③ 收据 ``mode == read_only_reuse``（``regression_fixture`` 收据不授予任何正式引用权限）；
    ④ 被引用的是官方证据白名单内的对象类型（``OFFICIAL_EVIDENCE_OBJECT_KINDS``）；事实不在白名单内，所以跨
       Campaign 的事实引用永远不获授权，``imported_fact_refs`` 只是收据里的出处记录。
    多份登记列出同一引用时任一份满足②③即可；理由按 ①→②③（第一份登记）→④ 的顺序报告首个不满足项。
    v1 Campaign 没有导入，一律不授权。
    """

    if not campaign_is_v2(campaign):
        return "v1 Campaign 不接受跨 Campaign 引用"
    shape = _reference_shape(reference)
    if shape == "receipt":
        return "① 收据不能导入，任何 predecessor_import 都不登记收据引用"
    listing = "imported_objects" if shape == "object" else "imported_fact_refs"
    candidates = [
        import_ref
        for import_ref, payload in predecessor_imports(store, campaign["campaign_id"])
        if reference in payload[listing]
        and (owner_campaign_id is None or payload["predecessor"]["campaign_id"] == owner_campaign_id)
    ]
    if not candidates:
        owner = "" if owner_campaign_id is None else f"（被引用者属于 {owner_campaign_id}）"
        return f"① 引用不在本 Campaign 的任何 predecessor_import 登记中{owner}"
    reasons: list[str] = []
    for import_ref in candidates:
        reason = _import_grant_denial(store, import_ref)
        if reason is None:
            break
        reasons.append(reason)
    else:
        return reasons[0]
    if shape == "fact":
        return "④ 被引用的是事实，不是官方证据白名单内的对象类型"
    if reference["object_kind"] not in OFFICIAL_EVIDENCE_OBJECT_KINDS:
        return f"④ 对象类型 {reference['object_kind']} 不在官方证据白名单内"
    return None


def authorize_cross_campaign_reference(
    store: Any,
    campaign: dict[str, Any],
    reference: dict[str, Any],
    *,
    owner_campaign_id: str | None,
    label: str,
) -> None:
    """消费端核验入口：同 Campaign 直接放行；否则须满足 ``cross_campaign_denial`` 的四项，不满足即拒绝。

    ``owner_campaign_id`` 是被引用者所属的 Campaign（事实的 ``campaign_id``、对象 payload 的 ``campaign_id``）；没有
    所属字段的对象（EvidencePackage）传 None。v1 Campaign 原样抛出 ``label``（原报错文案逐字不变）；v2 在 ``label``
    后附未获授权的理由。
    """

    if owner_campaign_id is not None and owner_campaign_id == campaign["campaign_id"]:
        return
    if not campaign_is_v2(campaign):
        raise ControlError(label)
    reason = cross_campaign_denial(store, campaign, reference, owner_campaign_id=owner_campaign_id)
    if reason is not None:
        raise ControlError(f"{label}（未获前序导入授权：{reason}）")


def fixture_only_imported_objects(store: Any) -> set[tuple[str, str]]:
    """只作回归夹具（或尚未 finalize）的导入对象 ``{(object_kind, sha256)}``：其外部证据不计入本 Store 的外部核验。

    导入对象带着前序 Store 的外部证据绑定（相对前序 Store 的 external_root）。被 read_only_reuse 收据授权的对象是
    本 Campaign 的正式依赖，外部证据照常核验；其余导入对象若也不被本 Store 自有内容（Campaign、事实、导入收据
    以外的收据、未被任何导入登记列出的对象）沿对象引用触达，就只是夹具，外部证据随前序 Store 自己的重放核验。
    没有导入登记的 Store（全部历史 Store）直接返回空集，重放行为不变。
    """

    imports = [
        (reference, store.load_object(reference)["payload"])
        for reference in store.list_object_refs("predecessor_import")
    ]
    if not imports:
        return set()
    imported: set[tuple[str, str]] = set()
    authorized: set[tuple[str, str]] = set()
    for reference, payload in imports:
        keys = {_object_key(item) for item in payload["imported_objects"]}
        imported |= keys
        found = find_predecessor_import_receipt(store, reference)
        if found is not None and found[1]["mode"] == "read_only_reuse":
            authorized |= keys
    candidates = imported - authorized
    if not candidates:
        return set()
    roots: list[dict[str, Any]] = []
    for path in sorted((store.root / "campaigns").iterdir()):
        if path.suffix != ".json":
            continue
        campaign = store.load_campaign(path.stem)
        roots.extend(iter_object_refs(campaign))
        for fact in store.list_facts(campaign["campaign_id"]):
            roots.extend(iter_object_refs(fact["payload"]))
    for kind in RECEIPT_KINDS:
        if kind == "predecessor_import":
            continue
        for reference in store.list_receipt_refs(kind):
            roots.extend(iter_object_refs(store.load_receipt(reference)))
    for kind in sorted(OBJECT_KINDS - {"predecessor_import"}):
        for reference in store.list_object_refs(kind):
            if _object_key(reference) not in imported:
                roots.extend(iter_object_refs(store.load_object(reference)["payload"]))
    reachable: set[tuple[str, str]] = set()
    pending = list(roots)
    while pending:
        reference = pending.pop()
        key = _object_key(reference)
        if key in reachable:
            continue
        reachable.add(key)
        pending.extend(iter_object_refs(store.load_object(reference)["payload"]))
    return candidates - reachable


# ------------------------------- 前序 Store 读取 -------------------------------


def require_predecessor_root_outside(store: Any, root: Path) -> Path:
    """前序 Store 根必须是绝对路径，且不是本 Store、也不与本 Store 互相嵌套；返回解析后的路径。"""

    if not root.is_absolute():
        raise ControlError("前序 Store 根必须是绝对路径")
    resolved = root.resolve()
    own = Path(store.root)
    if resolved == own or own in resolved.parents or resolved in own.parents:
        raise ControlError(f"前序 Store 不得指向本 Store，也不得与本 Store 互相嵌套：{resolved}")
    return resolved


def open_predecessor_store(store: Any, root: Path) -> Any:
    """只读打开前序 Store（B2-09 可直接调用）：路径先过 ``require_predecessor_root_outside``，任何写入都被拒绝。"""

    from .store import ControlStore

    return ControlStore.open_read_only(require_predecessor_root_outside(store, Path(root)))


def predecessor_object_closure(
    predecessor: Any, roots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """前序 Store 中 ``roots`` 的对象引用闭包，按内容排序，可直接作 ``imported_objects``（B2-09 准备登记清单用）。"""

    seen: dict[tuple[str, str], dict[str, Any]] = {}
    pending = list(roots)
    while pending:
        reference = pending.pop()
        key = _object_key(reference)
        if key in seen:
            continue
        document = predecessor.load_object(reference)
        seen[key] = {"object_kind": key[0], "sha256": key[1]}
        pending.extend(iter_object_refs(document["payload"]))
    return sorted(seen.values(), key=canonical_sha256)


def _load_predecessor_campaign(predecessor: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """读前序 Campaign（加载即复算其 identity_sha256），并核对与登记声明的身份摘要一致。"""

    declared = payload["predecessor"]
    campaign = predecessor.load_campaign(declared["campaign_id"])
    if campaign["identity_sha256"] != declared["campaign_identity_sha256"]:
        raise ControlError(
            "前序 Campaign 的 identity_sha256 与导入登记声明的不一致："
            f"声明 {declared['campaign_identity_sha256']}，实际 {campaign['identity_sha256']}"
        )
    return campaign


def check_import_declarations(
    payload: dict[str, Any], source: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    """两组七项声明与独立导出值逐项核对（导入 finalizer 的核心判据，登记时也先核一遍以便尽早失败）。

    ``source`` 是源 Campaign（或收据记录的源身份文档），``target`` 是目标 Campaign。不可证的项导出为 null，声明也
    必须是 null；任一项不符即拒绝并列出全部不符项。read_only_reuse 还要求七项全部可证且逐项相等（差异为空）；
    regression_fixture 不要求相等，差异与不可证项原样进入收据的 ``identity_differences``。
    """

    source_conditions, _source_unprovable = derive_identity_conditions(source)
    target_conditions, _target_unprovable = derive_identity_conditions(target)
    mismatches: list[str] = []
    for field, declared, derived in (
        ("source_identity_conditions", payload["source_identity_conditions"], source_conditions),
        ("identity_conditions", payload["identity_conditions"], target_conditions),
    ):
        for item in IDENTITY_CONDITION_ITEMS:
            if declared[item] != derived[item]:
                mismatches.append(
                    f"{field}.{item} 声明 {_show(declared[item])}，由 "
                    f"{IDENTITY_CONDITION_SOURCE_FIELDS[item]} 独立导出 {_show(derived[item])}"
                )
    if mismatches:
        raise ControlError("前序导入的七项身份声明与冻结字段独立导出的不一致：" + "；".join(mismatches))
    differences = identity_differences(source_conditions, target_conditions)
    if payload["mode"] == "read_only_reuse" and differences:
        raise ControlError(
            "read_only_reuse 要求七项全部可证且逐项相等："
            + "；".join(
                f"{item['item']} {item['reason']}（源 {_show(item['source'])}，目标 {_show(item['target'])}）"
                for item in differences
            )
        )
    return {
        "source_conditions": source_conditions,
        "target_conditions": target_conditions,
        "identity_differences": differences,
    }


def _imported_object_plan(
    predecessor: Any, payload: dict[str, Any]
) -> list[tuple[dict[str, Any], bytes]]:
    """在复制任何对象之前核完整个清单：每个对象可导入且属于前序 Campaign（``require_importable_object``）、清单对
    对象引用闭合；读出原始字节，按依赖顺序（被引用者在前）排列。"""

    predecessor_id = payload["predecessor"]["campaign_id"]
    listed = {_object_key(reference): reference for reference in payload["imported_objects"]}
    nested: dict[tuple[str, str], list[tuple[str, str]]] = {}
    contents: dict[tuple[str, str], bytes] = {}
    for key, reference in listed.items():
        content = predecessor.object_bytes(reference)
        document = parse_json_bytes(content, "前序对象")
        require_importable_object(document, predecessor_campaign_id=predecessor_id)
        children = [_object_key(item) for item in iter_object_refs(document["payload"])]
        missing = sorted(set(children) - set(listed))
        if missing:
            raise ControlError(
                f"predecessor_import.imported_objects 未对对象引用闭合：{key[0]}/{key[1]} 引用了未列入的 "
                + "、".join(f"{kind}/{digest}" for kind, digest in missing)
            )
        nested[key] = children
        contents[key] = content
    order: list[tuple[str, str]] = []
    placed: set[tuple[str, str]] = set()
    for root in sorted(listed):
        stack: list[tuple[tuple[str, str], bool]] = [(root, False)]
        while stack:
            key, expanded = stack.pop()
            if key in placed:
                continue
            if expanded:
                placed.add(key)
                order.append(key)
                continue
            stack.append((key, True))
            stack.extend((child, False) for child in nested[key] if child not in placed)
    return [(listed[key], contents[key]) for key in order]


def register_predecessor_import(store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """登记前序证据导入（B2-09 两条导入路径的第一步），返回登记结果。

    步骤：① 合同与目标侧判据（``WorkflowGates.validate_predecessor_import_target``：v2、client、批准时间、当前有效
    工具身份、store_root 不是本 Store、前序 Campaign ID 不与本地重名）；② 只读打开前序 Store，核对前序 Campaign 身份
    摘要与声明一致、两组七项声明与独立导出值一致（read_only_reuse 另要求逐项相等），导入事实都在前序 Campaign 链上；
    ③ 核对 ``imported_objects`` 对对象引用闭合，按依赖顺序逐字节复制进本 Store（同内容已存在即复用）；④ 封存
    ``predecessor_import`` 对象（同内容已封存即复用）。全部核对在复制之前完成。登记本身不授予任何引用权限，要等
    ``finalize_predecessor_import`` 签发 read_only_reuse 收据。

    返回 ``{"import_ref", "reused", "copied_objects", "reused_objects"}``：``reused`` 指登记对象本身已存在（重跑同一
    步骤），两个清单分别是本次新复制与本 Store 已有同内容的导入对象。
    """

    from .gates import WorkflowGates

    validate_object_document(
        {"schema_version": OBJECT_SCHEMA, "object_kind": "predecessor_import", "payload": payload}
    )
    # 目标侧判据（v2、client、批准时间、当前有效工具、store_root 不是本 Store、前序 ID 不与本地重名）先于读前序。
    campaign = WorkflowGates(store).validate_predecessor_import_target(payload, new=True)
    predecessor = open_predecessor_store(store, Path(payload["predecessor"]["store_root"]))
    source = _load_predecessor_campaign(predecessor, payload)
    check_import_declarations(payload, source, campaign)
    for reference in payload["imported_fact_refs"]:
        predecessor.load_fact(reference)
    copied: list[dict[str, Any]] = []
    reused_objects: list[dict[str, Any]] = []
    for reference, content in _imported_object_plan(predecessor, payload):
        if store.import_object(reference, content):
            reused_objects.append(reference)
        else:
            copied.append(reference)
    outcome = store.seal_object_outcome("predecessor_import", payload, allow_existing=True)
    return {
        "import_ref": outcome["object_ref"],
        "reused": outcome["reused"],
        "copied_objects": copied,
        "reused_objects": reused_objects,
    }


def capture_predecessor_material(
    store: Any, import_ref: dict[str, Any], *, predecessor_root: Path | None = None
) -> dict[str, Any]:
    """finalize／verify 从前序 Store 只读采集并核对的材料。

    缺省从登记的 ``predecessor.store_root`` 读，前序 Store 搬走后用 ``predecessor_root`` 给新位置。核对：前序 Campaign
    身份摘要等于登记声明；每个导入对象在前序 Store 中存在、摘要一致，且与本 Store 中的副本逐字节相同；每条导入
    事实在前序 Campaign 链上、摘要一致。返回 ``{"source_campaign_document", "imported_facts", "predecessor_root"}``，
    其中身份文档是规范 JSON 文本（不含末尾换行），导入事实是 ``{campaign_id, dimension, sequence, sha256, fact_kind}``。
    """

    payload = _import_payload(store, import_ref)
    root = Path(payload["predecessor"]["store_root"]) if predecessor_root is None else Path(predecessor_root)
    predecessor = open_predecessor_store(store, root)
    source = _load_predecessor_campaign(predecessor, payload)
    for reference in payload["imported_objects"]:
        if predecessor.object_bytes(reference) != store.object_bytes(reference):
            raise ControlError(f"导入对象与前序 Store 中的字节不一致：{reference}")
    facts: list[dict[str, Any]] = []
    for reference in payload["imported_fact_refs"]:
        fact = predecessor.load_fact(reference)
        facts.append({**{key: reference[key] for key in _FACT_REF_KEYS}, "fact_kind": fact["fact_kind"]})
    document = canonical_json_bytes(campaign_identity_document(source)).decode("utf-8")
    return {
        "source_campaign_document": document[:-1],
        "imported_facts": facts,
        "predecessor_root": str(predecessor.root),
    }


def _captured_from_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """重放用的前序材料：取收据自身记录的，不打开前序 Store。"""

    return {
        "source_campaign_document": receipt["source_campaign_document"],
        "imported_facts": receipt["imported_facts"],
    }


def build_predecessor_import_receipt(
    store: Any,
    import_ref: dict[str, Any],
    *,
    captured: dict[str, Any],
    completed_at_utc: str,
    formed_under: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """由导入登记、目标 Campaign 与前序材料确定性地生成导入收据（finalize 与重放共用）。

    ``captured`` 在 finalize 时来自 ``capture_predecessor_material``（读前序 Store），重放时取收据自身记录的；
    ``formed_under`` 只在重放时传入。源身份文档必须是登记声明的前序 Campaign（ID 一致、复算身份摘要等于声明），
    七项从源身份文档与目标 Campaign 独立导出后与两组声明逐项核对（``check_import_declarations``）。
    """

    expect_rfc3339(completed_at_utc, "completed_at_utc")
    payload = _import_payload(store, import_ref)
    campaign = store.load_campaign(payload["campaign_id"])
    if not campaign_is_v2(campaign):
        raise ControlError("前序证据导入收据只属于 Campaign v2")
    producer = _receipt_producer(store, campaign, formed_under)
    if payload["client"] != campaign["client"]:
        raise ControlError("前序证据导入的 client 与 Campaign 不一致")
    declared = payload["predecessor"]
    document = parse_identity_document_text(
        captured["source_campaign_document"], "source_campaign_document"
    )
    if (
        document["campaign_id"] != declared["campaign_id"]
        or campaign_identity_sha256(document) != declared["campaign_identity_sha256"]
    ):
        raise ControlError("前序 Campaign 身份文档与导入登记声明的前序 Campaign 不符")
    derived = check_import_declarations(payload, document, campaign)
    for reference in payload["imported_objects"]:
        store.load_object(reference)
    facts = captured["imported_facts"]
    if [
        {key: item[key] for key in _FACT_REF_KEYS} for item in facts
    ] != payload["imported_fact_refs"]:
        raise ControlError("导入事实记录与导入登记的 imported_fact_refs 不一致")
    if datetime.fromisoformat(completed_at_utc.replace("Z", "+00:00")) < datetime.fromisoformat(
        payload["approved_at_utc"].replace("Z", "+00:00")
    ):
        raise ControlError("导入收据完成时间早于导入批准时间")
    receipt: dict[str, Any] = {
        "schema_version": PREDECESSOR_IMPORT_RECEIPT_SCHEMA,
        "campaign_id": payload["campaign_id"],
        "client": payload["client"],
        "import_ref": {"object_kind": "predecessor_import", "sha256": import_ref["sha256"]},
        "mode": payload["mode"],
        "predecessor": dict(declared),
        "source_campaign_document": captured["source_campaign_document"],
        "target_campaign_identity_sha256": campaign["identity_sha256"],
        "source_conditions": derived["source_conditions"],
        "target_conditions": derived["target_conditions"],
        "condition_source_fields": dict(IDENTITY_CONDITION_SOURCE_FIELDS),
        "identity_differences": derived["identity_differences"],
        "imported_objects": payload["imported_objects"],
        "imported_facts": facts,
        "approved_by": payload["approved_by"],
        "completed_at_utc": completed_at_utc,
        "producer_tool_sha256": producer["tool_sha256"],
        "formed_under": producer["formed_under"],
        "identity_sha256": "",
    }
    receipt["identity_sha256"] = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    validate_receipt_document(receipt, "predecessor_import")
    return receipt


def finalize_predecessor_import(
    store: Any,
    import_ref: dict[str, Any],
    completed_at_utc: str,
    *,
    predecessor_root: Path | None = None,
) -> dict[str, Any]:
    """同 ``finalize_predecessor_import_outcome``，只返回收据引用。"""

    return finalize_predecessor_import_outcome(
        store, import_ref, completed_at_utc, predecessor_root=predecessor_root
    )["receipt_ref"]


def finalize_predecessor_import_outcome(
    store: Any,
    import_ref: dict[str, Any],
    completed_at_utc: str,
    *,
    predecessor_root: Path | None = None,
) -> dict[str, Any]:
    """签发前序导入收据（B2-09 两条导入路径的第二步），返回 ``{"receipt_ref", "reused"}``。

    幂等：同一登记已有收据时，重放核对后原样返回（``reused: true``），不论这次给的完成时间、当前工具身份或形成期
    坐标是否已变化——一份登记只对应一份收据。没有时按当前形成期坐标签发：只读采集并核对前序材料、独立导出两组
    七项核对声明，写入收据。按登记串行，两个并发 finalize 不会各写一份。
    """

    _import_payload(store, import_ref)
    with store._lock(f"predecessor-import-{import_ref['sha256']}"):
        existing = find_predecessor_import_receipt(store, import_ref)
        if existing is not None:
            replay_receipt(store, existing[0])
            return {"receipt_ref": existing[0], "reused": True}
        captured = capture_predecessor_material(
            store, import_ref, predecessor_root=predecessor_root
        )
        receipt = build_predecessor_import_receipt(
            store, import_ref, captured=captured, completed_at_utc=completed_at_utc
        )
        return store.write_receipt_outcome("predecessor_import", receipt, allow_existing=True)


def verify_predecessor_import(
    store: Any, import_ref: dict[str, Any], *, predecessor_root: Path | None = None
) -> dict[str, Any]:
    """对照前序 Store 现状复核一份已签发的导入收据（只读；前序 Store 搬走后用 ``predecessor_root`` 给新位置）。

    重放本身不打开前序 Store；这里额外证明收据记录的源身份文档与导入事实记录同前序 Store 现状一致、复制来的对象与
    前序 Store 中的字节仍完全相同。按内容核对，与前序 Store 当前所在路径无关。
    """

    found = find_predecessor_import_receipt(store, import_ref)
    if found is None:
        raise ControlError("导入登记尚未 finalize，没有可复核的导入收据")
    receipt_ref, receipt = found
    replay_receipt(store, receipt_ref)
    captured = capture_predecessor_material(store, import_ref, predecessor_root=predecessor_root)
    if captured["source_campaign_document"] != receipt["source_campaign_document"]:
        raise ControlError("前序 Store 中的 Campaign 身份文档与导入收据记录的不一致")
    if captured["imported_facts"] != receipt["imported_facts"]:
        raise ControlError("前序 Store 中的导入事实与导入收据记录的不一致")
    return {
        "schema_version": PREDECESSOR_IMPORT_VERIFICATION_SCHEMA,
        "result": "passed",
        "import_ref": receipt["import_ref"],
        "receipt_ref": receipt_ref,
        "mode": receipt["mode"],
        "recorded_store_root": receipt["predecessor"]["store_root"],
        "verified_store_root": captured["predecessor_root"],
        "imported_objects": len(receipt["imported_objects"]),
        "imported_facts": len(receipt["imported_facts"]),
        "identity_differences": receipt["identity_differences"],
    }
