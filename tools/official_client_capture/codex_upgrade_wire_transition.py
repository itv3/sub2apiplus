"""两阶段 wire-producer-transition、evaluation epoch 链与 attempt 身份裁定（方案 A2）。

有效 wire 身份：Campaign 的「当前有效 wire 身份」是 ``campaign.json.tool_identity`` 冻结
的 ``wire_producer_sha256``，或最新 ``final-<n>.json`` 的 ``to_wire_producer_sha256``。
seal 与全部 wire 校验点只比它；存在未 final 的 intent 时 seal 拒绝。

两阶段 transition（``control/wire-transitions/``）：

1. ``intent-<n>.json``：绑定 ``from_wire_producer_sha256``、``to_wire_producer_sha256``、
   变化文件与函数闭包清单、受影响 Job 闭集、``previous_transition_sha256``；操作员
   ``--approve-sha256`` 后生效。生效期间执行校验点对闭集内 Job 接受新 producer 身份，
   闭集外 Job 仍要求旧身份；受影响 Job 数等于全部时拒签，必须以普通 Formal 后继重跑。
2. 补跑闭集内 Job，全部 complete 后签 ``final-<n>.json``，绑定 intent SHA 与每个 Job 的
   新结果摘要。

evaluation epoch 链（attempt 目录 ``evaluation-epoch-<n>.json``）：``evidence_semantics``
变化不重采，追加 epoch，n 单调递增无上限，带 ``previous_epoch_sha256``；preview 与
draft 按 epoch 编号不覆盖。

裁定工具 ``verdict-official-attempt-identity``：从部署收据找 attempt 执行时生效的工具
树精确副本（下一次部署备份的 ``managed-tools-backup-before-*``），用策略算当时的
``wire_producer_sha256`` 与当前比较；找不到副本时只有 v1 整树 ``files_sha256`` 完全相等
才判「相等」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

INTENT_SCHEMA = "wire-producer-transition-intent/v1"
FINAL_SCHEMA = "wire-producer-transition-final/v1"
EPOCH_SCHEMA = "evaluation-epoch/v1"
VERDICT_SCHEMA = "official-attempt-identity-verdict/v1"
TRANSITIONS_DIR = Path("control") / "wire-transitions"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INTENT_RE = re.compile(r"^intent-(\d{2})\.json$")
FINAL_RE = re.compile(r"^final-(\d{2})\.json$")
EPOCH_RE = re.compile(r"^evaluation-epoch-(\d{2})\.json$")
DEPLOY_RECEIPT_GLOB = "codex-*-supervisor-enable-*.json"


class WireTransitionError(ValueError):
    """transition、epoch 或裁定输入被破坏或不满足前置。"""


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise WireTransitionError(f"{label}不是可信普通文件：{path}")
    try:
        payload = json.loads(path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WireTransitionError(f"{label}不是合法 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise WireTransitionError(f"{label}顶层必须是对象")
    return payload


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise WireTransitionError(f"输出已存在，禁止覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    path.chmod(0o600)


def _self_check(payload: Mapping[str, Any], label: str) -> None:
    unsigned = {k: v for k, v in payload.items() if k != "receipt_sha256"}
    if payload.get("receipt_sha256") != _fingerprint(unsigned):
        raise WireTransitionError(f"{label}自摘要不一致")


def _v2_identity(manifest: Mapping[str, Any]) -> dict[str, str] | None:
    identity = manifest.get("tool_identity")
    if not isinstance(identity, Mapping):
        raise WireTransitionError("Campaign 清单缺少 tool_identity")
    wire = identity.get("wire_producer_sha256")
    policy = identity.get("policy_sha256")
    if wire is None and policy is None:
        return None
    if not isinstance(wire, str) or not SHA256_RE.fullmatch(wire) or not isinstance(policy, str) or not SHA256_RE.fullmatch(policy):
        raise WireTransitionError("Campaign 清单的 v2 工具身份非法")
    return {
        "wire_producer_sha256": wire,
        "policy_sha256": policy,
        "evidence_semantics_sha256": str(identity.get("evidence_semantics_sha256", "")),
        "control_sha256": str(identity.get("control_sha256", "")),
        "files_sha256": str(identity.get("files_sha256", "")),
        "policy_version": int(identity.get("policy_version", 0) or 0),
    }


# ---------------------------------------------------------------------------
# transition 链
# ---------------------------------------------------------------------------


def _load_transitions(campaign_dir: Path) -> list[dict[str, Any]]:
    """读取 intent／final 链并校验：intent n 之后只能有 final n，再有 intent n+1。"""

    root = campaign_dir / TRANSITIONS_DIR
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise WireTransitionError("wire-transitions 目录不可信")
    intents: dict[int, dict[str, Any]] = {}
    finals: dict[int, dict[str, Any]] = {}
    for child in sorted(root.iterdir()):
        match = INTENT_RE.fullmatch(child.name)
        if match:
            payload = _read_json(child, f"intent {child.name}")
            if payload.get("schema_version") != INTENT_SCHEMA or payload.get("index") != int(match.group(1)):
                raise WireTransitionError(f"intent {child.name} schema 或编号非法")
            _self_check(payload, f"intent {child.name}")
            intents[int(match.group(1))] = payload
            continue
        match = FINAL_RE.fullmatch(child.name)
        if match:
            payload = _read_json(child, f"final {child.name}")
            if payload.get("schema_version") != FINAL_SCHEMA or payload.get("index") != int(match.group(1)):
                raise WireTransitionError(f"final {child.name} schema 或编号非法")
            _self_check(payload, f"final {child.name}")
            finals[int(match.group(1))] = payload
            continue
        raise WireTransitionError(f"wire-transitions 含非法文件：{child.name}")
    chain: list[dict[str, Any]] = []
    previous_sha: str | None = None
    for index in range(1, max(intents, default=0) + 1):
        intent = intents.get(index)
        if intent is None:
            raise WireTransitionError(f"transition 链缺少 intent-{index:02d}")
        if intent.get("previous_transition_sha256") != previous_sha:
            raise WireTransitionError(f"intent-{index:02d} 未衔接上一 transition")
        if intent.get("status") != "approved":
            raise WireTransitionError(f"intent-{index:02d} 未批准却已落盘")
        final = finals.get(index)
        if final is not None:
            if final.get("intent_sha256") != intent["receipt_sha256"]:
                raise WireTransitionError(f"final-{index:02d} 未绑定对应 intent")
            previous_sha = final["receipt_sha256"]
        elif index != max(intents):
            raise WireTransitionError(f"intent-{index:02d} 未 final 却已有后续 intent")
        chain.append({"index": index, "intent": intent, "final": final})
    if set(finals) - set(intents):
        raise WireTransitionError("存在没有 intent 的 final")
    return chain


def effective_wire_identity(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """返回当前有效 wire 身份、来源与是否存在未 final 的 intent；v1 Campaign 返回 None 字段。"""

    base = _v2_identity(manifest)
    if base is None:
        return {"policy_version": 1, "wire_producer_sha256": None, "policy_sha256": None, "source": "v1", "pending_intent": None, "chain_length": 0}
    chain = _load_transitions(campaign_dir)
    wire = base["wire_producer_sha256"]
    source = "manifest"
    pending: dict[str, Any] | None = None
    for item in chain:
        intent = item["intent"]
        if intent.get("from_wire_producer_sha256") != wire:
            raise WireTransitionError(f"intent-{item['index']:02d} 的起点不是当前有效 wire 身份")
        if item["final"] is None:
            pending = {"index": item["index"], "to_wire_producer_sha256": intent["to_wire_producer_sha256"], "affected_job_ids": list(intent["affected_job_ids"]), "intent_sha256": intent["receipt_sha256"]}
            break
        wire = str(item["final"]["to_wire_producer_sha256"])
        source = f"final-{item['index']:02d}"
    return {
        "policy_version": base["policy_version"],
        "wire_producer_sha256": wire,
        "policy_sha256": base["policy_sha256"],
        "evidence_semantics_sha256": base["evidence_semantics_sha256"],
        "source": source,
        "pending_intent": pending,
        "chain_length": len(chain),
    }


def build_intent_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    current_identity: Mapping[str, Any],
    policy: Mapping[str, Any],
    path_job_map: Mapping[str, Iterable[str]],
    planned_job_ids: Iterable[str],
) -> dict[str, Any]:
    """计算 wire 变化、闭包变化与受影响 Job 闭集，返回待批准的预览。"""

    effective = effective_wire_identity(campaign_dir, manifest)
    if effective["policy_version"] == 1:
        raise WireTransitionError("v1 Campaign 没有 wire 身份，不能签 wire transition")
    if effective["pending_intent"] is not None:
        raise WireTransitionError(f"intent-{effective['pending_intent']['index']:02d} 尚未 final，不能再签新 intent")
    if current_identity.get("policy_sha256") != effective["policy_sha256"]:
        raise WireTransitionError("策略变化不能用 wire transition 承接，须经 A2.6 兼容收据与新 Campaign")
    expected_entries = manifest["tool_identity"].get("entries")
    if not isinstance(expected_entries, list):
        raise WireTransitionError("Campaign 清单缺少工具文件清单")
    drift = tip.layer_drift(policy, expected_entries, list(current_identity["entries"]))
    changed_paths = list(drift["wire_producer"])
    current_wire = str(current_identity["wire_producer_sha256"])
    if current_wire == effective["wire_producer_sha256"]:
        raise WireTransitionError("当前 wire 身份与有效身份相同，无需 transition")
    orchestrator_changed = policy["orchestrator"]["file"] in drift["control"]
    planned = sorted(set(str(j) for j in planned_job_ids))
    affected: set[str] = set()
    unmapped: list[str] = []
    for path in changed_paths:
        mapped = path_job_map.get(path)
        if mapped is None:
            unmapped.append(path)
            continue
        affected.update(str(j) for j in mapped)
    closure_changed = orchestrator_changed and not changed_paths
    all_affected = bool(unmapped) or closure_changed or (changed_paths == [] and orchestrator_changed)
    if orchestrator_changed:
        # 编排器闭包变化映射不到具体 Job：按方案全部 Job 受影响。
        all_affected = True
    affected_ids = planned if all_affected else sorted(affected & set(planned))
    preview = {
        "schema_version": INTENT_SCHEMA,
        "index": effective["chain_length"] + 1,
        "campaign_id": manifest.get("campaign_id"),
        "from_wire_producer_sha256": effective["wire_producer_sha256"],
        "to_wire_producer_sha256": current_wire,
        "policy_sha256": effective["policy_sha256"],
        "changed_paths": changed_paths,
        "unmapped_paths": sorted(unmapped),
        "orchestrator_closure_changed": orchestrator_changed,
        "orchestrator_closures": current_identity.get("orchestrator_closures"),
        "planned_job_ids": planned,
        "affected_job_ids": affected_ids,
        "all_jobs_affected": all_affected or (len(affected_ids) == len(planned) and bool(planned)),
        "previous_transition_sha256": _last_transition_sha(campaign_dir),
        "created_at_utc": _utc_now(),
    }
    preview["review_sha256"] = _fingerprint({k: v for k, v in preview.items() if k not in {"created_at_utc"}})
    return preview


def _last_transition_sha(campaign_dir: Path) -> str | None:
    chain = _load_transitions(campaign_dir)
    if not chain:
        return None
    last = chain[-1]
    if last["final"] is None:
        return None
    return str(last["final"]["receipt_sha256"])


def approve_intent(campaign_dir: Path, preview: Mapping[str, Any], approve_sha256: str) -> Path:
    if preview.get("all_jobs_affected"):
        raise WireTransitionError("受影响 Job 等于全部计划 Job，拒签 intent；必须以普通 Formal 后继 Campaign 重新执行全部 Job")
    if approve_sha256 != preview.get("review_sha256"):
        raise WireTransitionError("批准摘要与预览不一致")
    payload = {k: v for k, v in preview.items() if k != "review_sha256"}
    payload["status"] = "approved"
    payload["approved_sha256"] = approve_sha256
    payload["approved_at_utc"] = _utc_now()
    payload["receipt_sha256"] = _fingerprint(payload)
    path = campaign_dir / TRANSITIONS_DIR / f"intent-{int(preview['index']):02d}.json"
    _write_once(path, payload)
    return path


def build_final(campaign_dir: Path, manifest: Mapping[str, Any], attempt: Mapping[str, Any]) -> Path:
    """闭集内 Job 全部 complete 后签 final，绑定 intent 与每个 Job 的结果摘要。"""

    effective = effective_wire_identity(campaign_dir, manifest)
    pending = effective["pending_intent"]
    if pending is None:
        raise WireTransitionError("没有待 final 的 intent")
    results = attempt.get("results")
    if not isinstance(results, list):
        raise WireTransitionError("attempt 缺少 results")
    by_id = {str(r.get("id")): r for r in results if isinstance(r, Mapping)}
    bindings: list[dict[str, Any]] = []
    for job_id in pending["affected_job_ids"]:
        result = by_id.get(job_id)
        if result is None or result.get("status") != "complete":
            raise WireTransitionError(f"闭集内 Job 未 complete：{job_id}")
        bindings.append({"job_id": job_id, "result_sha256": _fingerprint(result)})
    payload = {
        "schema_version": FINAL_SCHEMA,
        "index": pending["index"],
        "campaign_id": manifest.get("campaign_id"),
        "intent_sha256": pending["intent_sha256"],
        "attempt_id": attempt.get("attempt_id"),
        "to_wire_producer_sha256": pending["to_wire_producer_sha256"],
        "job_results": bindings,
        "created_at_utc": _utc_now(),
    }
    payload["receipt_sha256"] = _fingerprint(payload)
    path = campaign_dir / TRANSITIONS_DIR / f"final-{pending['index']:02d}.json"
    _write_once(path, payload)
    return path


# ---------------------------------------------------------------------------
# evaluation epoch 链
# ---------------------------------------------------------------------------


def load_epochs(attempt_root: Path) -> list[dict[str, Any]]:
    epochs: dict[int, dict[str, Any]] = {}
    for child in sorted(attempt_root.iterdir()) if attempt_root.is_dir() else []:
        match = EPOCH_RE.fullmatch(child.name)
        if not match:
            continue
        payload = _read_json(child, f"epoch {child.name}")
        if payload.get("schema_version") != EPOCH_SCHEMA or payload.get("index") != int(match.group(1)):
            raise WireTransitionError(f"epoch {child.name} schema 或编号非法")
        _self_check(payload, f"epoch {child.name}")
        epochs[int(match.group(1))] = payload
    chain: list[dict[str, Any]] = []
    previous: str | None = None
    for index in range(1, max(epochs, default=0) + 1):
        epoch = epochs.get(index)
        if epoch is None:
            raise WireTransitionError(f"epoch 链缺少 evaluation-epoch-{index:02d}")
        if epoch.get("previous_epoch_sha256") != previous:
            raise WireTransitionError(f"evaluation-epoch-{index:02d} 链断裂")
        previous = epoch["receipt_sha256"]
        chain.append(epoch)
    return chain


def current_evidence_semantics(attempt_root: Path, manifest: Mapping[str, Any]) -> str | None:
    base = _v2_identity(manifest)
    if base is None:
        return None
    chain = load_epochs(attempt_root)
    return str(chain[-1]["to_evidence_semantics_sha256"]) if chain else base["evidence_semantics_sha256"]


def append_epoch(attempt_root: Path, manifest: Mapping[str, Any], *, current_identity: Mapping[str, Any], reason: str) -> Path:
    base = _v2_identity(manifest)
    if base is None:
        raise WireTransitionError("v1 Campaign 没有 evidence semantics 身份，不能追加 epoch")
    if current_identity.get("policy_sha256") != base["policy_sha256"]:
        raise WireTransitionError("策略变化不能用 epoch 承接")
    chain = load_epochs(attempt_root)
    from_sha = str(chain[-1]["to_evidence_semantics_sha256"]) if chain else base["evidence_semantics_sha256"]
    to_sha = str(current_identity["evidence_semantics_sha256"])
    if to_sha == from_sha:
        raise WireTransitionError("evidence semantics 未变化，无需追加 epoch")
    payload = {
        "schema_version": EPOCH_SCHEMA,
        "index": len(chain) + 1,
        "from_evidence_semantics_sha256": from_sha,
        "to_evidence_semantics_sha256": to_sha,
        "wire_producer_sha256": current_identity.get("wire_producer_sha256"),
        "files_sha256": current_identity.get("files_sha256"),
        "reason": reason,
        "previous_epoch_sha256": chain[-1]["receipt_sha256"] if chain else None,
        "created_at_utc": _utc_now(),
    }
    payload["receipt_sha256"] = _fingerprint(payload)
    path = attempt_root / f"evaluation-epoch-{payload['index']:02d}.json"
    _write_once(path, payload)
    return path


# ---------------------------------------------------------------------------
# 身份裁定
# ---------------------------------------------------------------------------


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise WireTransitionError(f"{label}缺少时间戳")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _tree_entries(root: Path) -> list[dict[str, str]]:
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and p.suffix in {".py", ".sh", ".json"}
        and "tests" not in p.relative_to(root).parts and "versions" not in p.relative_to(root).parts and "__pycache__" not in p.relative_to(root).parts
    )
    return [{"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]


def verdict_official_attempt_identity(
    campaign_dir: Path,
    attempt_id: str,
    *,
    control_root: Path,
    current_identity: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _read_json(campaign_dir / "campaign.json", "Campaign 清单")
    attempt = _read_json(campaign_dir / "official" / "attempts" / attempt_id / "attempt.json", "attempt")
    started = _timestamp(attempt.get("started_at_utc"), "attempt.started_at_utc")
    frozen_files = str(manifest["tool_identity"].get("files_sha256", ""))
    receipts: list[tuple[datetime, Path, dict[str, Any]]] = []
    for path in sorted(control_root.glob(DEPLOY_RECEIPT_GLOB)):
        payload = _read_json(path, f"部署收据 {path.name}")
        if payload.get("status") != "passed":
            continue
        receipts.append((_timestamp(payload.get("created_at_utc"), "部署收据 created_at_utc"), path, payload))
    receipts.sort(key=lambda item: item[0])
    active = [item for item in receipts if item[0] <= started]
    later = [item for item in receipts if item[0] > started]
    active_receipt = active[-1] if active else None
    copy_root: Path | None = None
    copy_source: str | None = None
    problems: list[str] = []
    if active_receipt is None:
        problems.append("attempt 开始前没有通过的部署收据")
    else:
        deployed_sha = str(active_receipt[2].get("tool_files_sha256", ""))
        if deployed_sha != frozen_files:
            problems.append("attempt 时生效的部署收据整树摘要与 Campaign 冻结身份不一致")
        for _time, path, payload in later:
            backup = Path(str(payload.get("rollback_backup", "")))
            if backup.is_dir() and not backup.is_symlink() and backup.name.startswith(f"managed-tools-backup-before-{deployed_sha[:12]}"):
                copy_root = backup
                copy_source = path.name
                break
    frozen_identity = _v2_identity(manifest)
    verdict = "different"
    basis = "no_copy"
    historical: dict[str, Any] | None = None
    if copy_root is not None:
        entries = _tree_entries(copy_root)
        copy_files_sha256 = _fingerprint({"entries": entries})
        if active_receipt is not None and copy_files_sha256 != str(active_receipt[2].get("tool_files_sha256")):
            problems.append("历史副本整树摘要与部署收据不一致")
        else:
            historical = tip.compute_identity_v2(policy, copy_root, entries)
            verdict = "equal" if historical["wire_producer_sha256"] == current_identity["wire_producer_sha256"] else "different"
            basis = "historical_copy_policy_v2"
    if copy_root is None or historical is None:
        current_files = str(current_identity.get("files_sha256", ""))
        if not problems and current_files == frozen_files:
            verdict, basis = "equal", "v1_files_sha256_equal"
        else:
            verdict, basis = "different", "no_copy_and_files_differ" if copy_root is None else "copy_untrusted"
    return {
        "schema_version": VERDICT_SCHEMA,
        "campaign_id": manifest.get("campaign_id"),
        "attempt_id": attempt_id,
        "observed_at_utc": _utc_now(),
        "policy_sha256": policy["policy_sha256"],
        "frozen_files_sha256": frozen_files,
        "frozen_v2_identity": frozen_identity,
        "active_deployment_receipt": str(active_receipt[1]) if active_receipt else None,
        "historical_copy": str(copy_root) if copy_root else None,
        "historical_copy_source_receipt": copy_source,
        "historical_wire_producer_sha256": historical["wire_producer_sha256"] if historical else None,
        "current_wire_producer_sha256": current_identity.get("wire_producer_sha256"),
        "current_files_sha256": current_identity.get("files_sha256"),
        "problems": problems,
        "basis": basis,
        "verdict": verdict,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="wire transition、evaluation epoch 与身份裁定的只读辅助 CLI。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verdict = subparsers.add_parser("verdict-official-attempt-identity", help="裁定 attempt 执行时的 wire 身份是否等于当前")
    verdict.add_argument("--campaign-dir", type=Path, required=True)
    verdict.add_argument("--attempt-id", required=True)
    verdict.add_argument("--control-root", type=Path, required=True)
    verdict.add_argument("--output", type=Path, required=True)
    status = subparsers.add_parser("wire-identity-status", help="输出 Campaign 当前有效 wire 身份与 transition 链")
    status.add_argument("--campaign-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        from tools.official_client_capture import codex_upgrade

        if arguments.command == "verdict-official-attempt-identity":
            policy = tip.load_policy()
            result = verdict_official_attempt_identity(
                arguments.campaign_dir,
                arguments.attempt_id,
                control_root=arguments.control_root,
                current_identity=codex_upgrade._tool_identity(include_git=False),
                policy=policy,
            )
            _write_once(arguments.output, result)
            result = {k: v for k, v in result.items() if k != "frozen_v2_identity"}
        else:
            manifest = _read_json(arguments.campaign_dir / "campaign.json", "Campaign 清单")
            result = effective_wire_identity(arguments.campaign_dir, manifest)
    except (WireTransitionError, tip.ToolIdentityPolicyError, OSError, ValueError) as error:
        print(f"wire transition 失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("verdict", "equal") == "equal" else 3


if __name__ == "__main__":
    sys.exit(main())
