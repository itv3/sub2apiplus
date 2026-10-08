"""Mac v2 的结构夹具与显式测试来源；不操作应用、UI、SSH 或真实账号。

制品摘要故意只属于夹具。测试登记和编排器 wrapper 替身仅在 FixturePatch 生存期
有效；生产 registry 始终为空。现有候选／项目账本由旧夹具 helper 建立，计量仍
调用真实 ledger，而不是伪造 accounting.json。
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_upgrade_mac_client_capture as capture_control
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture.tests import claude_client_receipt_fixture as old


class FixtureSourceAdapter:
    def _roots(self, task: Mapping[str, Any], capture_root: Path) -> None:
        outer = old.temporary_root(capture_root)
        c.require(Path(task["execution"]["project_ledger"]).is_relative_to(outer / "staging"), "fixture source 账本不在临时 staging")
        plan, _ = ledger._load_plan(Path(task["execution"]["project_ledger"]))
        c.require(plan.get("fixture_only") is True and plan.get("client") == "claude", "fixture source 必须绑定 fixture_only Claude 总账")

    def verify_origin(self, task: Mapping[str, Any], bundle_root: Path, capture_root: Path) -> None:
        if task["schema_version"] == c.HISTORICAL_TASK_SCHEMA:
            old.FixtureSourceAdapter().verify_origin(task, bundle_root, capture_root)
            return
        self._roots(task, capture_root)
        c.require(bundle_root.is_relative_to(old.temporary_root(capture_root)), "fixture Mac 包不在同一临时根")
        for artifact in task["client_artifacts"]:
            proof = c.read_json(c.bound_file(bundle_root, artifact["official_source"]))
            c.require(proof.get("fixture_only") is True and proof["content"] == artifact["content"], "fixture source 收到非夹具制品来源")
        for root, side in ((bundle_root, "mac"), (capture_root, "capture")):
            proof = c.read_json(root / "source-origin.json")
            actual = c.read_json(c.bound_file(root, proof["attestation"]))
            c.require(actual == {"fixture_only": True, "side": side, "task_sha256": task["task_sha256"], "deployment": task["execution"]["source_deployments"][side]}, "受管来源原件不符")

    def verify_capture_origin(self, task: Mapping[str, Any], capture_root: Path) -> None:
        self._roots(task, capture_root)
        expected = {"deployment_id": "fixture-arm64", "tool_identity_sha256": "b" * 64}
        c.require(task["execution"]["source_deployments"]["capture"] == expected, "fixture capture 受管部署不符")
        # 来源认证只核已经真实存在的预约；ready 由完整派发／消费路径独立重验。
        source.verify_reservation_body_v2(task, capture_root)

    def verify_undispatched(self, task: Mapping[str, Any], capture_root: Path) -> None:
        self.verify_capture_origin(task, capture_root)
        proof = c.read_json(capture_root / "undispatched.json")
        c.require(c.read_json(c.bound_file(capture_root, proof["attestation"])) == {"fixture_only": True, "undispatched": True, "task_sha256": task["task_sha256"]}, "零收口证明不是受管夹具原件")

    def verify_isolation_recipe(self, task: Mapping[str, Any], recipe: Mapping[str, Any], root: Path) -> None:
        self._roots(task, Path(task["execution"]["capture_root"]))
        c.require(c.read_json(c.bound_file(root, recipe["official_support"])) == {"fixture_only": True, "client_id": task["client_id"]}, "fixture 产品隔离支持原件不符")

    def owns_services(self, services: Any) -> bool:
        return getattr(services, "fixture_only", False) is True

    def owns_recorder(self, recorder: Any) -> bool:
        return getattr(recorder, "fixture_only", False) is True


class FixturePatch:
    """保留旧测试使用的 start/stop 与 with 两种调用形式。"""

    def __init__(self) -> None:
        self.stack = contextlib.ExitStack()

    def start(self) -> Any:
        from tools.official_client_capture import claude_upgrade
        self.stack.enter_context(mock.patch.dict(c.SOURCE_ADAPTERS, {old.RECORDER: FixtureSourceAdapter()}))
        self.stack.enter_context(mock.patch.object(claude_upgrade, "verify_mac_source_v2", side_effect=source.verify_mac_source_v2, create=True))
        return self

    def stop(self) -> None:
        self.stack.close()

    def __enter__(self) -> Any:
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()


def material(attempt_root: Path, client_id: str, values: Mapping[str, Any], **kwargs: Any) -> tuple[Path, Path, Path]:
    task_path, root, capture_root = old.material_v1(attempt_root, client_id, values, **kwargs)
    legacy = c.read_json(task_path)
    moment = c.timestamp(legacy["issued_at_utc"])
    at = lambda n: (moment + timedelta(seconds=n)).isoformat()
    product = legacy["catalog"]["entry"]["product"]
    roles = [("application", "app_bundle", "product", "1" * 64), ("application_executable", "host_executable", "host", "3" * 64), ("embedded_cli", "embedded_cli", "embedded_cli", legacy["catalog"]["entry"]["binary_sha256"])]
    if client_id == "claude-vscode":
        roles += [("extension_host", "host_executable", "host", "4" * 64), ("extension_package", "extension_package", "extension_package", "5" * 64), ("extension_entry", "extension_entry", "extension_package", "6" * 64)]
    artifacts = []
    for role, kind, version_kind, sha in roles:
        content = {"kind": "directory" if kind in {"app_bundle", "extension_package"} else "file", "path": "/Applications/fixture-" + role, "sha256": sha, "bytes": 42}
        version = {"kind": version_kind, "value": legacy["catalog"]["entry"]["version"] if role == "embedded_cli" else "fixture-" + role + "-1"}
        origin = old.write(root / "origins" / f"{role}.json", {"fixture_only": True, "artifact_id": role, "content": content, "version": version})
        artifacts.append({"artifact_id": role, "kind": kind, "product": product, "role": role, "version": version, "platform": "darwin/arm64", "content": content, "official_source": c.binding(origin, root)})
    deployments = {"mac": {"deployment_id": "fixture-mac", "tool_identity_sha256": "a" * 64}, "capture": {"deployment_id": "fixture-arm64", "tool_identity_sha256": "b" * 64}}
    policy = {"methods": ["POST"], "model_paths": sorted(c.MODEL_ENDPOINTS), "retry_upper_bound": 0, "max_concurrency": 1, "deadline_utc": at(300)}
    support = old.write(root / "isolation-support.json", {"fixture_only": True, "client_id": client_id})
    recipe = old.write(root / "isolation-recipe.json", {"schema_version": "claude-upgrade-client-isolation-recipe/v2", "client_id": client_id, "deployment": deployments["mac"], "official_support": c.binding(support, root), "endpoint": legacy["execution"]["endpoint"], "credential_ref": legacy["execution"]["credential_ref"], "private_session_root": str(root.parent / "private-session"), "state_paths": ["/Users/fixture/.claude.json"], "recipe_id": "fixture-isolation", "request_policy": policy})
    task = {**legacy, "schema_version": c.TASK_SCHEMA, "client_artifacts": artifacts}
    task["execution"].update(source_deployments=deployments, isolation_recipe=c.binding(recipe, root), request_policy=policy)
    task["task_sha256"] = c.digest({k: v for k, v in task.items() if k != "task_sha256"})
    old.write(task_path, task)
    old.write(root / "client-identity.json", {"client_id": client_id, "artifacts": artifacts})
    processes, modules = [], []
    for pid, role, artifact_id, start in ((100, "application", "application_executable", 2), *(([(101, "extension_host", "extension_host", 2.2)]) if client_id == "claude-vscode" else [])):
        artifact = next(a for a in artifacts if a["artifact_id"] == artifact_id)
        row = {"process_instance_id": source.process_instance_id(pid, "fixture-boot", f"start-{pid}"), "pid": pid, "boot_id": "fixture-boot", "start_marker": f"start-{pid}", "parent_instance_id": processes[0]["process_instance_id"] if processes else None, "executable_artifact_id": artifact_id, "executable_sha256": artifact["content"]["sha256"], "role": role, "session_id": "fixture-session", "started_at_utc": at(start), "exited_at_utc": at(10)}
        proof = old.write(root / "observations" / f"process-{pid}.json", row)
        processes.append({**row, "observation": c.binding(proof, root)})
    for artifact_id in (["embedded_cli", "extension_entry"] if client_id == "claude-vscode" else ["embedded_cli"]):
        a = next(a for a in artifacts if a["artifact_id"] == artifact_id)
        row = {"module_instance_id": "module-" + artifact_id, "artifact_id": artifact_id, "host_process_instance_id": processes[-1]["process_instance_id"], "entrypoint": a["content"]["path"], "loaded_at_utc": at(2.4), "activated_at_utc": at(2.5)}
        proof = old.write(root / "observations" / f"{row['module_instance_id']}.json", row)
        modules.append({**row, "observation": c.binding(proof, root)})
    old.write(root / "process-session.json", {"client_id": client_id, "session_id": "fixture-session", "started_at_utc": at(2), "completed_at_utc": at(10), "processes": processes, "loaded_modules": modules})
    legacy_requests = source._json_lines(capture_root / "requests.jsonl", "夹具请求")
    calls, maps, connections, requests = [], [], [], []
    for index, legacy_row in enumerate(legacy_requests):
        connection_id = f"connection-{index+1}"
        call = {"socket_call_id": f"socket-{index+1}", "process_instance_id": processes[-1]["process_instance_id"], "module_instance_ids": [m["module_instance_id"] for m in modules], "session_id": "fixture-session", "scenario_id": legacy_row["scenario_id"], "client_endpoint": f"127.0.0.1:{3000+index}", "server_endpoint": "127.0.0.1:8080", "opened_at_utc": at(3), "closed_at_utc": at(7), "request_ordinals": [legacy_row["request_ordinal"]]}
        proof = old.write(root / "observations" / f"socket-{index+1}.json", call)
        calls.append({**call, "observation": c.binding(proof, root)})
        connection = {"connection_id": connection_id, "client_endpoint": call["client_endpoint"], "server_endpoint": call["server_endpoint"], "opened_at_utc": at(3), "closed_at_utc": at(7)}
        proof = old.write(capture_root / "observations" / f"connection-{index+1}.json", connection)
        connections.append({**connection, "observation": c.binding(proof, capture_root)})
        mapping = {"socket_call_id": call["socket_call_id"], "connection_id": connection_id, "hops": [], "observed_at_utc": at(4)}
        proof = old.write(capture_root / "observations" / f"mapping-{index+1}.json", mapping)
        maps.append({**mapping, "observation": c.binding(proof, capture_root)})
        requests.append({k: v for k, v in {**legacy_row, "connection_id": connection_id}.items() if k not in {"session_id", "process_pid", "scenario_id"}})
    def lines(path: Path, rows: list[dict[str, Any]]) -> None:
        path.write_bytes(b"".join(c.canonical(row)+b"\n" for row in rows))
        path.chmod(0o600)
    lines(root / "socket-calls.jsonl", calls)
    lines(capture_root / "connections.jsonl", connections)
    lines(capture_root / "transport-map.jsonl", maps)
    lines(capture_root / "requests.jsonl", requests)
    journal = []
    for event, second in (("prepared", 0), ("monitor_ready", 1), ("product_started", 2), ("product_stopped", 10), ("state_restored", 11), ("closed", 12)):
        row = {"event": event, "task_sha256": task["task_sha256"], "observed_at_utc": at(second)}
        proof = old.write(root / "observations" / f"journal-{event}.json", row)
        journal.append({**row, "observation": c.binding(proof, root)})
    lines(root / "recorder-journal.jsonl", journal)
    old.write(root / "monitor-ready.json", {"task_sha256": task["task_sha256"], "deployment": deployments["mac"], "started_at_utc": at(1)})
    for name in ("state-before.bin", "state-after.bin"):
        (root / name).write_bytes(b"fixture-original-user-state\n")
        (root / name).chmod(0o600)
    old.write(root / "state-restoration.json", {"task_sha256": task["task_sha256"], "status": "restored", "before": c.binding(root / "state-before.bin", root), "after": c.binding(root / "state-after.bin", root), "journal": c.binding(root / "recorder-journal.jsonl", root)})
    old.write(root / "recorder-execution.json", {"recorder_id": old.RECORDER, "task_sha256": task["task_sha256"], "run_id": task["task_nonce"], "status": kwargs.get("status", "complete"), "deployment": deployments["mac"], "monitor_started_at_utc": at(1), "journal": c.binding(root / "recorder-journal.jsonl", root)})
    budget = old.write(capture_root / "candidate-budget-reservation.json", {"fixture_only": True, "task_sha256": task["task_sha256"], "request_budget": task["execution"]["request_budget"]})
    reservation = old.write(capture_root / "reservation.json", {"schema_version": source.RESERVATION_SCHEMA, "task_sha256": task["task_sha256"], "candidate": task["candidate"], "capture_id": task["execution"]["capture_id"], "producer_run_id": task["execution"]["producer_run_id"], "endpoint": task["execution"]["endpoint"], "request_budget": task["execution"]["request_budget"], "reserved_at_utc": at(0), "deadline_utc": policy["deadline_utc"], "budget_reservation": c.binding(budget, capture_root), "source_deployments": deployments})
    ready = {"schema_version": source.READY_SCHEMA, "task_sha256": task["task_sha256"], "capture_id": task["execution"]["capture_id"], "reservation": c.binding(reservation, capture_root), "deployment": deployments["capture"], "ready_at_utc": at(.5)}
    proof = old.write(capture_root / "observations" / "ready.json", ready)
    old.write(capture_root / "ready.json", {**ready, "observation": c.binding(proof, capture_root)})
    for origin_root, side in ((root, "mac"), (capture_root, "capture")):
        proof = old.write(origin_root / "source-attestation.json", {"fixture_only": True, "side": side, "task_sha256": task["task_sha256"], "deployment": deployments[side]})
        old.write(origin_root / "source-origin.json", {"task_sha256": task["task_sha256"], "deployment": deployments[side], "attestation": c.binding(proof, origin_root)})
    old.write(capture_root / "capture-manifest.json", {"schema_version": c.CAPTURE_SCHEMA, "task_sha256": task["task_sha256"], "capture_id": task["execution"]["capture_id"], "producer_run_id": task["execution"]["producer_run_id"], "candidate": task["candidate"], "endpoint": task["execution"]["endpoint"], "status": kwargs.get("status", "complete"), **{k: c.binding(capture_root / v, capture_root) for k, v in {"requests":"requests.jsonl", "connections":"connections.jsonl", "transport_map":"transport-map.jsonl", "reservation":"reservation.json", "ready":"ready.json", "source_origin":"source-origin.json"}.items()}})
    files = [c.binding(p, root) for p in sorted(root.rglob("*")) if p.is_file() and p.name != "source-manifest.json"]
    old.write(root / "source-manifest.json", {"schema_version": c.BUNDLE_SCHEMA, "task_sha256": task["task_sha256"], "run_id": task["task_nonce"], "source_scope": task["execution"]["source_scope"], "status": kwargs.get("status", "complete"), "files": files})
    if task["execution"]["source_scope"] == "formal":
        index = {"schema_version": capture_control.INDEX_SCHEMA, "client_id": client_id, **{k: task[k] for k in c.COORDINATES}, "task": c.binding(task_path, attempt_root), "task_sha256": task["task_sha256"], "capture_root": str(capture_root), "capture_id": task["execution"]["capture_id"], "producer_run_id": task["execution"]["producer_run_id"], "reservation": c.binding(reservation, capture_root), "source_deployments": deployments}
        old.write(attempt_root / "client-evidence" / "reservations" / f"{index['producer_run_id']}.json", index)
    return task_path, root, capture_root


def write_receipt(attempt_root: Path, client_id: str, values: Mapping[str, Any]) -> Path:
    task_path, bundle, capture_root = material(attempt_root, client_id, values)
    task = c.read_json(task_path)
    verified = c.verify_client_bundle(task, bundle, capture_root)
    run = old.write(bundle.parent / "run.json", {"schema_version": source.RUN_SCHEMA, "client_id": client_id, "task": c.binding(task_path, attempt_root), "source_manifest": c.binding(bundle / "source-manifest.json", attempt_root), "source_scope": "formal", "producer_run_id": task["execution"]["producer_run_id"], "reservation": c.binding(capture_root / "reservation.json", capture_root)})
    project = old.fixture_ledger(attempt_root)
    keys = sorted(row["identity_key"] for row in verified["requests"])
    operation = "client-import:" + task["execution"]["producer_run_id"]
    payload = {"campaign_id": task["campaign_id"], "task_sha256": task["task_sha256"], "subject_kind": "client_capture", "subject_id": task["execution"]["producer_run_id"], "candidate_id": task["candidate_id"], "phase": "candidate", "request": {"status": "resolved", "identity_keys": keys, "estimated_delta": 0}}
    batch_sha = c.digest(payload)
    ledger.append_project_event(project, operation_id=operation, event_type="reconciliation_committed", payload=payload, source_batch_sha256=batch_sha)
    accounting = old.write(bundle.parent / "accounting.json", {"schema_version": "claude-upgrade-client-accounting/v1", "status": "accounted", "task_sha256": task["task_sha256"], "identity_keys_sha256": c.digest(keys), "operation_id": operation, "batch_sha256": batch_sha, "project_ledger": str(project)})
    return old.write(attempt_root / "client-evidence" / f"{client_id}.json", {"schema_version": c.RECEIPT_SCHEMA, "client_id": client_id, **dict(values), "status": "success", "task": c.binding(task_path, attempt_root), "bundle": c.binding(bundle / "source-manifest.json", attempt_root), "imported_run": c.binding(run, attempt_root), "verification": verified, "accounting_binding": c.binding(accounting, attempt_root), "issued_at_utc": task["issued_at_utc"], "conclusion_scope": "formal"})
