"""Claude 升级的两条前序证据导入路径（Claude 升级控制面设计 B2-09）。

``claude_upgrade.py import-predecessor-evidence --campaign-dir <本 Campaign> --predecessor-store <前序 Store 根>
--predecessor-campaign <前序 Store Campaign ID> --mode read_only_reuse|regression_fixture --approved-by <批准人>``
把前序 Store 里一个 Campaign 的 VC-1 官方证据导入本 Campaign 的受管 Store（``<campaign_dir>/control-store``），产出
B1-14 的 ``predecessor_import`` 登记对象与导入收据。两种模式共用同一条链：

1. 选取前序 Campaign 的 VC-1 官方证据：最新一条 ``evidence_recorded`` 事实引用的 EvidencePackage 及其对象闭包，
   该事实与它引用的 ``discovery_recorded`` 事实作为核验过的出处（``imported_fact_refs``）。
2. 两组七项身份声明一律从双方 Store Campaign 的冻结字段独立导出（``contracts.derive_identity_conditions``），编排器
   从不替申请方「声明」身份；Store 的 finalizer 再独立复算一次，任一项不符即拒绝。
3. 同内容的登记已存在即沿用（不看批准人、批准时间与前序 Store 当前位置），没有才按当前时间登记；收据一份登记只签一份，
   重跑返回原收据。前序 Store 一律只读打开（B1-14 ``open_predecessor_store``），搬家后用新位置重跑即可。
4. 签发后对照前序 Store 现状复核（``verify_predecessor_import``），并逐个报告导入对象能否被正式引用。

两种模式的差别：

* ``regression_fixture``：只登记夹具收据。逐项差异与不可证项写进收据 ``identity_differences``（例如 2.1.226 x86_64
  Store 导入 arm64 Campaign：平台 amd64→arm64、组织与模型目录两项不可证），导入对象不授予任何正式引用权限。
* ``read_only_reuse``：只在 Formal Campaign、且总计划 ``predecessor`` 冻结了这个前序 Campaign 时允许（复用决定必须在
  VC-0 建 Campaign 时冻结，不能事后挑选）；Store 要求七项全部可证且逐项相等才签发。签发后 EvidencePackage 获得正式
  引用权限，``vc1_reuse_plan`` 给出 VC-1 零请求封存的输入：``reuse_item_ids`` 是总计划全部 official Job，
  ``execute_item_ids`` 为空。零请求封存与计时账本三条事件在 seal 链（B2-07）写成 VC-1 checkpoint 之后接上。

总计划 ``predecessor`` 字段（B2-09 定义）：``null``，或 ``{"mode": "read_only_reuse", "campaign_id": …,
"campaign_identity_sha256": …}``。只冻结前序 Campaign 的身份，不冻结 Store 位置——位置在导入时由
``--predecessor-store`` 给出，按内容核对。回归夹具不影响 VC-1 的证据决定，不在总计划里声明。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层；不导入
``claude_upgrade``（锁、租约与总计划读取由编排器入口转调前完成）。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.contracts import (
    OFFICIAL_EVIDENCE_OBJECT_KINDS,
    PREDECESSOR_IMPORT_MODES,
    derive_identity_conditions,
)
from tools.official_client_control.errors import ControlError

IMPORT_SUMMARY_SCHEMA = "claude-upgrade-predecessor-import/v1"
VC1_REUSE_PLAN_SCHEMA = "claude-upgrade-vc1-reuse-plan/v1"
READ_ONLY_REUSE = "read_only_reuse"
REGRESSION_FIXTURE = "regression_fixture"
PREDECESSOR_BINDING_FIELDS = frozenset({"mode", "campaign_id", "campaign_identity_sha256"})
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# 判定两份登记是不是同一次导入时比较的字段：批准人、批准时间与前序 Store 当前位置都不算（重跑与搬家后照样沿用）。
_REGISTRATION_KEY_FIELDS = (
    "client",
    "campaign_id",
    "mode",
    "imported_objects",
    "imported_fact_refs",
    "identity_conditions",
    "source_identity_conditions",
)


# ---------------------------------------------------------------------------
# 总计划 predecessor 字段
# ---------------------------------------------------------------------------


def validate_predecessor_binding(value: Any, *, campaign_id: str) -> dict[str, Any] | None:
    """总计划 ``predecessor`` 的形状：``null``，或冻结前序 Campaign 身份的只读复用声明。

    只核形状（读总计划时调用，不打开前序 Store）；与前序 Store 的实际身份由导入时核对。
    """

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != PREDECESSOR_BINDING_FIELDS:
        raise ConfigurationError(
            "Claude Campaign 总计划 predecessor 必须为 null 或 {mode, campaign_id, campaign_identity_sha256}。"
        )
    if value.get("mode") != READ_ONLY_REUSE:
        raise ConfigurationError(
            "总计划 predecessor 只声明前序只读复用（mode=read_only_reuse）；回归夹具不改变 VC-1 证据决定，不在总计划声明。"
        )
    predecessor_id = value.get("campaign_id")
    if not isinstance(predecessor_id, str) or not SAFE_ID_RE.fullmatch(predecessor_id):
        raise ConfigurationError("总计划 predecessor.campaign_id 非法。")
    if predecessor_id == campaign_id:
        raise ConfigurationError("总计划 predecessor 不得指向本 Campaign 自身。")
    if not SHA256_RE.fullmatch(str(value.get("campaign_identity_sha256", ""))):
        raise ConfigurationError("总计划 predecessor.campaign_identity_sha256 非法。")
    return dict(value)


# ---------------------------------------------------------------------------
# 命令行参数
# ---------------------------------------------------------------------------


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("路径必须是绝对路径")
    return path


def add_import_arguments(parser: argparse.ArgumentParser) -> None:
    """``import-predecessor-evidence`` 的参数。"""

    parser.add_argument("--campaign-dir", "--campaign", dest="campaign_dir", type=_absolute_path, required=True)
    parser.add_argument(
        "--predecessor-store", type=_absolute_path, required=True, help="前序受管 Store 根（只读打开，按内容核对）"
    )
    parser.add_argument("--predecessor-campaign", required=True, help="前序 Store 里被导入的 Campaign ID")
    parser.add_argument(
        "--mode",
        choices=PREDECESSOR_IMPORT_MODES,
        required=True,
        help="read_only_reuse：VC-1 零请求复用前序官方证据；regression_fixture：只登记回归夹具，不授予正式引用权限",
    )
    parser.add_argument("--approved-by", required=True, help="批准人（新登记写入导入对象；沿用已有登记时以其记录为准）")


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _store_step(label: str, action: Callable[[], Any]) -> Any:
    """执行一步 Store 操作，把 ``ControlError`` 转成编排器的 ``ConfigurationError``（报错附步骤名）。"""

    try:
        return action()
    except ControlError as error:
        raise ConfigurationError(f"{label}失败：{error}") from error


def _require_approver(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128 or any(ord(character) < 0x20 for character in text):
        raise ConfigurationError("--approved-by 必须是不含控制字符的非空标识（不超过 128 字符）。")
    return text


def select_official_evidence(predecessor: Any, campaign_id: str) -> dict[str, Any]:
    """前序 Campaign 的 VC-1 官方证据：最新一条 ``evidence_recorded`` 及其 EvidencePackage 与 Discovery 出处。"""

    facts = [fact for fact in predecessor.list_facts(campaign_id, "evidence") if fact["fact_kind"] == "evidence_recorded"]
    if not facts:
        raise ConfigurationError(f"前序 Campaign {campaign_id} 没有官方证据事实 evidence_recorded，无可导入的 VC-1 证据。")
    fact = facts[-1]
    discovery_ref = fact["payload"]["discovery_fact_ref"]
    if discovery_ref.get("campaign_id") != campaign_id:
        raise ConfigurationError("前序 EvidenceFact 引用了其他 Campaign 的 Discovery，不能作为导入出处。")
    return {
        "evidence_fact_ref": predecessor.fact_ref(fact),
        "discovery_fact_ref": dict(discovery_ref),
        "evidence_package_ref": dict(fact["payload"]["evidence_package_ref"]),
    }


def _registration_key(payload: Mapping[str, Any]) -> dict[str, Any]:
    key = {field: payload[field] for field in _REGISTRATION_KEY_FIELDS}
    key["predecessor"] = {
        "campaign_id": payload["predecessor"]["campaign_id"],
        "campaign_identity_sha256": payload["predecessor"]["campaign_identity_sha256"],
    }
    return key


def _same_predecessor(payload: Mapping[str, Any], mode: str, campaign_id: str, identity_sha256: str) -> bool:
    return (
        payload["mode"] == mode
        and payload["predecessor"]["campaign_id"] == campaign_id
        and payload["predecessor"]["campaign_identity_sha256"] == identity_sha256
    )


def _existing_registration(store: Any, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """本 Campaign 对同一前序 Campaign、同一模式的已有登记：内容相同返回它；内容不同即拒绝（一份前序一种模式只登记一次）。"""

    predecessor = payload["predecessor"]
    matches = [
        (reference, existing)
        for reference, existing in store_receipts.predecessor_imports(store, payload["campaign_id"])
        if _same_predecessor(existing, payload["mode"], predecessor["campaign_id"], predecessor["campaign_identity_sha256"])
    ]
    if not matches:
        return None
    expected = canonical_sha256(_registration_key(payload))
    same = [item for item in matches if canonical_sha256(_registration_key(item[1])) == expected]
    if len(same) != len(matches) or len(same) != 1:
        raise ConfigurationError(
            "本 Campaign 已为同一前序 Campaign 登记过同一模式的导入，但导入内容与本次不同（前序 Campaign 在登记后又"
            "追加了证据，或身份已变化）；一份前序一种模式只登记一次，不能另起一份。"
        )
    return same[0]


def build_import_payload(
    store_campaign: Mapping[str, Any],
    predecessor: Any,
    source: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    mode: str,
    approved_by: str,
    approved_at_utc: str,
) -> dict[str, Any]:
    """B1-14 ``predecessor_import`` payload：导入对象取 EvidencePackage 的对象闭包，两组七项身份从双方冻结字段导出。"""

    return {
        "client": "claude",
        "campaign_id": store_campaign["campaign_id"],
        "predecessor": {
            "store_root": str(predecessor.root),
            "campaign_id": source["campaign_id"],
            "campaign_identity_sha256": source["identity_sha256"],
        },
        "imported_objects": store_receipts.predecessor_object_closure(predecessor, [dict(evidence["evidence_package_ref"])]),
        "imported_fact_refs": sorted(
            [dict(evidence["discovery_fact_ref"]), dict(evidence["evidence_fact_ref"])], key=canonical_sha256
        ),
        "identity_conditions": derive_identity_conditions(dict(store_campaign))[0],
        "source_identity_conditions": derive_identity_conditions(dict(source))[0],
        "mode": mode,
        "approved_by": approved_by,
        "approved_at_utc": approved_at_utc,
    }


def _formal_reference_report(store: Any, store_campaign: Mapping[str, Any], objects: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """逐个导入对象报告能否被本 Campaign 正式引用（消费端核验四项，见 ``receipts.cross_campaign_denial``）。"""

    report = []
    for reference in objects:
        denial = store_receipts.cross_campaign_denial(store, dict(store_campaign), dict(reference))
        report.append({"object_ref": dict(reference), "authorized": denial is None, "denial": denial})
    return report


def import_predecessor_evidence(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    predecessor_store: Path,
    predecessor_campaign: str,
    mode: str,
    approved_by: str,
    now: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """执行一次前序证据导入（调用方已在 Campaign 锁内、确认没有未释放租约并读好总计划），返回导入摘要。"""

    if mode not in PREDECESSOR_IMPORT_MODES:
        raise ConfigurationError(f"--mode 非法：{mode!r}")
    if not isinstance(predecessor_campaign, str) or not SAFE_ID_RE.fullmatch(predecessor_campaign):
        raise ConfigurationError("--predecessor-campaign 非法。")
    approver = _require_approver(approved_by)
    binding = validate_predecessor_binding(manifest.get("predecessor"), campaign_id=str(manifest["campaign_id"]))
    if mode == READ_ONLY_REUSE:
        if manifest.get("campaign_mode") != "formal":
            raise ConfigurationError("read_only_reuse 只用于 Formal Campaign：preflight_only 不进入正式证据链。")
        if binding is None:
            raise ConfigurationError(
                "总计划没有冻结前序只读复用（predecessor 为 null）：复用决定必须在 VC-0 建 Campaign 时冻结；"
                "只作回归对照请用 --mode regression_fixture。"
            )
        if binding["campaign_id"] != predecessor_campaign:
            raise ConfigurationError(
                f"--predecessor-campaign {predecessor_campaign} 不是总计划冻结的前序 Campaign {binding['campaign_id']}。"
            )
        # VC-1 复用计划要复用全部 official Job：总计划没有可复用的 Job 时在写 Store 之前拒绝，不留下用不上的授权。
        official_job_ids(manifest)

    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    predecessor = _store_step("只读打开前序 Store", lambda: store_receipts.open_predecessor_store(store, Path(predecessor_store)))
    source = _store_step("读取前序 Campaign", lambda: predecessor.load_campaign(predecessor_campaign))
    if binding is not None and mode == READ_ONLY_REUSE and source["identity_sha256"] != binding["campaign_identity_sha256"]:
        raise ConfigurationError(
            "前序 Campaign 的身份摘要与总计划冻结的不一致：总计划 "
            f"{binding['campaign_identity_sha256']}，前序 Store {source['identity_sha256']}（不能伪称同一前序）。"
        )
    evidence = _store_step("选取前序官方证据", lambda: select_official_evidence(predecessor, predecessor_campaign))
    payload = _store_step(
        "构造导入登记",
        lambda: build_import_payload(
            store_campaign, predecessor, source, evidence, mode=mode, approved_by=approver, approved_at_utc=now()
        ),
    )

    existing = _store_step("查找已有导入登记", lambda: _existing_registration(store, payload))
    if existing is None:
        registered = _store_step("登记前序导入", lambda: store_receipts.register_predecessor_import(store, payload))
        import_ref = registered["import_ref"]
        registration = {
            "reused": registered["reused"],
            "copied_objects": len(registered["copied_objects"]),
            "reused_objects": len(registered["reused_objects"]),
        }
        recorded = payload
    else:
        import_ref, recorded = existing
        registration = {"reused": True, "copied_objects": 0, "reused_objects": len(recorded["imported_objects"])}

    outcome = _store_step(
        "签发导入收据",
        lambda: store_receipts.finalize_predecessor_import_outcome(
            store, import_ref, now(), predecessor_root=predecessor.root
        ),
    )
    verification = _store_step(
        "对照前序 Store 复核导入收据",
        lambda: store_receipts.verify_predecessor_import(store, import_ref, predecessor_root=predecessor.root),
    )
    receipt = store.load_receipt(outcome["receipt_ref"])
    if receipt["mode"] != mode:
        raise ConfigurationError("导入收据的模式与本次请求不一致，Store 已不一致。")
    references = _store_step(
        "核对导入对象的正式引用权限",
        lambda: _formal_reference_report(store, store_campaign, recorded["imported_objects"]),
    )
    package_ref = dict(evidence["evidence_package_ref"])
    package_authorized = next(item["authorized"] for item in references if item["object_ref"] == package_ref)
    if mode == READ_ONLY_REUSE and not package_authorized:
        raise ConfigurationError("read_only_reuse 收据已签发，EvidencePackage 却仍未获正式引用授权，Store 已不一致。")
    summary = {
        "schema_version": IMPORT_SUMMARY_SCHEMA,
        "campaign_id": manifest["campaign_id"],
        "mode": mode,
        "predecessor": {
            "store_root": str(predecessor.root),
            "campaign_id": source["campaign_id"],
            "campaign_identity_sha256": source["identity_sha256"],
        },
        "import_ref": import_ref,
        "registration": {**registration, "approved_by": recorded["approved_by"], "approved_at_utc": recorded["approved_at_utc"]},
        "receipt_ref": outcome["receipt_ref"],
        "receipt_reused": outcome["reused"],
        "imported_objects": recorded["imported_objects"],
        "imported_facts": receipt["imported_facts"],
        "evidence_package_ref": package_ref,
        "source_conditions": receipt["source_conditions"],
        "target_conditions": receipt["target_conditions"],
        "identity_differences": receipt["identity_differences"],
        "verification": verification["result"],
        "formal_references": references,
        "vc1_reuse": None,
        "live_request_count": 0,
    }
    if mode == READ_ONLY_REUSE:
        summary["vc1_reuse"] = vc1_reuse_plan_from_store(store, store_campaign, manifest)
    return summary


# ---------------------------------------------------------------------------
# read_only_reuse 的 VC-1 零请求封存输入（供 seal 链 B2-07 与 VC 批次入口 B3-01 调用）
# ---------------------------------------------------------------------------


def official_job_ids(manifest: Mapping[str, Any]) -> list[str]:
    """总计划冻结的全部 official Job ID（按冻结顺序）。"""

    jobs = manifest.get("jobs")
    if not isinstance(jobs, list):
        raise ConfigurationError("总计划 jobs 必须是数组。")
    identifiers = [str(job.get("id")) for job in jobs if isinstance(job, Mapping) and job.get("phase") == "official"]
    if not identifiers or len(identifiers) != len(set(identifiers)) or any(not SAFE_ID_RE.fullmatch(item) for item in identifiers):
        raise ConfigurationError("总计划没有可复用的 official Job，或 Job ID 非法、重复。")
    return identifiers


def find_reuse_import(store: Any, store_campaign: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    """总计划冻结的前序只读复用对应的导入登记与收据（收据须重放通过、mode 为 read_only_reuse）。"""

    matches = [
        (reference, payload)
        for reference, payload in store_receipts.predecessor_imports(store, str(store_campaign["campaign_id"]))
        if _same_predecessor(payload, READ_ONLY_REUSE, binding["campaign_id"], binding["campaign_identity_sha256"])
    ]
    if not matches:
        raise ConfigurationError("总计划冻结了前序只读复用，但 Store 里还没有对应的导入登记：先执行 import-predecessor-evidence。")
    if len(matches) > 1:
        raise ConfigurationError("同一前序 Campaign 存在多份只读复用登记，无法确定 VC-1 复用来源。")
    import_ref, payload = matches[0]
    found = store_receipts.find_predecessor_import_receipt(store, import_ref)
    if found is None:
        raise ConfigurationError("前序只读复用登记尚未 finalize：重跑 import-predecessor-evidence 签发导入收据。")
    receipt_ref, receipt = found
    store_receipts.replay_receipt(store, receipt_ref)
    if receipt["mode"] != READ_ONLY_REUSE or receipt["identity_differences"]:
        raise ConfigurationError("前序只读复用的导入收据不是 read_only_reuse 或身份不一致。")
    return {"import_ref": import_ref, "payload": payload, "receipt_ref": receipt_ref, "receipt": receipt}


def vc1_reuse_plan_from_store(store: Any, store_campaign: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """read_only_reuse 的 VC-1 零请求封存输入：复用全部 official Job，绑定已授权的前序 EvidencePackage 与出处事实。"""

    binding = validate_predecessor_binding(manifest.get("predecessor"), campaign_id=str(manifest["campaign_id"]))
    if binding is None:
        raise ConfigurationError("总计划没有冻结前序只读复用，VC-1 不能零请求复用前序证据。")
    found = _store_step("查找前序只读复用导入", lambda: find_reuse_import(store, store_campaign, binding))
    payload = found["payload"]
    packages = [item for item in payload["imported_objects"] if item["object_kind"] == "evidence_package"]
    evidence_facts = [item for item in payload["imported_fact_refs"] if item["dimension"] == "evidence"]
    if len(packages) != 1 or len(evidence_facts) != 1:
        raise ConfigurationError("前序只读复用登记必须恰好导入一份 EvidencePackage 与一条 EvidenceFact。")
    denial = _store_step(
        "核对 EvidencePackage 的正式引用权限",
        lambda: store_receipts.cross_campaign_denial(store, dict(store_campaign), dict(packages[0])),
    )
    if denial is not None:
        raise ConfigurationError(f"前序 EvidencePackage 未获正式引用授权：{denial}")
    authorized = [
        item for item in payload["imported_objects"] if item["object_kind"] in OFFICIAL_EVIDENCE_OBJECT_KINDS
    ]
    return {
        "schema_version": VC1_REUSE_PLAN_SCHEMA,
        "campaign_id": manifest["campaign_id"],
        "predecessor": dict(binding),
        "import_ref": found["import_ref"],
        "receipt_ref": found["receipt_ref"],
        "evidence_package_ref": dict(packages[0]),
        "source_evidence_fact_ref": dict(evidence_facts[0]),
        "authorized_objects": authorized,
        "execute_item_ids": [],
        "reuse_item_ids": official_job_ids(manifest),
        "live_request_count": 0,
    }


def vc1_reuse_plan(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """按 Campaign 目录取 read_only_reuse 的 VC-1 零请求封存输入（seal 链 B2-07 调用）。"""

    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    return vc1_reuse_plan_from_store(store, store_campaign, manifest)
