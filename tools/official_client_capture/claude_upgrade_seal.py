"""Claude 官方取证 seal 链（Claude 升级控制面设计 B2-07）：``claude_upgrade.py capture-official seal`` 的控制流程。

一、两条路径（由总计划 ``predecessor`` 决定，不由参数选择）
================================================================
* **采集路径**（``predecessor`` 为 null）：对一次 ``awaiting_receipts`` 的正式 attempt（B2-06）四阶段封存，产物都在
  ``official/seal/<代号>/`` 下：
  ① ``client-checkpoint.json``：封存所依据的官方客户端身份（版本、二进制摘要、run 与 seal 两次二进制核验指纹）与
     attempt 绑定，首次写入时刻即 ``client_checkpoint_created``；
  ② ``drafts/``：evidence 根 ``_seal_capture_attempt`` 生成 capture manifest、CaptureIndex、完整 DiscoveryInventory、语义
     候选、正交候选分母与跨来源矩阵（B2-07 补充项「VC-1 发现补全」，见 ``claude_upgrade_discovery``）；SinkInventory 与它的
     sink containment 证据由 VC-1 静态分析产出（``--target-inventory``、``--sink-containment``），这里只核对同源并绑定；
  ③ 预览：唯一一次深度扫描（evidence 根 ``_evidence_inventory``）写 ``evidence-manifest.json``，再由 evidence 根绑定摘要、
     算出 EvidencePackage v3 与 Store 写入计划，写 ``seal-draft.json``、``seal-preview.json``，返回 ``review_sha256``，
     退出码 2；
  ④ 批准（``--approve-seal-sha256``，闭合门禁必须是 closed）：经 ``official_client_control`` CLI 幂等追加——
     ``artifact-seal --allow-existing`` 三个对象、``discovery-record``／``evidence-record`` 带幂等键——Store 状态必须
     是 ``evidence_recorded``；随后写 ``official/result.json`` 与 ``control/vc/vc-1-checkpoint.json``。
  代号由「attempt 收据摘要、Store Campaign 身份、Store 有效工具身份、evidence 层摘要、静态输入与冻结输入」导出：
  工具演进或输入变化得到新代号，旧代的草稿与预览原样保留、不能被批准。
* **只读复用路径**（``predecessor`` 冻结了 read_only_reuse，B2-09 已导入并签发收据）：零请求、一步完成——写本 Campaign
  的 ``discovery_recorded``，再写引用导入 EvidencePackage 的 ``evidence_recorded``，写阶段结果与 VC-1 checkpoint
  （执行项为空、复用项为全部 official Job），最后补「VC-0 完成、VC-1 开始、VC-1 完成」三条计时账本事件。补账对照 Codex
  ``_align_official_reuse_timing``：写入前先做一次同 ID 冲突预检，补齐时在计时账本收口锁内再查一次，已完成阶段与已
  active 的开始事件跳过，先重放 VC-0／VC-1 checkpoint。

二、幂等与崩溃收敛（验收：批准中途 SIGKILL 后重跑收敛、Store 事实数不增、status 为 evidence_recorded）
=====================================================================================================
* Store 写入一律经 CLI（进程内 ``build_parser`` + ``execute``），输入写成临时文件：对象内容寻址、``--allow-existing``
  同内容复用；两条事实带幂等键 ``fact_idempotency_key(逻辑动作, payload 摘要, 事实种类)``，payload 只用冻结值（attempt
  完成时刻、导入收据完成时刻），重跑得到同一 payload、同一键，Store 复用原事实；有父监督器上下文时带 ``--producer-run``。
* 文件制品只写一次：已存在时除时间字段外逐字段比较，相同即复用、不同即拒绝（与 Codex ``_complete_vc_phase`` 同一判据）。
* 运行在 Campaign 锁内，并要求没有仍在执行的租约（与 B2-09 导入同口径，``_require_quiescent_campaign_lease``；B2-08 起
  停线租约视为静止；B3-06 起 run 被强杀留下的过期 active 租约先固化为停线收据再继续，与 Codex 同语义）：seal 不取得
  ``CampaignLease``，被强杀后不留下需要对账的过期租约；父监督器（campaign-run）不写本地租约文件，监督器内派发不受影响。

三、交给后续工作项的边界
========================
* VC checkpoint 的 Campaign 级最小实现在 ``claude_upgrade_vc``（B3-01 接手）；阶段结果读取 ``_load_stage_result``、
  阶段路径 ``_stage_path`` 仍归 B3-01，这里按 Codex 的固定路径 ``official/result.json`` 写入，结果带 ``attempt`` 绑定。
* 环境隔离、污染、对账终态与工具演进下的 attempt 失效判定归 B2-08：遇到相应制品时失败关闭，不在这里猜测语义。
* 采集路径不补计时账本事件：VC-1 批次的阶段事件由批次入口（B3-01）登记；只读复用没有批次派发，三条事件在这里补。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层；evidence
根只经编排器调用，本模块不进入 evidence 闭包。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_target_inventory
from tools.official_client_capture import claude_upgrade_discovery
from tools.official_client_capture import claude_upgrade_evidence
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import claude_upgrade_predecessor
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256
from tools.official_client_control import cli as store_cli_module
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.contracts import derive_identity_conditions, fact_idempotency_key
from tools.official_client_control.errors import ControlError

SEAL_ROOT = Path("official") / "seal"
STAGE_RESULT_RELATIVE = Path("official") / "result.json"
CLIENT_CHECKPOINT_SCHEMA = "claude-upgrade-official-client-checkpoint/v1"
SEAL_DRAFT_SCHEMA = "claude-upgrade-official-seal-draft/v1"
SEAL_PREVIEW_SCHEMA = "claude-upgrade-official-seal-preview/v1"
STAGE_RESULT_SCHEMA = "claude-upgrade-official-stage-result/v1"
SEAL_RESULT_SCHEMA = "claude-upgrade-official-seal-result/v1"
CLIENT_CHECKPOINT_FILENAME = "client-checkpoint.json"
EVIDENCE_MANIFEST_FILENAME = "evidence-manifest.json"
SEAL_DRAFT_FILENAME = "seal-draft.json"
SEAL_PREVIEW_FILENAME = "seal-preview.json"
DRAFTS_DIRNAME = "drafts"
STORE_SCRATCH_DIRNAME = "store-inputs"
# Store 事实的逻辑动作名（幂等键的一部分，跨重派稳定）。
SEAL_LOGICAL_ACTION = "claude-upgrade:capture-official:seal"
REUSE_LOGICAL_ACTION = "claude-upgrade:capture-official:predecessor-reuse"
REUSE_DISCOVERY_SOURCE = "claude-upgrade-vc1-predecessor-reuse"
STORE_EVIDENCE_CHECKPOINT = "evidence_recorded"
# 只读复用补齐的三条固定零请求事件：编号、阶段、类型与下一动作逐字段固定（对照 Codex 的 recovery-import-*）。
REUSE_TIMING_EVENTS = (
    ("predecessor-reuse-vc0-completed", "VC-0", "stage_completed", "VC-0 收口完成；VC-1 以前序只读复用零请求封存"),
    ("predecessor-reuse-vc1-started", "VC-1", "stage_started", "VC-1 由 capture-official seal 零请求复用前序证据"),
    ("predecessor-reuse-vc1-completed", "VC-1", "stage_completed", "等待 VC-2 批次"),
)
_TIMING_NULL_FIELDS = (
    "attempt_id",
    "root_cause_id",
    "revision",
    "candidate_id",
    "revision_commit_sha256",
    "supersedes_revision",
    "evaluation_baseline",
    "baseline_commit_sha256",
    "baseline_kind",
    "recovery_revision",
)


def _orchestrator() -> Any:
    """编排器模块（延迟导入，避免循环；测试与演练对编排器属性的改写照样生效）。"""

    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _capture() -> Any:
    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def add_seal_arguments(target: argparse.ArgumentParser) -> None:
    """``capture-official seal`` 的参数（与 run 共用一个子命令解析器）。"""

    target.add_argument("--attempt-id", help="seal：要封存的 awaiting_receipts 正式 attempt（只读复用路径不接受）。")
    target.add_argument("--approve-seal-sha256", help="seal：批准预览输出的 review_sha256。")
    target.add_argument(
        "--target-inventory",
        type=Path,
        help="seal：VC-1 静态分析产出的目标 SinkInventory（Campaign 目录内文件；每个 sink 与入口未决项都进 DiscoveryInventory）。",
    )
    target.add_argument(
        "--sink-containment",
        type=Path,
        help=(
            "seal：VC-1 静态分析对同一份目标 SinkInventory 生成的 sink containment 证据（Campaign 目录内文件；精确 AST 调用"
            "据此归并语义候选，词法命中据此判定是否非流量）。"
        ),
    )
    target.add_argument(
        "--discovery-dispositions",
        type=Path,
        help=f"seal：发现项的人工显式处置（{claude_upgrade_evidence.DISCOVERY_DISPOSITIONS_SCHEMA}，Campaign 目录内文件）。",
    )


def seal_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``capture-official seal``：返回 (摘要, 退出码)。预览需要批准时退出码 2，封存完成为 0。"""

    orchestrator = _orchestrator()
    for flag, name in (("acknowledge_live_requests", "--acknowledge-live-requests"), ("zero_request_rehearsal", "--zero-request-rehearsal")):
        if getattr(arguments, flag, False):
            raise ConfigurationError(f"capture-official seal 不接受 {name}（只用于 run）。")
    if getattr(arguments, "job_id", None):
        raise ConfigurationError("capture-official seal 不接受 --job-id（只用于零请求演练 run）。")
    campaign_dir = Path(arguments.campaign_dir)
    orchestrator._validate_existing_campaign_path(campaign_dir)
    # 与 run 同一口径用解析后的真实路径：权限收口按路径字符串绑定内部根，重放必须得到同一组字符串。
    campaign_dir = campaign_dir.resolve(strict=True)
    orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        # B2-08 定的恢复协议：停线租约（执行者已退出）视为静止，仍在执行或已过期的 active 租约拒绝（零写入）。
        orchestrator._require_quiescent_campaign_lease(campaign_dir, action="seal", attempt_id=getattr(arguments, "attempt_id", None))
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        if manifest.get("predecessor") is not None:
            return _seal_predecessor_reuse(campaign_dir, manifest, arguments), 0
        return _seal_captured_attempt(campaign_dir, manifest, arguments)


# ---------------------------------------------------------------------------
# 通用：Store CLI、一次写入、静态输入
# ---------------------------------------------------------------------------


def store_cli(argv: Sequence[str]) -> Any:
    """经 ``official_client_control`` 命令行（进程内）执行一条 Store 命令；拒绝统一转成 ``ConfigurationError``。"""

    parser = store_cli_module.build_parser()
    try:
        arguments = parser.parse_args([str(item) for item in argv])
    except SystemExit as error:
        raise ConfigurationError(f"Store 命令参数非法：{list(argv)[:1]}") from error
    try:
        return store_cli_module.execute(arguments)
    except ControlError as error:
        raise ConfigurationError(f"Store 拒绝 {argv[0]}：{error}") from error


class _StoreSession:
    """一次 seal 里对 Campaign Store 的写入：输入写成私有临时文件后经 CLI 追加，调用结束即删除临时文件。"""

    def __init__(self, root: Path, campaign_id: str, scratch: Path) -> None:
        self.root = Path(root)
        self.campaign_id = campaign_id
        self.scratch = scratch

    def _with_input(self, payload: Mapping[str, Any], call: Any) -> Any:
        ensure_private_directory(self.scratch)
        descriptor, name = tempfile.mkstemp(prefix=".input-", suffix=".json", dir=self.scratch)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            return call(path)
        finally:
            path.unlink(missing_ok=True)

    def seal_object(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        outcome = self._with_input(
            payload,
            lambda path: store_cli(["artifact-seal", "--store", self.root, "--kind", kind, "--input", path, "--allow-existing"]),
        )
        return dict(outcome["object_ref"])

    def record_fact(self, command: str, fact_kind: str, payload: Mapping[str, Any], *, issued_at: str, logical_action: str) -> dict[str, Any]:
        key = fact_idempotency_key(logical_action, canonical_sha256(dict(payload)), fact_kind)
        producer = _producer_run()

        def call(path: Path) -> Any:
            argv: list[Any] = [
                command, "--store", self.root, "--campaign", self.campaign_id, "--input", path,
                "--issued-at", issued_at, "--idempotency-key", key,
            ]
            if producer is None:
                return store_cli(argv)
            return self._with_input(producer, lambda producer_path: store_cli([*argv, "--producer-run", producer_path]))

        outcome = self._with_input(payload, call)
        return {"fact_ref": dict(outcome["fact_ref"]), "reused": bool(outcome["reused"]), "idempotency_key": key}

    def status(self) -> dict[str, Any]:
        return dict(store_cli(["status", "--store", self.root, "--campaign", self.campaign_id]))


def _producer_run() -> dict[str, str] | None:
    """父监督器上下文里本动作的 run 坐标（``validate_producer_run`` 三键）；直接 CLI 运行时为 None。"""

    if os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV) != "1":
        return None
    values = {
        "run_dir": os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_DIR_ENV),
        "owner_nonce": os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_OWNER_NONCE_ENV),
        "action_id": os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_ACTION_ID_ENV),
    }
    if not all(values.values()):
        raise ConfigurationError("父监督器上下文缺少 producer run 坐标（run 目录、owner nonce 或动作 ID）。")
    return {key: str(value) for key, value in values.items()}


def _read_json(path: Path, label: str) -> dict[str, Any]:
    return _orchestrator()._read_json(path, label)


def _write_once_or_reuse(path: Path, payload: Mapping[str, Any], *, ignore: Sequence[str] = ()) -> dict[str, Any]:
    """只写一次：不存在即独占发布；已存在时除 ``ignore`` 字段外逐字段比较，相同返回既有内容，不同拒绝。"""

    orchestrator = _orchestrator()
    if path.exists() or path.is_symlink():
        existing = _read_json(path, "既有封存制品")
        strip = set(ignore)
        if {key: value for key, value in existing.items() if key not in strip} != {
            key: value for key, value in payload.items() if key not in strip
        }:
            raise ConfigurationError(f"封存制品已存在但内容不同（输入漂移），禁止覆盖：{path}")
        return existing
    ensure_private_directory(path.parent)
    orchestrator._secure_write_json_once(path, dict(payload))
    return dict(payload)


def _binding(path: Path, base: Path) -> dict[str, Any]:
    return {"path": path.relative_to(base).as_posix(), "sha256": file_sha256(path), "bytes": path.stat().st_size}


def _campaign_input_file(campaign_dir: Path, value: Path | None, label: str) -> Path | None:
    """命令行给出的静态输入：必须是 Campaign 目录内的普通文件（才能进深度扫描与完整性清单）；没给出时为 None。"""

    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label} 不是可信普通文件：{value}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(campaign_dir):
        raise ConfigurationError(f"{label} 必须位于 Campaign 目录内：{value}")
    return resolved


def _frozen_input(campaign_dir: Path, manifest: Mapping[str, Any], name: str) -> Path:
    """总计划 ``inputs.<name>`` 冻结的 Campaign 内副本（摘要已由 ``load_campaign_manifest`` 核过，这里读前再核一次）。"""

    reference = (manifest.get("inputs") or {}).get(name)
    if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str):
        raise ConfigurationError(f"Claude Campaign 总计划缺少 inputs.{name} 冻结副本（建 Campaign 时冻结，B2-11）。")
    path = _orchestrator()._campaign_file(campaign_dir, str(reference["path"]))
    if path.is_symlink() or not path.is_file() or file_sha256(path) != reference.get("sha256"):
        raise ConfigurationError(f"Campaign 冻结的 inputs.{name} 缺失或摘要漂移。")
    return path


# ---------------------------------------------------------------------------
# Store Campaign 与工具身份
# ---------------------------------------------------------------------------


def create_store_from_frozen_inputs(campaign_dir: Path, manifest: Mapping[str, Any]) -> bool:
    """总计划冻结了 Store 建立输入时幂等建立 Store Campaign（``campaign-create --schema v2 --allow-existing``），返回是否声明了输入。

    ``inputs.store_bootstrap``（bootstrap 对象 payload）与 ``inputs.store_campaign``（Campaign v2）由建 Campaign（B2-11）
    冻结；建 Campaign 时（总计划写出之前，传入内存里的总计划）与 seal 时共用这一组命令，重跑逐字复用。
    """

    inputs = manifest.get("inputs") or {}
    declared = [name for name in ("store_bootstrap", "store_campaign") if inputs.get(name) is not None]
    if not declared:
        return False
    if len(declared) != 2:
        raise ConfigurationError("总计划只冻结了 store_bootstrap 与 store_campaign 之一，无法幂等建立 Store Campaign。")
    root = claude_upgrade_store.campaign_store_root(campaign_dir)
    if not (root / "store.json").exists():
        store_cli(["init-store", "--root", root, "--created-at", str(manifest["created_at_utc"])])
    store_cli(
        ["artifact-seal", "--store", root, "--kind", "bootstrap", "--input", _frozen_input(campaign_dir, manifest, "store_bootstrap"), "--allow-existing"]
    )
    store_cli(
        ["campaign-create", "--store", root, "--input", _frozen_input(campaign_dir, manifest, "store_campaign"), "--schema", "v2", "--allow-existing"]
    )
    return True


def _ensure_store_campaign(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    """打开 Campaign 的受管 Store；总计划冻结了 Store 建立输入时先幂等建立（``create_store_from_frozen_inputs``）。

    两份 Store 建立输入都没有时 Store Campaign 必须已经存在。
    """

    create_store_from_frozen_inputs(campaign_dir, manifest)
    # B2-08：编排器链已登记、Store 尚未登记的工具演进在这里补登（tool-evolution 批准后、Store 登记之前中断时），seal 两层
    # 核对同一身份。Store Campaign 本身必须在工具变化之前建立（VC-0 收口，B2-11），tool-evolution 在登记前核对。
    from tools.official_client_capture import claude_upgrade_evolution

    claude_upgrade_evolution.ensure_store_tool_registration(campaign_dir, manifest)
    return claude_upgrade_store.open_campaign_store(campaign_dir, manifest)


def _require_store_tool_current(store: Any, campaign: Mapping[str, Any]) -> str:
    """Store 有效工具身份（最新演进的 to_identity）必须等于当前受管代码；返回其 ``tool_files_sha256``。"""

    try:
        effective = store_receipts.effective_tool_identity(store, dict(campaign))
        current = store_receipts.current_tool_manifest("claude")["tool_files_sha256"]
    except ControlError as error:
        raise ConfigurationError(f"Store 工具身份无法核对：{error}") from error
    if effective is None or effective["tool_files_sha256"] != current:
        raise ConfigurationError("当前受管代码不是 Store Campaign 的有效工具身份：工具变化须先登记演进（tool-evolution）再封存。")
    return str(effective["tool_files_sha256"])


def _store_identity(campaign: Mapping[str, Any]) -> dict[str, Any]:
    derived, _unprovable = derive_identity_conditions(dict(campaign))
    return {
        "persona": dict(campaign["persona"]),
        "official_artifacts": [dict(item) for item in campaign["official_artifacts"]],
        "platforms": list(campaign["platforms"]),
        "entrypoints": list(campaign["entrypoints"]),
        "default_conditions": list(campaign["default_conditions"]),
        "platform": derived["platform"],
        "privacy_mode": derived["privacy_mode"],
    }


def _record_evidence(
    session: _StoreSession,
    discovery_payload: Mapping[str, Any],
    evidence_package_ref: Mapping[str, Any],
    *,
    issued_at: str,
    logical_action: str,
) -> dict[str, Any]:
    """Discovery 与 Evidence 两条事实（幂等键）；Store 状态必须推进到 evidence_recorded。"""

    discovery = session.record_fact(
        "discovery-record", "discovery_recorded", discovery_payload, issued_at=issued_at, logical_action=logical_action
    )
    evidence = session.record_fact(
        "evidence-record",
        "evidence_recorded",
        {"discovery_fact_ref": discovery["fact_ref"], "evidence_package_ref": dict(evidence_package_ref)},
        issued_at=issued_at,
        logical_action=logical_action,
    )
    status = session.status()
    if status.get("checkpoint") != STORE_EVIDENCE_CHECKPOINT:
        raise ConfigurationError(f"Store 状态没有推进到 {STORE_EVIDENCE_CHECKPOINT}：{status.get('checkpoint')}")
    return {
        "root": claude_upgrade_store.CAMPAIGN_STORE_DIRNAME,
        "campaign_id": session.campaign_id,
        "checkpoint": status["checkpoint"],
        "discovery_fact_ref": discovery["fact_ref"],
        "evidence_fact_ref": evidence["fact_ref"],
        "evidence_package_ref": dict(evidence_package_ref),
        "facts_reused": {"discovery_recorded": discovery["reused"], "evidence_recorded": evidence["reused"]},
    }


def _complete_stage(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    payload: dict[str, Any],
    *,
    execute_item_ids: Sequence[str],
    reuse_item_ids: Sequence[str],
    live_request_count: int,
    scanned_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """写阶段结果（只写一次，``sealed_at_utc`` 首次写入时定）与 VC-1 checkpoint（完成时刻取阶段结果的封存时刻）。"""

    orchestrator = _orchestrator()
    payload = {
        "schema_version": STAGE_RESULT_SCHEMA,
        "client": "claude",
        "campaign_id": manifest["campaign_id"],
        "stage": "capture-official",
        "status": "complete",
        **payload,
        "execute_item_ids": sorted(execute_item_ids),
        "reuse_item_ids": sorted(reuse_item_ids),
        "live_request_count": int(live_request_count),
        "scanned_bytes": int(scanned_bytes),
        "sealed_at_utc": orchestrator._utc_now(),
    }
    path = campaign_dir / STAGE_RESULT_RELATIVE
    stage = _write_once_or_reuse(path, payload, ignore=("sealed_at_utc",))
    checkpoint = claude_upgrade_vc.complete_campaign_phase(
        campaign_dir,
        manifest,
        phase="VC-1",
        stage_receipt_path=path,
        completed_at_utc=str(stage["sealed_at_utc"]),
        execute_item_ids=execute_item_ids,
        reuse_item_ids=reuse_item_ids,
        live_request_count=live_request_count,
        scanned_bytes=scanned_bytes,
    )
    return stage, checkpoint


STAGE_RESULT_FIELDS = frozenset(
    {
        "schema_version", "client", "campaign_id", "stage", "status", "mode", "attempt", "seal", "predecessor_reuse",
        "store", "execute_item_ids", "reuse_item_ids", "live_request_count", "scanned_bytes", "sealed_at_utc",
    }
)


def load_stage_result(campaign_dir: Path, path: Path, *, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取并核对官方阶段结果（B2-08 起供编排器 ``_load_stage_result`` 使用；完整阶段重放归 B3-01）。

    核对合同、字段闭集、Campaign、阶段与完成状态、两种路径各自的绑定（采集路径：attempt 收据摘要；只读复用：复用
    来源），以及 VC-1 checkpoint 已经绑定这份阶段结果——阶段结果写出、checkpoint 尚未写出的半成品按未封存处理
    （重跑 seal 即收敛）。没有阶段结果时报「尚未封存」。
    """

    orchestrator = _orchestrator()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError("官方阶段结果路径不可信。")
    if not path.exists():
        raise ConfigurationError("官方阶段尚未封存（official/result.json 不存在）。")
    stage = _read_json(path, "官方阶段结果")
    campaign_id = (manifest or _read_json(Path(campaign_dir) / "campaign.json", "Campaign 核心清单")).get("campaign_id")
    mode = stage.get("mode")
    if (
        set(stage) != STAGE_RESULT_FIELDS
        or stage.get("schema_version") != STAGE_RESULT_SCHEMA
        or stage.get("client") != "claude"
        or stage.get("campaign_id") != campaign_id
        or stage.get("stage") != "capture-official"
        or stage.get("status") != "complete"
        or mode not in {"captured", "predecessor_reuse"}
        or not orchestrator._is_rfc3339_timestamp(stage.get("sealed_at_utc"))
    ):
        raise ConfigurationError("官方阶段结果合同、Campaign 或状态不一致。")
    if mode == "captured":
        binding = stage.get("attempt")
        orchestrator._require_file_binding(binding, "官方阶段结果 attempt")
        attempt_path = orchestrator._campaign_file(Path(campaign_dir), str(binding["path"]))
        if attempt_path.is_symlink() or not attempt_path.is_file() or file_sha256(attempt_path) != binding["sha256"]:
            raise ConfigurationError("官方阶段结果绑定的 attempt 收据摘要漂移。")
        if stage.get("predecessor_reuse") is not None or not isinstance(stage.get("seal"), Mapping):
            raise ConfigurationError("采集路径的官方阶段结果必须绑定 seal 预览且不带复用来源。")
    elif stage.get("attempt") is not None or stage.get("seal") is not None or not isinstance(stage.get("predecessor_reuse"), Mapping):
        raise ConfigurationError("只读复用的官方阶段结果不得绑定 attempt 或 seal 预览。")
    checkpoint_path = claude_upgrade_vc.vc_checkpoint_path(Path(campaign_dir), "VC-1")
    if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
        raise ConfigurationError("官方阶段尚未封存完整：阶段结果已写出、VC-1 checkpoint 尚未写出（重跑 capture-official seal 收敛）。")
    checkpoint = _read_json(checkpoint_path, "VC-1 checkpoint")
    receipt = checkpoint.get("stage_receipt") if isinstance(checkpoint.get("stage_receipt"), Mapping) else {}
    if receipt.get("path") != STAGE_RESULT_RELATIVE.as_posix() or receipt.get("sha256") != file_sha256(path):
        raise ConfigurationError("VC-1 checkpoint 没有绑定这份官方阶段结果。")
    return stage


# ---------------------------------------------------------------------------
# 采集路径：四阶段
# ---------------------------------------------------------------------------


def _attempt_results(attempt: Mapping[str, Any], jobs: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    """attempt 结果与 Campaign 冻结的 official Job 逐个对齐：全部完成、执行摘要一致、恰好一个证据根。"""

    orchestrator = _orchestrator()
    results = {str(item.get("id")): item for item in attempt.get("results") or [] if isinstance(item, Mapping)}
    if set(results) != {job.job_id for job in jobs}:
        raise ConfigurationError("attempt 的 Job 结果与 Campaign 冻结的 official Job 不一致，禁止 seal。")
    for job in jobs:
        result = results[job.job_id]
        if result.get("status") != "complete" or result.get("execution_sha256") != orchestrator._job_execution_sha256(job):
            raise ConfigurationError(f"Job {job.job_id} 未完成或执行摘要漂移，禁止 seal。")
        roots = result.get("evidence_roots")
        if not isinstance(roots, list) or len(roots) != 1:
            raise ConfigurationError(f"Job {job.job_id} 必须恰好登记一个证据根。")
    return results


def _verify_relay_checks(job: Any, result: Mapping[str, Any], layout: Any) -> list[Path]:
    """run 时逐步核过的 relay 清单在 seal 时摘要不变；返回该 Job 各 probe 的宿主证据目录。"""

    capture = _capture()
    probes = [str(step["probe_id"]) for step in job.steps]
    checks = result.get("relay_checks") or []
    if [row.get("probe_id") for row in checks] != probes:
        raise ConfigurationError(f"Job {job.job_id} 的 relay 核对行与冻结步骤不一致。")
    directories: list[Path] = []
    for row in checks:
        manifest = row.get("manifest") if isinstance(row.get("manifest"), Mapping) else {}
        path = capture._host_path(str(manifest.get("path", "")), layout)
        if row.get("status") != "passed" or path.is_symlink() or not path.is_file() or file_sha256(path) != manifest.get("sha256"):
            raise ConfigurationError(f"Job {job.job_id} 的 {row.get('probe_id')} relay 清单缺失、未通过或在 run／seal 之间漂移。")
        directories.append(path.parent)
    return directories


def _static_inputs(
    campaign_dir: Path,
    arguments: argparse.Namespace,
    data_root: Path,
    target_version: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """VC-1 静态输入：默认取 ``analyze-target`` 的规范产物（B2-11，``claude_upgrade_static``）；显式给出的
    ``--target-inventory``／``--sink-containment`` 在规范产物存在时必须与之逐字节相同，规范产物不存在时照旧按显式值；
    另返回规范收据与 Campaign 冻结的生产现状清单（都可缺，绑定进完整性清单）。"""

    from tools.official_client_capture import claude_upgrade_static

    canonical = claude_upgrade_static.canonical_outputs(campaign_dir, manifest)
    inventory_path = _campaign_input_file(campaign_dir, getattr(arguments, "target_inventory", None), "--target-inventory")
    containment_path = _campaign_input_file(campaign_dir, getattr(arguments, "sink_containment", None), "--sink-containment")
    if canonical is not None:
        for flag, given, default in (
            ("--target-inventory", inventory_path, canonical["target_inventory"]),
            ("--sink-containment", containment_path, canonical["sink_containment"]),
        ):
            if given is not None and file_sha256(given) != file_sha256(default):
                raise ConfigurationError(f"{flag} 与 VC-1 静态分析的规范产物不一致：{given}")
        inventory_path = inventory_path or canonical["target_inventory"]
        containment_path = containment_path or canonical["sink_containment"]
    if inventory_path is None:
        raise ConfigurationError(
            "capture-official seal 缺少目标 SinkInventory：先在 VC-1 批次里执行 analyze-target，或显式提供 --target-inventory。"
        )
    if containment_path is None:
        raise ConfigurationError(
            "capture-official seal 缺少 sink containment 证据：先在 VC-1 批次里执行 analyze-target，或显式提供 --sink-containment。"
        )
    static_receipt = canonical["receipt"] if canonical is not None else None
    inventory_reference = (manifest.get("inputs") or {}).get("production_inventory")
    production_inventory = (
        _frozen_input(campaign_dir, manifest, "production_inventory") if inventory_reference is not None else None
    )
    dispositions_path = _campaign_input_file(campaign_dir, getattr(arguments, "discovery_dispositions", None), "--discovery-dispositions")
    inventory = _read_json(inventory_path, "目标 SinkInventory")
    if inventory.get("schema_version") != claude_target_inventory.INVENTORY_SCHEMA or inventory.get("target_version") != target_version:
        raise ConfigurationError("--target-inventory 不是本 Campaign 目标版本的目标 SinkInventory。")
    containment = _read_json(containment_path, "sink containment 证据")
    # 与目标清单同源（containment 绑定的清单摘要）、逐 sink 无遗漏由 evidence 语义（发现补全）核对，这里只挡错文件。
    if (
        containment.get("schema_version") != claude_upgrade_discovery.CONTAINMENT_SCHEMA
        or containment.get("target_version") != target_version
    ):
        raise ConfigurationError("--sink-containment 不是本 Campaign 目标版本的 sink containment 证据。")
    return {
        "target_inventory": inventory,
        "target_inventory_path": inventory_path.relative_to(data_root).as_posix(),
        "target_inventory_file": inventory_path,
        "target_inventory_sha256": file_sha256(inventory_path),
        "sink_containment": containment,
        "sink_containment_path": containment_path.relative_to(data_root).as_posix(),
        "sink_containment_file": containment_path,
        "dispositions": _read_json(dispositions_path, "发现项显式处置") if dispositions_path else None,
        "dispositions_path": dispositions_path.relative_to(data_root).as_posix() if dispositions_path else None,
        "dispositions_file": dispositions_path,
        "static_analysis_file": static_receipt,
        "static_analysis_path": static_receipt.relative_to(data_root).as_posix() if static_receipt else None,
        "production_inventory_file": production_inventory.resolve(strict=True) if production_inventory else None,
        "production_inventory_path": (
            production_inventory.resolve(strict=True).relative_to(data_root).as_posix() if production_inventory else None
        ),
    }


def _generation(key: Mapping[str, Any]) -> str:
    return "g-" + canonical_sha256(dict(key))[:16]


def _seal_captured_attempt(campaign_dir: Path, manifest: dict[str, Any], arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    orchestrator = _orchestrator()
    capture = _capture()
    attempt_id = getattr(arguments, "attempt_id", None)
    if not attempt_id:
        raise ConfigurationError("capture-official seal 必须提供 --attempt-id。")
    # B2-08：未隔离的污染事实封锁 Campaign（已被 environment-isolate 覆盖的旁路标记不再挡住续跑之后的 seal）。
    orchestrator._reject_contaminated_campaign(campaign_dir)
    key = ("official", None, str(attempt_id))
    if key in orchestrator._conflict_quarantined_attempts(campaign_dir):
        raise ConfigurationError("该 attempt 已被证据根冲突隔离（证据不可信，永不复用、永不 seal）。")
    if key in capture._invalidated_attempts(campaign_dir):
        raise ConfigurationError("该 attempt 已被环境隔离或冲突隔离作废，永不 seal。")
    stage_path = campaign_dir / STAGE_RESULT_RELATIVE
    if stage_path.exists() or stage_path.is_symlink():
        sealed = _read_json(stage_path, "官方阶段结果")
        if not isinstance(sealed.get("attempt"), Mapping) or Path(str(sealed["attempt"].get("path", ""))).parent.name != attempt_id:
            raise ConfigurationError("官方证据已经由另一 attempt 或前序复用封存，禁止覆盖。")
    attempt_root, attempt = orchestrator._load_capture_attempt(
        campaign_dir, "official", None, str(attempt_id), _verified_campaign_manifest=manifest
    )
    if attempt.get("status") != "awaiting_receipts":
        raise ConfigurationError("只有 awaiting_receipts attempt 可以 seal。")
    failed = orchestrator._failed_job_ids(attempt.get("results"))
    if failed:
        raise ConfigurationError("attempt 仍有失败 Job，禁止 seal：" + "、".join(failed) + "；只能定向补跑失败闭集（B2-08）。")
    invalidated = orchestrator._attempt_evolution_invalidated_job_ids(
        campaign_dir, manifest, attempt_root, attempt, phase="official", candidate_id=None
    )
    if invalidated:
        raise ConfigurationError(
            f"官方 attempt {attempt_id} 有作业被工具演进作废（{'、'.join(invalidated)}）：先 reconcile-attempt 按作废对账、"
            "批准恢复预览后 resume --rerun-failed 只重跑失效作业，再对新 attempt 走 seal。"
        )
    jobs = orchestrator._campaign_jobs(campaign_dir, manifest, "official")
    results = _attempt_results(attempt, jobs)
    current_tool = orchestrator._tool_identity(include_git=False)
    orchestrator._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action="capture-official seal")
    layout = capture._capture_layout(campaign_dir, manifest)
    data_root = Path(layout.host_data_root).resolve(strict=True)
    store, store_campaign = _ensure_store_campaign(campaign_dir, manifest)
    tool_files_sha256 = _require_store_tool_current(store, store_campaign)
    labels_path = _frozen_input(campaign_dir, manifest, "evidence_labels")
    scenarios_path = _frozen_input(campaign_dir, manifest, "target_scenarios")
    static = _static_inputs(campaign_dir, arguments, data_root, str(manifest["target_version"]), manifest)
    attempt_path = attempt_root / "attempt.json"
    generation_key = {
        "attempt": _binding(attempt_path, data_root),
        "store_campaign_identity_sha256": store_campaign["identity_sha256"],
        "tool_files_sha256": tool_files_sha256,
        "evidence_semantics_sha256": current_tool["evidence_semantics_sha256"],
        "target_inventory": _binding(static["target_inventory_file"], data_root),
        "sink_containment": _binding(static["sink_containment_file"], data_root),
        "dispositions": _binding(static["dispositions_file"], data_root) if static["dispositions_file"] else None,
        # B2-11 的两份可缺输入只在存在时进入代号（B2-07 以来的夹具 Campaign 代号不变）。
        **{
            name: _binding(static[f"{name}_file"], data_root)
            for name in ("static_analysis", "production_inventory")
            if static[f"{name}_file"] is not None
        },
        "evidence_labels": _binding(labels_path, data_root),
        "target_scenarios": _binding(scenarios_path, data_root),
    }
    generation = _generation(generation_key)
    seal_dir = campaign_dir / SEAL_ROOT / generation
    approve = getattr(arguments, "approve_seal_sha256", None)
    if approve:
        return _approve_seal(campaign_dir, manifest, store, store_campaign, seal_dir, generation, str(approve), jobs, attempt_path), 0
    return _preview_seal(
        campaign_dir,
        manifest,
        attempt=attempt,
        attempt_path=attempt_path,
        jobs=jobs,
        results=results,
        layout=layout,
        data_root=data_root,
        store_campaign=store_campaign,
        tool_files_sha256=tool_files_sha256,
        labels_path=labels_path,
        scenarios_path=scenarios_path,
        static=static,
        generation=generation,
        generation_key=generation_key,
        seal_dir=seal_dir,
    ), 2


def _preview_seal(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    attempt: Mapping[str, Any],
    attempt_path: Path,
    jobs: Sequence[Any],
    results: Mapping[str, Mapping[str, Any]],
    layout: Any,
    data_root: Path,
    store_campaign: Mapping[str, Any],
    tool_files_sha256: str,
    labels_path: Path,
    scenarios_path: Path,
    static: Mapping[str, Any],
    generation: str,
    generation_key: Mapping[str, Any],
    seal_dir: Path,
) -> dict[str, Any]:
    """①～③：客户端检查点、草稿、唯一一次深度扫描与预览；全部只写一次，同代重跑逐字复用。"""

    orchestrator = _orchestrator()
    capture = _capture()
    # ① 客户端检查点：run 与 seal 两次核验的官方二进制必须一致。
    binary = orchestrator._verify_official_binaries(manifest)
    if orchestrator._fingerprint(dict(binary)) != orchestrator._fingerprint(dict(attempt.get("binary_verification") or {})):
        raise ConfigurationError("官方客户端二进制在 run／seal 之间发生漂移。")
    checkpoint_path = seal_dir / CLIENT_CHECKPOINT_FILENAME
    _write_once_or_reuse(
        checkpoint_path,
        {
            "schema_version": CLIENT_CHECKPOINT_SCHEMA,
            "campaign_id": manifest["campaign_id"],
            "attempt": generation_key["attempt"],
            "target_version": manifest["target_version"],
            "target_sha256": manifest["target_sha256"],
            "binary_verification_sha256": orchestrator._fingerprint(dict(binary)),
            "observed_at_utc": orchestrator._utc_now(),
        },
        ignore=("observed_at_utc",),
    )
    # ② 证据视图与草稿（evidence 根）。
    view_jobs: list[dict[str, Any]] = []
    roots: list[Path] = []
    probe_dirs: list[Path] = []
    for job in jobs:
        result = results[job.job_id]
        root = capture._host_path(str(result["evidence_roots"][0]), layout)
        probe_dirs.extend(_verify_relay_checks(job, result, layout))
        roots.append(root)
        view_jobs.append(
            {
                "job_id": job.job_id,
                "evidence_root": root.relative_to(data_root).as_posix(),
                "probe_ids": [str(step["probe_id"]) for step in job.steps],
            }
        )
    scanned = orchestrator.scan_evidence(roots, "official", base=data_root)
    drafts_dir = seal_dir / DRAFTS_DIRNAME
    draft_paths = {name: drafts_dir / filename for name, filename in claude_upgrade_evidence.DRAFT_FILES.items()}
    view = {
        "campaign_id": manifest["campaign_id"],
        "target_version": manifest["target_version"],
        "target_sha256": manifest["target_sha256"],
        "runtime_image": layout.runtime_image,
        "attempt": {key: attempt[key] for key in ("attempt_id", "run_nonce", "started_at_utc", "completed_at_utc")},
        "jobs": view_jobs,
        "required_rules": list(manifest["required_rules"]),
        "scenarios": claude_upgrade_plan.read_scenario_manifest(
            scenarios_path,
            expected_version=str(manifest["target_version"]),
            rules=tuple(manifest["required_rules"]),
            verify_source_spec=False,
        ),
        "labels_path": str(labels_path),
        "probes": scanned["probes"],
        "surfaces": scanned["surfaces"],
        "static": {
            key: static[key]
            for key in (
                "target_inventory",
                "target_inventory_path",
                "target_inventory_sha256",
                "sink_containment",
                "sink_containment_path",
                "dispositions",
                "dispositions_path",
            )
        },
        "store": _store_identity(store_campaign),
        "tool_files_sha256": tool_files_sha256,
        "completeness_paths": {
            **{name: path.relative_to(data_root).as_posix() for name, path in draft_paths.items()},
            "target_inventory": static["target_inventory_path"],
            "sink_containment": static["sink_containment_path"],
            "dispositions": static["dispositions_path"],
            "static_analysis": static["static_analysis_path"],
            "production_inventory": static["production_inventory_path"],
            "attempt": generation_key["attempt"]["path"],
            "evidence_labels": generation_key["evidence_labels"]["path"],
            "target_scenarios": generation_key["target_scenarios"]["path"],
        },
    }
    drafted = orchestrator._seal_capture_attempt(view)
    for name, path in draft_paths.items():
        _write_once_or_reuse(path, drafted["drafts"][name])
    # ③ 唯一一次深度扫描：Job 证据根、草稿与全部被绑定的静态与冻结输入。
    scan_roots = [
        *roots,
        drafts_dir,
        static["target_inventory_file"],
        static["sink_containment_file"],
        attempt_path,
        labels_path,
        scenarios_path,
    ]
    for name in ("dispositions_file", "static_analysis_file", "production_inventory_file"):
        if static[name] is not None:
            scan_roots.append(static[name])
    inventory = orchestrator._evidence_inventory(scan_roots, base=data_root)
    bound = orchestrator._seal_capture_attempt(view, inventory=inventory)
    if bound["drafts"] != drafted["drafts"]:
        raise ConfigurationError("同一证据视图两次生成的封存草稿不一致（证据在扫描期间被改动）。")
    try:
        counted = provenance.collect_root_provenance(probe_dirs)
    except (ValueError, RuntimeError, OSError) as error:
        raise ConfigurationError(f"VC-1 真实请求核算失败（B1-11 口径）：{error}") from error
    if counted["unresolved_roots"]:
        raise ConfigurationError(f"VC-1 真实请求核算有未决证据根：{counted['unresolved_roots']}")
    manifest_path = seal_dir / EVIDENCE_MANIFEST_FILENAME
    _write_once_or_reuse(manifest_path, inventory)
    draft_path = seal_dir / SEAL_DRAFT_FILENAME
    _write_once_or_reuse(
        draft_path,
        {
            "schema_version": SEAL_DRAFT_SCHEMA,
            "campaign_id": manifest["campaign_id"],
            "attempt_id": attempt["attempt_id"],
            "generation": generation,
            "generation_key": dict(generation_key),
            "drafts": {name: _binding(path, campaign_dir) for name, path in draft_paths.items()},
            "evidence_items": bound["evidence_items"],
            "closure": bound["closure"],
            "metrics": bound["metrics"],
            "seal": bound["seal"],
            "execute_item_ids": sorted(job.job_id for job in jobs),
            "live_request_provenance": {
                key: counted[key]
                for key in ("schema_version", "counting_rule", "precise_total", "model_requests_by_endpoint", "auxiliary_by_category")
            },
        },
    )
    core = {
        "schema_version": SEAL_PREVIEW_SCHEMA,
        "campaign_id": manifest["campaign_id"],
        "attempt_id": attempt["attempt_id"],
        "generation": generation,
        "client_checkpoint": _binding(checkpoint_path, campaign_dir),
        "evidence_manifest": _binding(manifest_path, campaign_dir),
        "seal_draft": _binding(draft_path, campaign_dir),
        "closure": bound["closure"],
        "metrics": bound["metrics"],
        "store_plan": {
            "objects": [{"name": item["name"], "object_kind": item["kind"], "ref": item["ref"]} for item in bound["seal"]["objects"]],
            "evidence_package_ref": bound["seal"]["evidence_package_ref"],
            "discovery_payload_sha256": canonical_sha256(bound["seal"]["discovery_payload"]),
        },
        "live_request_count": int(counted["precise_total"]),
        "scanned_bytes": int(inventory["total_bytes"]),
    }
    preview = {**core, "review_sha256": orchestrator._fingerprint(core)}
    preview = _write_once_or_reuse(seal_dir / SEAL_PREVIEW_FILENAME, preview)
    blocked = bound["closure"]["status"] != "closed"
    return {
        "schema_version": SEAL_RESULT_SCHEMA,
        "status": "seal_blocked" if blocked else "awaiting_seal_approval",
        "mode": "captured",
        "campaign_id": manifest["campaign_id"],
        "attempt_id": attempt["attempt_id"],
        "generation": generation,
        "review_sha256": preview["review_sha256"],
        "closure": bound["closure"],
        "metrics": bound["metrics"],
        "live_request_count": preview["live_request_count"],
        "scanned_bytes": preview["scanned_bytes"],
        "preview": str(seal_dir / SEAL_PREVIEW_FILENAME),
        "next_command": (
            "闭合门禁 blocked：先处置阻断原因（未决发现项写 --discovery-dispositions），再重新预览"
            if blocked
            else f"capture-official seal --attempt-id {attempt['attempt_id']} --approve-seal-sha256 {preview['review_sha256']}"
        ),
    }


def _verify_bound_file(campaign_dir: Path, binding: Mapping[str, Any], label: str) -> Path:
    path = _orchestrator()._campaign_file(campaign_dir, str(binding.get("path", "")))
    if path.is_symlink() or not path.is_file() or file_sha256(path) != binding.get("sha256") or path.stat().st_size != binding.get("bytes"):
        raise ConfigurationError(f"{label}缺失或摘要漂移：{binding.get('path')}")
    return path


def _approve_seal(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    store: Any,
    store_campaign: Mapping[str, Any],
    seal_dir: Path,
    generation: str,
    approve: str,
    jobs: Sequence[Any],
    attempt_path: Path,
) -> dict[str, Any]:
    """④ 批准：核对冻结预览与草案，经 CLI 幂等写 Store 到 evidence_recorded，再写阶段结果与 VC-1 checkpoint。

    批准不再做深度扫描：证据边界由 ``_load_capture_attempt`` 的权限收口重放担保，草案与扫描清单按预览绑定的摘要核对。
    """

    orchestrator = _orchestrator()
    preview_path = seal_dir / SEAL_PREVIEW_FILENAME
    if preview_path.is_symlink() or not preview_path.is_file():
        raise ConfigurationError("当前输入（attempt、工具身份与静态输入）没有冻结的 seal 预览：先不带 --approve-seal-sha256 运行预览。")
    preview = _read_json(preview_path, "seal 预览")
    core = {key: value for key, value in preview.items() if key != "review_sha256"}
    if preview.get("review_sha256") != orchestrator._fingerprint(core) or preview.get("generation") != generation:
        raise ConfigurationError("seal 预览自摘要或代号不一致。")
    if approve != preview["review_sha256"]:
        raise ConfigurationError("批准摘要与当前冻结预览的 review_sha256 不一致（预览可能已因输入变化而换代）。")
    if preview["closure"].get("status") != "closed":
        raise ConfigurationError(f"闭合门禁 blocked，禁止批准：{preview['closure'].get('reasons')}")
    _verify_bound_file(campaign_dir, preview["client_checkpoint"], "客户端检查点")
    _verify_bound_file(campaign_dir, preview["evidence_manifest"], "深度扫描清单")
    draft = _read_json(_verify_bound_file(campaign_dir, preview["seal_draft"], "seal 草案"), "seal 草案")
    for name, binding in draft["drafts"].items():
        _verify_bound_file(campaign_dir, binding, f"封存草稿 {name}")
    session = _StoreSession(store.root, str(store_campaign["campaign_id"]), seal_dir / STORE_SCRATCH_DIRNAME)
    for item in draft["seal"]["objects"]:
        if session.seal_object(item["kind"], item["payload"]) != item["ref"]:
            raise ConfigurationError(f"Store 封存 {item['name']} 得到的引用与预览不一致。")
    store_summary = _record_evidence(
        session,
        draft["seal"]["discovery_payload"],
        draft["seal"]["evidence_package_ref"],
        issued_at=str(draft["seal"]["discovery_payload"]["discovered_at_utc"]),
        logical_action=SEAL_LOGICAL_ACTION,
    )
    execute = sorted(job.job_id for job in jobs)
    if execute != draft["execute_item_ids"]:
        raise ConfigurationError("Campaign official Job 与 seal 草案登记的执行项不一致。")
    attempt_binding = {"path": attempt_path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(attempt_path)}
    if attempt_binding["sha256"] != draft["generation_key"]["attempt"]["sha256"]:
        raise ConfigurationError("attempt 收据与 seal 草案绑定的摘要不一致。")
    stage, checkpoint = _complete_stage(
        campaign_dir,
        manifest,
        {
            "mode": "captured",
            "attempt": attempt_binding,
            "seal": {
                "generation": generation,
                "preview": _binding(preview_path, campaign_dir),
                "client_checkpoint": dict(preview["client_checkpoint"]),
                "evidence_manifest": dict(preview["evidence_manifest"]),
                "seal_draft": dict(preview["seal_draft"]),
                "review_sha256": preview["review_sha256"],
            },
            "predecessor_reuse": None,
            "store": {key: value for key, value in store_summary.items() if key != "facts_reused"},
        },
        execute_item_ids=execute,
        reuse_item_ids=[],
        live_request_count=int(preview["live_request_count"]),
        scanned_bytes=int(preview["scanned_bytes"]),
    )
    return {
        "schema_version": SEAL_RESULT_SCHEMA,
        "status": "complete",
        "mode": "captured",
        "campaign_id": manifest["campaign_id"],
        "attempt_id": preview["attempt_id"],
        "generation": generation,
        "store": store_summary,
        "stage_result": str(campaign_dir / STAGE_RESULT_RELATIVE),
        "vc1_checkpoint": str(claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-1")),
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "live_request_count": stage["live_request_count"],
        "scanned_bytes": stage["scanned_bytes"],
    }


# ---------------------------------------------------------------------------
# 只读复用路径（B2-09 挪来）
# ---------------------------------------------------------------------------


def reuse_expected_events() -> list[dict[str, Any]]:
    """只读复用补齐的三条固定零请求事件（逐字段固定，冲突预检按同 ID 逐字段比较）。"""

    return [
        {
            "event_id": event_id,
            "phase": phase,
            "event_type": event_type,
            "next_action": action,
            "live_request_count": 0,
            "receipts": [],
            **{field: None for field in _TIMING_NULL_FIELDS},
        }
        for event_id, phase, event_type, action in REUSE_TIMING_EVENTS
    ]


def _reuse_timing_state(campaign_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """只读核对复用所用计时账本：与 Campaign 同版本、同用途，当前 active。"""

    ledger_dir = _orchestrator()._campaign_timing_ledger_dir(campaign_dir, manifest)
    try:
        state = codex_upgrade_timing_ledger.phase_ledger_state(ledger_dir)
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise ConfigurationError(f"计时账本不可读：{error}") from error
    if any(state.get(key) != manifest.get(key) for key in ("baseline_version", "target_version", "campaign_purpose")):
        raise ConfigurationError("只读复用账本与 Campaign 版本或用途不一致。")
    if state.get("status") != "active":
        raise ConfigurationError(f"只读复用账本当前状态为 {state.get('status')}，禁止封存或补账。")
    return Path(ledger_dir)


def check_reuse_event_conflicts(ledger_dir: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """三条固定事件同编号已存在时必须逐字段相同，否则拒绝；返回已有事件与期望事件。

    Store 写入之前调用一次，冲突时不留下永远补不齐账本的封存；补账时在收口锁内再调用一次，覆盖两次检查之间的并发写入。
    """

    try:
        existing = {event["event_id"]: event for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_dir)}
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise ConfigurationError(f"计时账本事件不可读：{error}") from error
    expected = reuse_expected_events()
    for item in expected:
        actual = existing.get(item["event_id"])
        if actual is not None and any(actual.get(key) != value for key, value in item.items()):
            raise ConfigurationError(f"只读复用事件 {item['event_id']} 已存在但内容冲突。")
    return existing, expected


def align_reuse_timing(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[str]:
    """在计时账本收口锁内补齐三条固定事件；已完成阶段与已 active 的开始事件不倒退、不重开。返回本次追加的事件 ID。"""

    ledger_dir = _reuse_timing_state(campaign_dir, manifest)
    appended: list[str] = []
    try:
        with codex_upgrade_supervisor._timing_closeout_lock(ledger_dir):
            _reuse_timing_state(campaign_dir, manifest)
            existing, expected = check_reuse_event_conflicts(ledger_dir)
            plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
            for phase in ("VC-0", "VC-1"):
                claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, phase)
            for item in expected:
                if item["event_id"] in existing:
                    continue
                state = codex_upgrade_timing_ledger.phase_ledger_state(ledger_dir)
                if item["phase"] in state.get("completed_phases", []):
                    continue
                if item["event_type"] == "stage_started" and state.get("active_phase") == item["phase"]:
                    continue
                codex_upgrade_timing_ledger.append_event(ledger_dir, **item)
                appended.append(item["event_id"])
    except (codex_upgrade_timing_ledger.TimingLedgerError, codex_upgrade_supervisor.SupervisorError) as error:
        raise ConfigurationError(f"只读复用补账失败：{error}") from error
    return appended


def _seal_predecessor_reuse(campaign_dir: Path, manifest: dict[str, Any], arguments: argparse.Namespace) -> dict[str, Any]:
    """read_only_reuse：零请求写 Discovery／Evidence（引用导入包）、阶段结果与 VC-1 checkpoint，再补三条账本事件。"""

    for name in ("attempt_id", "approve_seal_sha256", "target_inventory", "sink_containment", "discovery_dispositions"):
        if getattr(arguments, name, None):
            raise ConfigurationError(
                f"总计划冻结了前序只读复用：seal 走零请求复用路径，不接受 --{name.replace('_', '-')}。"
            )
    stage_path = campaign_dir / STAGE_RESULT_RELATIVE
    if stage_path.exists() and _read_json(stage_path, "官方阶段结果").get("mode") != "predecessor_reuse":
        raise ConfigurationError("官方证据已经由采集路径封存，禁止再以前序复用覆盖。")
    ledger_dir = _reuse_timing_state(campaign_dir, manifest)
    check_reuse_event_conflicts(ledger_dir)
    store, store_campaign = _ensure_store_campaign(campaign_dir, manifest)
    tool_files_sha256 = _require_store_tool_current(store, store_campaign)
    plan = claude_upgrade_predecessor.vc1_reuse_plan_from_store(store, store_campaign, manifest)
    try:
        # 导入收据已在 vc1_reuse_plan_from_store 里重放通过；Discovery 的发现时刻取收据完成时刻（冻结值，重跑不变）。
        completed_at = str(store.load_receipt(dict(plan["receipt_ref"]))["completed_at_utc"])
    except ControlError as error:
        raise ConfigurationError(f"前序只读复用导入收据不可读：{error}") from error
    session = _StoreSession(store.root, str(store_campaign["campaign_id"]), campaign_dir / SEAL_ROOT / "predecessor-reuse" / STORE_SCRATCH_DIRNAME)
    store_summary = _record_evidence(
        session,
        {
            "version": manifest["target_version"],
            "source": REUSE_DISCOVERY_SOURCE,
            "discovered_at_utc": completed_at,
            "tool_sha256": tool_files_sha256,
            "artifact_refs": [dict(item) for item in store_campaign["official_artifacts"]],
        },
        plan["evidence_package_ref"],
        issued_at=completed_at,
        logical_action=REUSE_LOGICAL_ACTION,
    )
    stage, checkpoint = _complete_stage(
        campaign_dir,
        manifest,
        {
            "mode": "predecessor_reuse",
            "attempt": None,
            "seal": None,
            "predecessor_reuse": {
                "plan_sha256": canonical_sha256(plan),
                "predecessor": dict(plan["predecessor"]),
                "import_ref": dict(plan["import_ref"]),
                "receipt_ref": dict(plan["receipt_ref"]),
                "source_evidence_fact_ref": dict(plan["source_evidence_fact_ref"]),
                "imported_at_utc": completed_at,
            },
            "store": {key: value for key, value in store_summary.items() if key != "facts_reused"},
        },
        execute_item_ids=plan["execute_item_ids"],
        reuse_item_ids=plan["reuse_item_ids"],
        live_request_count=0,
        scanned_bytes=0,
    )
    appended = align_reuse_timing(campaign_dir, manifest)
    return {
        "schema_version": SEAL_RESULT_SCHEMA,
        "status": "complete",
        "mode": "predecessor_reuse",
        "campaign_id": manifest["campaign_id"],
        "store": store_summary,
        "stage_result": str(stage_path),
        "vc1_checkpoint": str(claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-1")),
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "execute_item_ids": stage["execute_item_ids"],
        "reuse_item_ids": stage["reuse_item_ids"],
        "live_request_count": 0,
        "timing_events_appended": appended,
    }
