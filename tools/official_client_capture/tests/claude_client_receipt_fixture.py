"""完整 Mac 来源夹具：真实附件/请求验证器运行，只有来源适配器是显式离线实现。

适配器仅在测试作用域登记，证据限定系统临时目录，账本必须为 staging 内的
fixture_only。它不读取或操作实际应用；构造的材料只证明离线合同可重放。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture.tests.project_ledger_fixture import FIXTURE_ONLY_ENV

RECORDER = "fixture-recorded-client"
NOW = "2026-10-08T00:00:00Z"


def write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(contract.canonical(value) + b"\n")
    path.chmod(0o600)
    return path


def _recorded_layout(path: Path, temp: Path) -> tuple[Path, Path] | None:
    """原 runner 的准确夹具根与 staging 外壳；普通单测不走此接缝。"""
    # 录制世界用 mkdtemp 直接建在 runner 的 staging 下；只接受该
    # runner 的三处准确坐标一致，不把任意名为 staging 的祖先当授权。
    tree_value = os.environ.get("CLAUDE_CONTROL_CHAIN_TREE")
    records_value = os.environ.get("CLAUDE_RECORDED_CHAIN_RECORD_DIR")
    if temp.name == "staging" and tree_value and records_value:
        tree, records = Path(tree_value).resolve(), Path(records_value).resolve()
        contract.require(tree == temp / "tree" and records == temp / "recorded-worlds",
                         "录制 Mac 来源的原 tree、记录根与 TMPDIR 不一致")
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        method_root = stops.record_root()
        contract.require(method_root is not None and method_root.parent == records / "method-records",
                         "录制 Mac 来源缺同一 runner 的原方法记录目录")
        roots = [saved["root"] for name in ("capture", "batch")
                 if (saved := stops.load_state("fixture:" + name)) is not None]
        for root in roots:
            if root.parent == temp and path.is_relative_to(root):
                return root, temp.parent
        candidate = stops.load_state("candidate-recorded-world")
        if candidate is not None:
            root = candidate["root"]
            if root.parent == temp and candidate["data"] == root / "staging/data" and path.is_relative_to(root / "staging"):
                return root, root
        contract.require(False, "录制 Mac 来源不属于原方法的夹具世界")
    return None


def temporary_root(path: Path) -> Path:
    """从实际临时树识别 staging 边界；runner 的 TMPDIR 本身可就是 staging。"""
    temp = Path(tempfile.gettempdir()).resolve()
    resolved = path.resolve()
    contract.require(resolved.is_relative_to(temp), "fixture source 只允许系统临时目录")
    recorded = _recorded_layout(resolved, temp)
    if recorded is not None:
        return recorded[1]
    contract.require(resolved != temp, "fixture source 必须位于独立临时世界")
    return temp / resolved.relative_to(temp).parts[0]


def require_same_world(first: Path, second: Path) -> None:
    """Mac包与捕获根必须属于同一原世界，不能借共享 staging 混入兄弟夹具。"""
    contract.require(temporary_root(first) == temporary_root(second), "fixture 两侧原件越出同一临时根")
    temp = Path(tempfile.gettempdir()).resolve()
    left, right = _recorded_layout(first.resolve(), temp), _recorded_layout(second.resolve(), temp)
    contract.require(left is None and right is None or left is not None and right is not None and left[0] == right[0],
                     "录制 Mac 两侧原件不属于同一原夹具世界")


def fixture_ledger(path: Path) -> Path:
    """沿用 Campaign 实际账本；最小世界缺账本时只允许在其 staging 数据根原位创建。"""
    staging = temporary_root(path) / "staging"
    campaign = next((parent for parent in path.parents if (parent / "campaign.json").is_file()), None)
    contract.require(campaign is not None and campaign.is_relative_to(staging), "Mac 夹具 Campaign 必须在同一临时 staging 树")
    root = ledger.find_project_ledger(campaign, client="claude")
    if root is None:
        contract.require(not os.environ.get("CLAUDE_FAULT_METHOD_RECORD_DIR"),
                         "录制 Mac 夹具缺少原 Campaign 的项目账本，不能重建")
        data_root = campaign.parents[2] if campaign.parent.name == "campaigns" and campaign.parent.parent.name == "evidence" else campaign
        data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        data_root.chmod(0o700)
        root = ledger.create_fixture_ledger(data_root, client="claude")
    contract.require(root.is_relative_to(staging), "Mac 夹具不得改用 Campaign 之外的项目账本")
    plan, _raw = ledger._load_plan(root)
    contract.require(plan.get("fixture_only") is True and plan.get("client") == "claude", "Mac 夹具必须沿用 Campaign 的 Claude fixture_only 总账")
    return root


def install_staging_scope(test_case: Any) -> None:
    """既有测试创建数据前启用 staging 临时根；账本仍由原 Campaign 注册流程建立。"""
    outer = tempfile.TemporaryDirectory()
    test_case.addCleanup(outer.cleanup)
    staging = Path(outer.name).resolve() / "staging"
    staging.mkdir(mode=0o700)
    original = tempfile.TemporaryDirectory

    def temporary(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("dir", staging)
        return original(*args, **kwargs)

    patch = mock.patch.object(tempfile, "TemporaryDirectory", side_effect=temporary)
    patch.start()
    test_case.addCleanup(patch.stop)
    environment = mock.patch.dict(os.environ, {FIXTURE_ONLY_ENV: "1"})
    environment.start()
    test_case.addCleanup(environment.stop)


class FixtureSourceAdapter:
    """来源适配器验证隔离边界；产品/请求/场景/附件仍由正式合同逐项核对。"""

    def verify_origin(self, task: Mapping[str, Any], bundle_root: Path, capture_root: Path) -> None:
        root = temporary_root(bundle_root)
        require_same_world(bundle_root, capture_root)
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
    from tools.official_client_capture.tests.claude_mac_v2_fixture import FixturePatch
    return FixturePatch()


@contextlib.contextmanager
def source_scope() -> Iterator[None]:
    with source_patch():
        yield


def candidate_inputs(attempt_root: Path, values: Mapping[str, Any], client_id: str) -> dict[str, Any]:
    """仅补齐无正式合同的历史夹具；真实 attempt 逐项验证，保持冻结原件字节不变。"""
    from tools.official_client_capture import claude_upgrade

    path = attempt_root / "attempt.json"
    attempt = contract.read_json(path) if path.exists() else {}
    contract.require(isinstance(attempt.get("identity", {}), dict), "fixture attempt identity 必须是对象")
    protected = "schema_version" in attempt or "attempt_digest" in attempt or "schema_version" in attempt.get("identity", {})
    for key in contract.COORDINATES:
        contract.require(key in values and (key not in attempt and not protected or attempt.get(key) == values[key]), f"fixture attempt 的 {key} 与调用坐标不符")
    campaign = next((parent for parent in attempt_root.parents if (parent / "campaign.json").is_file()), temporary_root(attempt_root))
    if not (campaign / "campaign.json").exists():
        contract.require(not protected, "正式 attempt 缺少 Campaign 原件，不能由夹具补写")
        write(campaign / "campaign.json", {"campaign_id": values["campaign_id"], "client": "claude"})
    build_path = campaign / "candidates" / values["candidate_id"] / "build-receipt.json"
    if not build_path.exists():
        contract.require(not protected, "正式 attempt 缺少构建原件，不能由夹具补写")
        source_root = temporary_root(attempt_root) / "fixture-candidate-source" / values["candidate_id"]
        entries = [{"client_id": f"{client}-fixture", "product": "claude-desktop" if client == "claude-desktop" else "claude-code-vscode", "version": "fixture-cli-1", "platform": "darwin/arm64", "binary_sha256": "2" * 64} for client in contract.CLIENTS]
        write(source_root / "backend/internal/officialegress/catalogdata/claude/official-ingress-catalog.json", {"entries": entries})
        write(build_path, {"campaign_id": values["campaign_id"], "candidate_id": values["candidate_id"], "candidate_revision": 1, "source": {"root": str(source_root), "tree_sha256": claude_upgrade._directory_tree_digest(source_root)}, "image": {"manifest_digest": "sha256:" + "a" * 64}, "profile": {"profile_digest": "a" * 64}})
    build = contract.read_json(build_path)
    catalog = contract.read_json(Path(build["source"]["root"]) / "backend/internal/officialegress/catalogdata/claude/official-ingress-catalog.json")
    product = "claude-desktop" if client_id == "claude-desktop" else "claude-code-vscode"
    entry = next(row for row in catalog["entries"] if row["product"] == product and row["platform"] == "darwin/arm64")
    expected_identity = {"candidate_revision": build["candidate_revision"], "source_tree_sha256": build["source"]["tree_sha256"], "manifest_digest": build["image"]["manifest_digest"], "profile_digest": build["profile"]["profile_digest"]}
    expected_build = {"path": build_path.relative_to(campaign).as_posix(), "sha256": contract.binding(build_path, campaign)["sha256"]}
    identity = attempt.get("identity", {})
    contract.require(isinstance(identity, dict), "fixture attempt identity 必须是对象")
    for key, expected in expected_identity.items():
        contract.require(key not in identity and not protected or identity.get(key) == expected, f"fixture attempt 的 {key} 与构建原件不符")
    reference = identity.get("build_receipt", {})
    contract.require(isinstance(reference, dict), "fixture attempt build_receipt 必须是对象")
    for key, expected in expected_build.items():
        contract.require(key not in reference and not protected or reference.get(key) == expected, f"fixture attempt 构建绑定的 {key} 不符")
    if protected:
        gateway = attempt.get("candidate_gateway", {})
        contract.require(isinstance(gateway, dict) and bool(gateway.get("base_url")), "正式 attempt 缺少候选网关，不能由夹具补写")
        if "attempt_digest" in attempt:
            contract.require(attempt["attempt_digest"] == claude_upgrade._fingerprint({key: value for key, value in attempt.items() if key != "attempt_digest"}), "正式 attempt 自摘要不符，不能由夹具修复")
    else:
        # 历史最小夹具可以补缺字段；既有值和扩展绑定（例如 receipt_digest）始终保留。
        for key in contract.COORDINATES:
            attempt.setdefault(key, values[key])
        identity = attempt.setdefault("identity", {})
        for key, expected in expected_identity.items():
            identity.setdefault(key, expected)
        reference = identity.setdefault("build_receipt", {})
        for key, expected in expected_build.items():
            reference.setdefault(key, expected)
        attempt.setdefault("candidate_gateway", {"base_url": "http://candidate.invalid:8080"})
        if not path.exists() or contract.read_json(path) != attempt:
            write(path, attempt)
    return contract.candidate_context(attempt_root, entry["client_id"])


def material_v1(attempt_root: Path, client_id: str, values: Mapping[str, Any], *, scope: str = "formal", status: str = "complete", nonce: str | None = None, scenarios: list[dict[str, Any]] | None = None, scenario_reuse: list[dict[str, Any]] | None = None, issued_at_utc: str = NOW) -> tuple[Path, Path, Path]:
    """生成独立 Mac 包与 server 原件；故意不把服务端原件放进 Mac 上传包。"""
    nonce = nonce or f"{client_id}-task-{contract.digest(dict(values))[:12]}"
    client_root = attempt_root / "client-evidence" / client_id
    task_path = client_root / "tasks" / nonce / "task.json"
    bundle_root = client_root / "runs" / nonce / "source"
    project = fixture_ledger(attempt_root)
    # 捕获原件从创建时就在实际账本的数据根内。账本身份已由上面的
    # helper 核过；这不是从 task 的绝对路径字符串取得归档权限。
    capture_root = project.parent / "server-originals" / contract.digest(dict(values))[:12] / nonce
    if task_path.exists():
        frozen = contract.read_json(contract.secure_file(attempt_root, task_path.relative_to(attempt_root).as_posix()))
        if frozen.get("schema_version") == contract.HISTORICAL_TASK_SCHEMA:
            contract._validate_task_v1(frozen)
        else:
            contract.validate_task(frozen)
        contract.require(frozen["client_id"] == client_id and frozen["task_nonce"] == nonce
                         and all(frozen[key] == values[key] for key in contract.COORDINATES),
                         "已有 fixture task 的冻结坐标与本次不同，禁止重建")
        contract.require(frozen["execution"]["capture_root"] == str(capture_root)
                         and frozen["execution"]["project_ledger"] == str(project),
                         "已有 fixture task 的捕获根已冻结，禁止迁移或重写")
        # 已发布世界只读核对当前候选原件；不得调用会补历史字段的创建路径。
        contract.verify_task_attempt(attempt_root, frozen)
        return task_path, bundle_root, capture_root
    current = candidate_inputs(attempt_root, values, client_id)
    # 逐级新建私有目录；递归 mkdir 的 mode 只作用于末级，不能留下 755 的客户端证据或 runs 父目录。
    # 重入时只核已有目录权限，不 chmod 已发布的来源世界。
    for directory in (client_root.parent, client_root, client_root / "tasks", client_root / "runs", bundle_root.parent):
        directory.mkdir(mode=0o700, exist_ok=True)
        contract.require(directory.is_dir() and not directory.is_symlink() and directory.stat().st_mode & 0o077 == 0,
                         "fixture 客户端目录必须保持私有")
    for directory in (capture_root.parent.parent, capture_root.parent, capture_root):
        directory.mkdir(mode=0o700, exist_ok=True)
        contract.require(directory.is_dir() and not directory.is_symlink() and directory.stat().st_mode & 0o077 == 0,
                         "fixture 捕获目录必须保持私有")
    artifacts = [{"role": "application", "version": "fixture-app-1", "platform": "darwin/arm64", "sha256": "1" * 64}, {"role": "embedded_cli", "version": current["catalog"]["entry"]["version"], "platform": "darwin/arm64", "sha256": current["catalog"]["entry"]["binary_sha256"]}]
    if client_id == "claude-vscode":
        artifacts.append({"role": "extension", "version": "fixture-extension-1", "platform": "darwin/arm64", "sha256": "3" * 64})
    task = {"schema_version": contract.HISTORICAL_TASK_SCHEMA, "client_id": client_id, **dict(values), "candidate_revision": 1, "candidate": {key: "a" * 64 for key in ("source_tree_sha256", "build_receipt_sha256", "image_digest", "profile_digest", "gateway_sha256")}, "catalog": {"sha256": "b" * 64, "entry": {"client_id": f"{client_id}-fixture", "product": "claude-desktop" if client_id == "claude-desktop" else "claude-code-vscode", "version": "fixture-cli-1", "platform": "darwin/arm64", "binary_sha256": "2" * 64}}, "client_artifacts": artifacts, "scenarios": [{"id": "client-smoke", "model": "fixture-model", "challenge": "fixture-unique-challenge-1234", "response_marker": "fixture-response", "minimum_requests": 1}], "execution": {"endpoint": "http://candidate.invalid:8080", "credential_ref": "private-ref:fixture", "timeout_seconds": 300, "request_budget": 10, "recorder_id": RECORDER, "capture_id": nonce, "producer_run_id": f"producer-{nonce}", "source_scope": scope, "capture_root": str(capture_root)}, "issued_at_utc": NOW, "task_nonce": nonce}
    task.update({key: current[key] for key in ("candidate", "candidate_revision", "catalog")})
    task["execution"]["endpoint"] = current["endpoint"]
    task["execution"]["project_ledger"] = str(project)
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
    files = [contract.binding(path, bundle_root) for path in sorted(bundle_root.rglob("*")) if path.is_file() and path.name != "source-manifest.json"]
    write(bundle_root / "source-manifest.json", {"schema_version": contract.HISTORICAL_BUNDLE_SCHEMA, "task_sha256": task["task_sha256"], "run_id": nonce, "source_scope": scope, "status": status, "files": files})
    reservation = write(capture_root / "reservation.json", {"task_sha256": task["task_sha256"], "candidate": task["candidate"], "capture_id": nonce, "producer_run_id": f"producer-{nonce}", "reserved_at_utc": issued_at_utc})
    rows = []
    for index, scenario in enumerate(executing, start=1):
        body = contract.canonical({"model": scenario["model"], "messages": [{"role": "user", "content": scenario["challenge"]}]})
        rows.append({"connection_id": "connection-1", "request_ordinal": index, "session_id": "fixture-session", "process_pid": 100, "scenario_id": scenario["id"], "observed_at_utc": (moment + timedelta(seconds=5)).isoformat(), "host": "candidate.invalid", "method": "POST", "path": "/v1/messages", "request_body_base64": base64.b64encode(body).decode(), "response_body_base64": base64.b64encode(scenario["response_marker"].encode()).decode()})
    requests = capture_root / "requests.jsonl"
    requests.write_bytes(b"".join(contract.canonical(row) + b"\n" for row in rows))
    requests.chmod(0o600)
    write(capture_root / "capture-manifest.json", {"schema_version": contract.HISTORICAL_CAPTURE_SCHEMA, "task_sha256": task["task_sha256"], "capture_id": nonce, "producer_run_id": f"producer-{nonce}", "candidate": task["candidate"], "endpoint": task["execution"]["endpoint"], "status": status, "requests": contract.binding(requests, capture_root), "reservation": contract.binding(reservation, capture_root)})
    fixture_ledger(attempt_root)
    return task_path, bundle_root, capture_root


def write_receipt_v1(attempt_root: Path, client_id: str, values: Mapping[str, Any]) -> Path:
    """为既有 seal/accept 世界迁移完整输入夹具；真实读侧仍重放全部附件及账本事件。"""
    task_path, bundle, capture = material_v1(attempt_root, client_id, values)
    task = contract.read_json(task_path)
    verified = contract._verify_client_bundle_v1(task, bundle, capture)
    write(bundle.parent / "run.json", {"schema_version": "claude-upgrade-client-imported-run/v1", "client_id": client_id, "task": contract.binding(task_path, attempt_root), "source_manifest": contract.binding(bundle / "source-manifest.json", attempt_root), "source_scope": "formal", "producer_run_id": task["execution"]["producer_run_id"]})
    root = fixture_ledger(attempt_root)
    keys = sorted(row["identity_key"] for row in verified["requests"])
    operation = "client-import:" + task["execution"]["producer_run_id"]
    payload = {"campaign_id": task["campaign_id"], "task_sha256": task["task_sha256"], "subject_kind": "client_capture", "subject_id": task["execution"]["producer_run_id"], "candidate_id": task["candidate_id"], "phase": "candidate", "request": {"status": "resolved", "identity_keys": keys, "estimated_delta": 0}}
    batch_sha = contract.digest(payload)
    ledger.append_project_event(root, operation_id=operation, event_type="reconciliation_committed", payload=payload, source_batch_sha256=batch_sha)
    accounting = write(bundle.parent / "accounting.json", {"schema_version": "claude-upgrade-client-accounting/v1", "status": "accounted", "task_sha256": task["task_sha256"], "identity_keys_sha256": contract.digest(keys), "operation_id": operation, "batch_sha256": batch_sha, "project_ledger": str(root)})
    return write(attempt_root / "client-evidence" / f"{client_id}.json", {"schema_version": contract.HISTORICAL_RECEIPT_SCHEMA, "client_id": client_id, **dict(values), "status": "success", "task": contract.binding(task_path, attempt_root), "bundle": contract.binding(bundle / "source-manifest.json", attempt_root), "verification": verified, "accounting_binding": contract.binding(accounting, attempt_root), "issued_at_utc": NOW, "conclusion_scope": "formal"})


def material(attempt_root: Path, client_id: str, values: Mapping[str, Any], **kwargs: Any) -> tuple[Path, Path, Path]:
    """当前消费测试生成完整 v2；形成期夹具由 material_v1 显式生成。"""
    from tools.official_client_capture.tests import claude_mac_v2_fixture
    return claude_mac_v2_fixture.material(attempt_root, client_id, values, **kwargs)


def write_receipt(attempt_root: Path, client_id: str, values: Mapping[str, Any]) -> Path:
    from tools.official_client_capture.tests import claude_mac_v2_fixture
    return claude_mac_v2_fixture.write_receipt(attempt_root, client_id, values)
