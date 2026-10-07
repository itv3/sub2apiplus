"""完整 Mac 来源夹具：真实附件/请求验证器运行，只有来源适配器是显式离线实现。

适配器仅在测试作用域登记，证据限定系统临时目录，账本必须为 staging 内的
fixture_only。它不读取或操作实际应用；构造的材料只证明离线合同可重放。
"""

from __future__ import annotations

import base64
import contextlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import codex_upgrade_project_ledger as ledger

RECORDER = "fixture-recorded-client"
NOW = "2026-10-08T00:00:00Z"


def write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(contract.canonical(value) + b"\n")
    path.chmod(0o600)
    return path


def temporary_root(path: Path) -> Path:
    temp = Path(tempfile.gettempdir()).resolve()
    resolved = path.resolve()
    contract.require(resolved.is_relative_to(temp), "fixture source 只允许系统临时目录")
    return temp / resolved.relative_to(temp).parts[0]


def fixture_ledger(path: Path) -> Path:
    staging = temporary_root(path) / "staging"
    campaigns = [parent for parent in path.parents if (parent / "campaign.json").is_file() and parent.parent.name == "campaigns" and parent.parent.parent.name == "evidence"]
    data_root = campaigns[0].parents[2] if campaigns else None
    root = data_root if data_root is not None and data_root.is_relative_to(staging) else staging / "mac-client-fixture"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return ledger.create_fixture_ledger(root, client="claude")


class FixtureSourceAdapter:
    """来源适配器验证隔离边界；产品/请求/场景/附件仍由正式合同逐项核对。"""

    def verify_origin(self, task: Mapping[str, Any], bundle_root: Path, capture_root: Path) -> None:
        root = temporary_root(bundle_root)
        contract.require(capture_root.resolve().is_relative_to(root), "fixture capture 越出同一临时根")
        project = Path(task["execution"]["project_ledger"])
        contract.require(project.is_relative_to(root / "staging"), "fixture source 账本不在同一临时 staging 树")
        plan, _raw = ledger._load_plan(project)
        contract.require(plan.get("fixture_only") is True and plan.get("client") == "claude", "fixture source 必须绑定 Claude fixture_only 项目账本")
        contract.require(task["execution"]["recorder_id"] == RECORDER, "fixture recorder 身份不符")
        # 固定的私有测试制品；真实应用不会用这些摘要，因此不能冒充已审核的真实制品。
        contract.require(all(row["sha256"] in {"1" * 64, "3" * 64} for row in task["client_artifacts"] if row["role"] != "embedded_cli"), "fixture source 收到非夹具应用制品")
        embedded = next(row for row in task["client_artifacts"] if row["role"] == "embedded_cli")
        contract.require(embedded["sha256"] == task["catalog"]["entry"]["binary_sha256"], "fixture 内置 CLI 必须沿用候选实际 Catalog")


def source_patch() -> Any:
    return mock.patch.dict(contract.SOURCE_ADAPTERS, {RECORDER: FixtureSourceAdapter()})


@contextlib.contextmanager
def source_scope() -> Iterator[None]:
    with source_patch():
        yield


def candidate_inputs(attempt_root: Path, values: Mapping[str, Any], client_id: str) -> dict[str, Any]:
    """补齐最小测试世界的候选原件；既有 VC-4 世界沿用自己的构建和实际 Catalog。"""
    from tools.official_client_capture import claude_upgrade

    campaign = next((parent for parent in attempt_root.parents if (parent / "campaign.json").is_file()), temporary_root(attempt_root))
    if not (campaign / "campaign.json").exists():
        write(campaign / "campaign.json", {"campaign_id": values["campaign_id"]})
    build_path = campaign / "candidates" / values["candidate_id"] / "build-receipt.json"
    if not build_path.exists():
        source_root = temporary_root(attempt_root) / "fixture-candidate-source" / values["candidate_id"]
        entries = [{"client_id": f"{client}-fixture", "product": "claude-desktop" if client == "claude-desktop" else "claude-code-vscode", "version": "fixture-cli-1", "platform": "darwin/arm64", "binary_sha256": "2" * 64} for client in contract.CLIENTS]
        write(source_root / "backend/internal/officialegress/catalogdata/claude/official-ingress-catalog.json", {"entries": entries})
        write(build_path, {"campaign_id": values["campaign_id"], "candidate_id": values["candidate_id"], "candidate_revision": 1, "source": {"root": str(source_root), "tree_sha256": claude_upgrade._directory_tree_digest(source_root)}, "image": {"manifest_digest": "sha256:" + "a" * 64}, "profile": {"profile_digest": "a" * 64}})
    build = contract.read_json(build_path)
    catalog = contract.read_json(Path(build["source"]["root"]) / "backend/internal/officialegress/catalogdata/claude/official-ingress-catalog.json")
    product = "claude-desktop" if client_id == "claude-desktop" else "claude-code-vscode"
    entry = next(row for row in catalog["entries"] if row["product"] == product and row["platform"] == "darwin/arm64")
    path = attempt_root / "attempt.json"
    attempt = contract.read_json(path) if path.exists() else {}
    attempt.update(values)
    identity = attempt.setdefault("identity", {})
    identity.update({"candidate_revision": build["candidate_revision"], "source_tree_sha256": build["source"]["tree_sha256"], "manifest_digest": build["image"]["manifest_digest"], "profile_digest": build["profile"]["profile_digest"], "build_receipt": {"path": build_path.relative_to(campaign).as_posix(), "sha256": contract.binding(build_path, campaign)["sha256"]}})
    attempt.setdefault("candidate_gateway", {"base_url": "http://candidate.invalid:8080"})
    if not path.exists() or contract.read_json(path) != attempt:
        write(path, attempt)
    return contract.candidate_context(attempt_root, entry["client_id"])


def material(attempt_root: Path, client_id: str, values: Mapping[str, Any], *, scope: str = "formal", status: str = "complete", nonce: str | None = None, scenarios: list[dict[str, Any]] | None = None, scenario_reuse: list[dict[str, Any]] | None = None, issued_at_utc: str = NOW) -> tuple[Path, Path, Path]:
    """生成独立 Mac 包与 server 原件；故意不把服务端原件放进 Mac 上传包。"""
    nonce = nonce or f"{client_id}-task-{contract.digest(dict(values))[:12]}"
    current = candidate_inputs(attempt_root, values, client_id)
    client_root = attempt_root / "client-evidence" / client_id
    task_path = client_root / "tasks" / nonce / "task.json"
    bundle_root = client_root / "runs" / nonce / "source"
    capture_root = temporary_root(attempt_root) / "server-originals" / contract.digest(dict(values))[:12] / nonce
    artifacts = [{"role": "application", "version": "fixture-app-1", "platform": "darwin/arm64", "sha256": "1" * 64}, {"role": "embedded_cli", "version": current["catalog"]["entry"]["version"], "platform": "darwin/arm64", "sha256": current["catalog"]["entry"]["binary_sha256"]}]
    if client_id == "claude-vscode":
        artifacts.append({"role": "extension", "version": "fixture-extension-1", "platform": "darwin/arm64", "sha256": "3" * 64})
    task = {"schema_version": contract.TASK_SCHEMA, "client_id": client_id, **dict(values), "candidate_revision": 1, "candidate": {key: "a" * 64 for key in ("source_tree_sha256", "build_receipt_sha256", "image_digest", "profile_digest", "gateway_sha256")}, "catalog": {"sha256": "b" * 64, "entry": {"client_id": f"{client_id}-fixture", "product": "claude-desktop" if client_id == "claude-desktop" else "claude-code-vscode", "version": "fixture-cli-1", "platform": "darwin/arm64", "binary_sha256": "2" * 64}}, "client_artifacts": artifacts, "scenarios": [{"id": "client-smoke", "model": "fixture-model", "challenge": "fixture-unique-challenge-1234", "response_marker": "fixture-response", "minimum_requests": 1}], "execution": {"endpoint": "http://candidate.invalid:8080", "credential_ref": "private-ref:fixture", "timeout_seconds": 300, "request_budget": 10, "recorder_id": RECORDER, "capture_id": nonce, "producer_run_id": f"producer-{nonce}", "source_scope": scope, "capture_root": str(capture_root)}, "issued_at_utc": NOW, "task_nonce": nonce}
    task.update({key: current[key] for key in ("candidate", "candidate_revision", "catalog")})
    task["execution"]["endpoint"] = current["endpoint"]
    task["execution"]["project_ledger"] = str(fixture_ledger(attempt_root))
    task["scenarios"] = scenarios if scenarios is not None else task["scenarios"]
    task["scenario_reuse"] = scenario_reuse or []
    task["issued_at_utc"] = issued_at_utc
    task["task_sha256"] = contract.digest(task)
    write(task_path, task)
    write(bundle_root / "client-identity.json", {"client_id": client_id, "artifacts": artifacts})
    moment = contract.timestamp(issued_at_utc)
    processes = [{"pid": i + 100, "parent_pid": None if i == 0 else 100, "role": row["role"], "started_at_utc": issued_at_utc, "executable_sha256": row["sha256"]} for i, row in enumerate(artifacts)]
    write(bundle_root / "process-session.json", {"client_id": client_id, "session_id": "fixture-session", "started_at_utc": (moment + timedelta(seconds=1)).isoformat(), "completed_at_utc": (moment + timedelta(seconds=10)).isoformat(), "processes": processes})
    reused = {scenario_id for reuse in task["scenario_reuse"] for scenario_id in reuse["scenario_ids"]}
    executing = [row for row in task["scenarios"] if row["id"] not in reused]
    events = [{"session_id": "fixture-session", "scenario_id": row["id"], "kind": kind, "text": row[key]} for row in executing for kind, key in (("input", "challenge"), ("completed", "response_marker"))]
    events_path = bundle_root / "session-events.jsonl"
    events_path.write_bytes(b"".join(contract.canonical(event) + b"\n" for event in events))
    events_path.chmod(0o600)
    write(bundle_root / "recorder-execution.json", {"recorder_id": RECORDER, "task_sha256": task["task_sha256"], "run_id": nonce, "status": status})
    files = [contract.binding(path, bundle_root) for path in sorted(bundle_root.iterdir()) if path.name != "source-manifest.json"]
    write(bundle_root / "source-manifest.json", {"schema_version": contract.BUNDLE_SCHEMA, "task_sha256": task["task_sha256"], "run_id": nonce, "source_scope": scope, "status": status, "files": files})
    reservation = write(capture_root / "reservation.json", {"task_sha256": task["task_sha256"], "candidate": task["candidate"], "capture_id": nonce, "producer_run_id": f"producer-{nonce}", "reserved_at_utc": issued_at_utc})
    rows = []
    for index, scenario in enumerate(executing, start=1):
        body = contract.canonical({"model": scenario["model"], "messages": [{"role": "user", "content": scenario["challenge"]}]})
        rows.append({"connection_id": "connection-1", "request_ordinal": index, "session_id": "fixture-session", "process_pid": 100, "scenario_id": scenario["id"], "observed_at_utc": (moment + timedelta(seconds=5)).isoformat(), "host": "candidate.invalid", "method": "POST", "path": "/v1/messages", "request_body_base64": base64.b64encode(body).decode(), "response_body_base64": base64.b64encode(scenario["response_marker"].encode()).decode()})
    requests = capture_root / "requests.jsonl"
    requests.write_bytes(b"".join(contract.canonical(row) + b"\n" for row in rows))
    requests.chmod(0o600)
    write(capture_root / "capture-manifest.json", {"schema_version": contract.CAPTURE_SCHEMA, "task_sha256": task["task_sha256"], "capture_id": nonce, "producer_run_id": f"producer-{nonce}", "candidate": task["candidate"], "endpoint": task["execution"]["endpoint"], "status": status, "requests": contract.binding(requests, capture_root), "reservation": contract.binding(reservation, capture_root)})
    fixture_ledger(attempt_root)
    return task_path, bundle_root, capture_root


def write_receipt(attempt_root: Path, client_id: str, values: Mapping[str, Any]) -> Path:
    """为既有 seal/accept 世界迁移完整输入夹具；真实读侧仍重放全部附件及账本事件。"""
    task_path, bundle, capture = material(attempt_root, client_id, values)
    task = contract.read_json(task_path)
    verified = contract.verify_client_bundle(task, bundle, capture)
    write(bundle.parent / "run.json", {"schema_version": "claude-upgrade-client-imported-run/v1", "client_id": client_id, "task": contract.binding(task_path, attempt_root), "source_manifest": contract.binding(bundle / "source-manifest.json", attempt_root), "source_scope": "formal", "producer_run_id": task["execution"]["producer_run_id"]})
    root = fixture_ledger(attempt_root)
    keys = sorted(row["identity_key"] for row in verified["requests"])
    operation = "client-import:" + task["execution"]["producer_run_id"]
    payload = {"campaign_id": task["campaign_id"], "task_sha256": task["task_sha256"], "subject_kind": "client_capture", "subject_id": task["execution"]["producer_run_id"], "candidate_id": task["candidate_id"], "phase": "candidate", "request": {"status": "resolved", "identity_keys": keys, "estimated_delta": 0}}
    batch_sha = contract.digest(payload)
    ledger.append_project_event(root, operation_id=operation, event_type="reconciliation_committed", payload=payload, source_batch_sha256=batch_sha)
    accounting = write(bundle.parent / "accounting.json", {"schema_version": "claude-upgrade-client-accounting/v1", "status": "accounted", "task_sha256": task["task_sha256"], "identity_keys_sha256": contract.digest(keys), "operation_id": operation, "batch_sha256": batch_sha, "project_ledger": str(root)})
    return write(attempt_root / "client-evidence" / f"{client_id}.json", {"schema_version": contract.RECEIPT_SCHEMA, "client_id": client_id, **dict(values), "status": "success", "task": contract.binding(task_path, attempt_root), "bundle": contract.binding(bundle / "source-manifest.json", attempt_root), "verification": verified, "accounting_binding": contract.binding(accounting, attempt_root), "issued_at_utc": NOW, "conclusion_scope": "formal"})
