"""Mac 客户端任务、来源包与收据的离线合同。

摘要证明完整性，来源适配器证明材料取得方式；两者都通过才允许正式引用。
产品记录器尚未交付，因此正式来源登记表默认为空。历史材料可以离线审计，但不能
只靠客户端名、Header 或自报 success 变成正式客户端证据。所有读侧重放同一入口。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from tools.official_client_capture.capturelib.model import ConfigurationError

TASK_SCHEMA = "claude-upgrade-client-task/v1"
BUNDLE_SCHEMA = "claude-upgrade-client-source-manifest/v1"
CAPTURE_SCHEMA = "claude-upgrade-client-server-capture/v1"
RECEIPT_SCHEMA = "claude-upgrade-client-receipt/v1"
CLIENTS = ("claude-desktop", "claude-vscode")
SOURCE_KIND = "claude_mac_candidate"
MODEL_ENDPOINTS = frozenset({"/v1/messages", "/v1/messages/count_tokens", "/v1/messages/batches"})
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COORDINATES = ("campaign_id", "candidate_id", "attempt_id", "run_nonce")
# 名称 → 实现 verify_origin(task, bundle_root, capture_root) 的受管来源适配器。
# 不提供从命令行动态导入模块的后门；真实 recorder 落地时须经受管源码登记。
SOURCE_ADAPTERS: dict[str, Any] = {}
TASK_FIELDS = frozenset({"schema_version", "client_id", *COORDINATES, "candidate_revision", "candidate", "catalog", "client_artifacts", "scenarios", "scenario_reuse", "execution", "issued_at_utc", "task_nonce", "task_sha256"})


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ConfigurationError(f"Mac 客户端证据：{message}")


def require_plain_path(path: Path) -> None:
    """根目录以上的链接也必须拒绝，防止通过父目录替换另一份证据。"""
    require(path.is_absolute() and ".." not in path.parts, "证据路径必须为无回退的绝对路径")
    require(all(not parent.is_symlink() for parent in (path, *path.parents)), f"证据路径包含符号链接：{path}")


def secure_file(root: Path, relative: str) -> Path:
    """逐级拒绝符号链接、越界与特殊文件；不先 resolve 掩盖路径上的链接。"""
    root = Path(root)
    require_plain_path(root)
    parts = PurePosixPath(str(relative)).parts
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(), "证据根必须是非符号链接绝对目录")
    require(bool(parts) and not PurePosixPath(str(relative)).is_absolute() and all(p not in {".", "..", ""} for p in parts), "附件路径越界")
    path = root
    for part in parts:
        path = path / part
        require(not path.is_symlink(), f"附件路径包含符号链接：{relative}")
    require(path.is_file() and stat.S_ISREG(path.stat().st_mode), f"附件缺失或不是普通文件：{relative}")
    return path


def read_json(path: Path) -> dict[str, Any]:
    path = Path(path)
    require_plain_path(path)
    require(not path.is_symlink() and path.is_file(), f"JSON 文件不可信：{path}")
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as error:
        raise ConfigurationError(f"Mac 客户端证据 JSON 非法：{path}") from error
    require(isinstance(value, dict), "JSON 顶层必须是对象")
    return value


def binding(path: Path, root: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    path = secure_file(root, relative)
    raw = path.read_bytes()
    return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def bound_file(root: Path, value: Any) -> Path:
    require(isinstance(value, Mapping) and set(value) == {"path", "sha256", "bytes"}, "附件绑定字段不完整")
    path = secure_file(root, str(value["path"]))
    require(binding(path, root) == dict(value), f"附件摘要或字节数漂移：{value['path']}")
    return path


def timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ConfigurationError("Mac 客户端证据时间非法") from error
    require(result.tzinfo is not None, "时间必须带时区")
    return result


def candidate_context(attempt_root: Path, catalog_entry_id: str) -> dict[str, Any]:
    """从当前 attempt 及其绑定的构建原件复算候选五项和实际源码 Catalog。"""
    from tools.official_client_capture import claude_upgrade

    attempt = read_json(secure_file(attempt_root, "attempt.json"))
    candidates = [parent for parent in attempt_root.parents if (parent / "campaign.json").is_file()]
    require(bool(candidates), "attempt 没有受管 Campaign 祖先")
    campaign = candidates[0]
    manifest = read_json(campaign / "campaign.json")
    require(attempt.get("campaign_id") == manifest.get("campaign_id"), "attempt 与 Campaign 身份不一致")
    identity = attempt.get("identity") or {}
    build_path = secure_file(campaign, f"candidates/{attempt['candidate_id']}/build-receipt.json")
    build = read_json(build_path)
    build_sha = binding(build_path, campaign)["sha256"]
    reference = identity.get("build_receipt") or {}
    require(reference.get("path") == build_path.relative_to(campaign).as_posix() and reference.get("sha256") == build_sha, "attempt 未绑定当前候选构建收据")
    require(build.get("candidate_id") == attempt["candidate_id"] and build.get("campaign_id") == manifest["campaign_id"] and identity.get("candidate_revision") == build.get("candidate_revision"), "构建原件的候选、Campaign 或 revision 不一致")
    source_root = Path(str(build.get("source", {}).get("root", "")))
    require_plain_path(source_root)
    require(claude_upgrade._directory_tree_digest(source_root) == build["source"]["tree_sha256"], "候选实际源码树与构建原件不符")
    gateway = attempt.get("candidate_gateway") or {}
    candidate = {"source_tree_sha256": build["source"]["tree_sha256"], "build_receipt_sha256": build_sha, "image_digest": build["image"]["manifest_digest"], "profile_digest": build["profile"]["profile_digest"], "gateway_sha256": digest(gateway)}
    for field, expected in (("source_tree_sha256", candidate["source_tree_sha256"]), ("manifest_digest", candidate["image_digest"]), ("profile_digest", candidate["profile_digest"])):
        require(identity.get(field) == expected, f"attempt 与当前构建的 {field} 不一致")
    catalog_path = secure_file(source_root, "backend/internal/officialegress/catalogdata/claude/official-ingress-catalog.json")
    entries = [row for row in read_json(catalog_path).get("entries", []) if isinstance(row, dict) and row.get("client_id") == catalog_entry_id]
    require(len(entries) == 1, "候选实际 OfficialIngressCatalog 没有唯一指定客户端")
    return {"attempt": attempt, "candidate_revision": build["candidate_revision"], "candidate": candidate, "catalog": {"sha256": binding(catalog_path, source_root)["sha256"], "entry": entries[0]}, "endpoint": gateway.get("base_url")}


def verify_task_attempt(attempt_root: Path, task: Mapping[str, Any]) -> None:
    """所有读侧核当前候选原件，移接任务或改写自报摘要都不能改换候选。"""
    current = candidate_context(attempt_root, task["catalog"]["entry"]["client_id"])
    require(all(current["attempt"].get(key) == task[key] for key in COORDINATES), "任务不是本候选 attempt")
    require(all(current[key] == task[key] for key in ("candidate_revision", "candidate", "catalog")), "任务候选身份或 Catalog 与当前构建不符")
    require(current["endpoint"] == task["execution"]["endpoint"], "任务 endpoint 不是当前候选网关")


def validate_task(document: Mapping[str, Any]) -> dict[str, Any]:
    """校验冻结任务的闭集、独立客户端版本及场景挑战。"""
    task = dict(document)
    require(set(task) == TASK_FIELDS and task.get("schema_version") == TASK_SCHEMA, "任务 schema 或字段闭集非法")
    require(task["client_id"] in CLIENTS, "客户端不受支持")
    for field in (*COORDINATES, "task_nonce"):
        require(isinstance(task[field], str) and ID_RE.fullmatch(task[field]), f"任务 {field} 非法")
    require(type(task["candidate_revision"]) is int and task["candidate_revision"] > 0, "候选 revision 非法")
    require(task["task_sha256"] == digest({k: v for k, v in task.items() if k != "task_sha256"}), "任务摘要不符")
    timestamp(task["issued_at_utc"])
    candidate = task["candidate"]
    require(isinstance(candidate, dict) and set(candidate) == {"source_tree_sha256", "build_receipt_sha256", "image_digest", "profile_digest", "gateway_sha256"}, "候选身份字段不完整")
    require(all(SHA_RE.fullmatch(str(value).removeprefix("sha256:")) for value in candidate.values()), "候选身份摘要非法")
    catalog = task["catalog"]
    require(isinstance(catalog, dict) and set(catalog) == {"sha256", "entry"} and SHA_RE.fullmatch(str(catalog["sha256"])), "Catalog 绑定非法")
    entry = catalog["entry"]
    require(isinstance(entry, dict) and all(entry.get(k) for k in ("client_id", "product", "version", "platform", "binary_sha256")), "Catalog 独立客户端条目缺失")
    expected_product = "claude-desktop" if task["client_id"] == "claude-desktop" else "claude-code-vscode"
    require(entry["product"] == expected_product and entry["platform"].startswith("darwin/"), "Catalog 客户端产品或平台不符")
    artifacts = task["client_artifacts"]
    require(isinstance(artifacts, list) and artifacts, "未冻结已审核的客户端制品")
    roles = set()
    for artifact in artifacts:
        require(isinstance(artifact, dict) and set(artifact) == {"role", "version", "platform", "sha256"}, "制品字段非法")
        require(artifact["role"] not in roles and artifact["version"] and artifact["platform"] == entry["platform"] and SHA_RE.fullmatch(str(artifact["sha256"])), "制品版本、平台或摘要非法")
        roles.add(artifact["role"])
    required_roles = {"application", "embedded_cli"} | ({"extension"} if task["client_id"] == "claude-vscode" else set())
    require(roles == required_roles, "应用、扩展与内置 CLI 必须分别冻结")
    embedded = next(item for item in artifacts if item["role"] == "embedded_cli")
    require(embedded["version"] == entry["version"] and embedded["sha256"] == entry["binary_sha256"], "内置 CLI 与候选实际 Catalog 不一致")
    scenarios = task["scenarios"]
    require(isinstance(task["scenario_reuse"], list), "场景复用清单必须是数组")
    require(isinstance(scenarios, list) and scenarios, "没有冻结客户端场景")
    seen, challenges = set(), set()
    for row in scenarios:
        require(isinstance(row, dict) and set(row) == {"id", "model", "challenge", "response_marker", "minimum_requests"}, "场景字段非法")
        require(ID_RE.fullmatch(str(row["id"])) and row["id"] not in seen and row["model"] and row["challenge"] not in challenges and len(str(row["challenge"])) >= 16 and row["response_marker"], "场景身份、模型或挑战非法")
        require(type(row["minimum_requests"]) is int and row["minimum_requests"] >= 1, "场景请求下限非法")
        seen.add(row["id"])
        challenges.add(row["challenge"])
    execution = task["execution"]
    require(isinstance(execution, dict) and set(execution) == {"endpoint", "credential_ref", "timeout_seconds", "request_budget", "recorder_id", "capture_id", "producer_run_id", "source_scope", "capture_root", "project_ledger"}, "执行冻结字段不完整")
    require(execution["source_scope"] in {"formal", "regression_fixture"}, "来源用途非法")
    require(str(execution["endpoint"]).startswith(("http://", "https://")) and "@" not in execution["endpoint"], "隔离 endpoint 非法")
    require(str(execution["credential_ref"]).startswith("private-ref:") and "\n" not in execution["credential_ref"], "仅接受私有凭据引用")
    require(all(type(execution[k]) is int and execution[k] > 0 for k in ("timeout_seconds", "request_budget")), "超时或请求预算非法")
    require(all(ID_RE.fullmatch(str(execution[k])) for k in ("recorder_id", "capture_id", "producer_run_id")), "记录器或捕获身份非法")
    require(Path(str(execution["capture_root"])).is_absolute(), "独立服务端捕获根必须为绝对路径")
    require(Path(str(execution["project_ledger"])).is_absolute(), "项目账本必须提前冻结绝对路径")
    return task


def _inventory(root: Path, manifest: Mapping[str, Any]) -> dict[str, Path]:
    rows = manifest.get("files")
    require(isinstance(rows, list) and rows, "来源包附件清单缺失")
    found = {}
    for row in rows:
        path = bound_file(root, row)
        require(row["path"] not in found, "附件清单有重复路径")
        found[row["path"]] = path
    actual = set()
    for path in root.rglob("*"):
        require(not path.is_symlink(), "来源包包含符号链接")
        require(path.is_dir() or stat.S_ISREG(path.stat().st_mode), "来源包包含特殊文件")
        if path.is_file() and path != root / "source-manifest.json":
            actual.add(path.relative_to(root).as_posix())
    require(actual == set(found), "来源包存在未登记或缺失的附件")
    return found


def verify_client_bundle(task: Mapping[str, Any], bundle_root: Path, candidate_evidence_root: Path, *, _depth: int = 0) -> dict[str, Any]:
    """逐文件、产品参与、独立捕获及场景关联全部重算；失败运行仍返回精确请求或账务未决。"""
    task = validate_task(task)
    require(_depth < 32, "客户端场景承接链过深或存在循环")
    root, capture_root = Path(bundle_root), Path(candidate_evidence_root)
    require(capture_root == Path(task["execution"]["capture_root"]), "服务端原件必须来自任务预约的独立捕获根")
    require(not capture_root.is_relative_to(root), "服务端原件不能来自 Mac 上传包")
    source = read_json(secure_file(root, "source-manifest.json"))
    require(set(source) == {"schema_version", "task_sha256", "run_id", "source_scope", "status", "files"} and source["schema_version"] == BUNDLE_SCHEMA, "来源清单 schema 非法")
    require(source["task_sha256"] == task["task_sha256"] and source["source_scope"] == task["execution"]["source_scope"], "来源清单不是本任务或用途不同")
    require(ID_RE.fullmatch(str(source["run_id"])) and source["status"] in {"complete", "failed", "truncated"}, "来源运行身份或状态非法")
    files = _inventory(root, source)
    required = {"client-identity.json", "process-session.json", "session-events.jsonl", "recorder-execution.json"}
    require(required <= set(files), "缺少进程、会话、制品或记录器原件")
    identity = read_json(files["client-identity.json"])
    require(identity == {"client_id": task["client_id"], "artifacts": task["client_artifacts"]}, "客户端制品、版本或产品身份不符")
    process = read_json(files["process-session.json"])
    require(set(process) == {"client_id", "session_id", "started_at_utc", "completed_at_utc", "processes"} and process["client_id"] == task["client_id"], "产品进程会话来源不符")
    start, end = timestamp(process["started_at_utc"]), timestamp(process["completed_at_utc"])
    require(timestamp(task["issued_at_utc"]) <= start <= end and (end-start).total_seconds() <= task["execution"]["timeout_seconds"], "会话不在冻结任务的运行窗口内")
    processes = process["processes"]
    require(isinstance(processes, list) and processes, "没有应用进程来源，CLI headers 不能代替产品参与")
    seen_pids, roles = set(), set()
    for item in processes:
        require(isinstance(item, dict) and set(item) == {"pid", "parent_pid", "role", "started_at_utc", "executable_sha256"}, "进程来源字段不完整")
        require(type(item["pid"]) is int and item["pid"] > 0 and item["pid"] not in seen_pids, "进程 PID 非法或重复")
        require((not seen_pids and item["parent_pid"] is None and item["role"] == "application") or (bool(seen_pids) and item["parent_pid"] in seen_pids), "进程父子链不闭合")
        require(item["role"] not in roles, "产品参与角色重复")
        artifact = next((a for a in task["client_artifacts"] if a["role"] == item["role"]), None)
        require(artifact is not None and artifact["sha256"] == item["executable_sha256"] and timestamp(item["started_at_utc"]) <= end, "进程执行文件或启动时间不符")
        roles.add(item["role"])
        seen_pids.add(item["pid"])
    require(roles == {row["role"] for row in task["client_artifacts"]}, "缺少真实应用、内置 CLI 或扩展参与链")
    recorder = read_json(files["recorder-execution.json"])
    require(recorder == {"recorder_id": task["execution"]["recorder_id"], "task_sha256": task["task_sha256"], "run_id": source["run_id"], "status": source["status"]}, "记录器执行记录不闭合")
    capture = read_json(secure_file(capture_root, "capture-manifest.json"))
    require(set(capture) == {"schema_version", "task_sha256", "capture_id", "producer_run_id", "candidate", "endpoint", "status", "requests", "reservation"} and capture["schema_version"] == CAPTURE_SCHEMA, "独立捕获清单 schema 非法")
    for key in ("capture_id", "producer_run_id", "endpoint"):
        require(capture[key] == task["execution"][key], f"独立捕获 {key} 不符")
    require(capture["task_sha256"] == task["task_sha256"] and capture["candidate"] == task["candidate"], "独立捕获未绑定本轮任务与候选")
    reservation = read_json(bound_file(capture_root, capture["reservation"]))
    require(reservation == {"task_sha256": task["task_sha256"], "candidate": task["candidate"], "capture_id": capture["capture_id"], "producer_run_id": capture["producer_run_id"], "reserved_at_utc": reservation.get("reserved_at_utc")}, "独立捕获预约不闭合")
    require(timestamp(task["issued_at_utc"]) <= timestamp(reservation["reserved_at_utc"]) <= start, "捕获预约晚于客户端请求窗口")
    require(capture["status"] in {"complete", "failed", "truncated"}, "独立捕获终态缺失")
    request_path = bound_file(capture_root, capture["requests"])
    try:
        requests = [json.loads(line) for line in request_path.read_bytes().splitlines() if line.strip()]
        events = [json.loads(line) for line in files["session-events.jsonl"].read_bytes().splitlines() if line.strip()]
    except (ValueError, UnicodeError) as error:
        raise ConfigurationError("Mac 客户端逐请求或会话记录非法") from error
    result_requests, auxiliary, coordinates = [], [], set()
    scenario_results = []
    for index, row in enumerate(requests):
        required_request = {"connection_id", "request_ordinal", "session_id", "process_pid", "scenario_id", "observed_at_utc", "host", "method", "path", "request_body_base64", "response_body_base64"}
        require(isinstance(row, dict) and set(row) == required_request, "逐请求字段不完整")
        require(ID_RE.fullmatch(str(row["connection_id"])) and type(row["request_ordinal"]) is int and row["request_ordinal"] >= 1, "原生请求去重坐标非法")
        coordinate = {"capture_id": capture["capture_id"], "connection_id": row["connection_id"], "request_ordinal": row["request_ordinal"]}
        require(digest(coordinate) not in coordinates, "独立捕获重复原生请求坐标")
        coordinates.add(digest(coordinate))
        require(row["session_id"] == process["session_id"] and row["process_pid"] in seen_pids and start <= timestamp(row["observed_at_utc"]) <= end, "请求与产品进程会话无关联")
        try:
            body = base64.b64decode(row["request_body_base64"], validate=True)
            response = base64.b64decode(row["response_body_base64"], validate=True)
            payload = json.loads(body) if body else {}
        except (ValueError, UnicodeError) as error:
            raise ConfigurationError("Mac 客户端原始请求或响应字节非法") from error
        path = str(row["path"]).split("?", 1)[0]
        model_request = row["method"] == "POST" and path in MODEL_ENDPOINTS
        counted = model_request or (row["host"] == "platform.claude.com" and row["method"] == "POST" and path == "/v1/oauth/token")
        if counted:
            item = {"identity_key": digest({"producer_run_id": capture["producer_run_id"], "source_kind": SOURCE_KIND, "native_coordinate": coordinate}), "producer_run_id": capture["producer_run_id"], "source_kind": SOURCE_KIND, "native_coordinate": coordinate, "source_path": str(request_path), "source_sha256": capture["requests"]["sha256"], "record_index": index, "host": row["host"], "method": row["method"], "path": path, "model": payload.get("model"), "scenario_id": row["scenario_id"]}
            (result_requests if model_request else auxiliary).append(item)
        row["decoded_body"] = body.decode("utf-8", errors="strict")
        row["decoded_response"] = response.decode("utf-8", errors="strict")
    inherited: dict[str, Any] = {}
    scenarios_by_id = {row["id"]: row for row in task["scenarios"]}
    for reuse in task["scenario_reuse"]:
        require(isinstance(reuse, dict) and set(reuse) == {"root", "task", "source_manifest", "scenario_ids"}, "场景复用绑定字段不完整")
        previous_root = Path(str(reuse["root"]))
        previous_task = validate_task(read_json(bound_file(previous_root, reuse["task"])))
        verify_task_attempt(previous_root, previous_task)
        previous_bundle = bound_file(previous_root, reuse["source_manifest"]).parent
        require(timestamp(previous_task["issued_at_utc"]) < timestamp(task["issued_at_utc"]), "场景复用必须指向更早冻结的任务")
        for key in (*COORDINATES, "client_id", "candidate", "catalog", "client_artifacts", "candidate_revision"):
            require(previous_task[key] == task[key], f"场景复用 {key} 已变化，必须重新取证")
        require(previous_task["execution"]["source_scope"] == task["execution"]["source_scope"], "夹具不能承接为正式来源")
        previous = verify_client_bundle(previous_task, previous_bundle, Path(previous_task["execution"]["capture_root"]), _depth=_depth + 1)
        if previous_task["execution"]["source_scope"] == "formal":
            verify_origin(previous_task, previous_bundle, Path(previous_task["execution"]["capture_root"]))
            from tools.official_client_capture import claude_upgrade_client_receipts

            accounting = read_json(previous_bundle.parent / "accounting.json")
            claude_upgrade_client_receipts.verify_accounting(accounting, previous_task, sorted(row["identity_key"] for row in previous["requests"]))
        require(previous["accounting_status"] == "resolved", "账务未决运行不能承接场景")
        previous_scenarios = {row["id"]: row for row in previous_task["scenarios"]}
        require(isinstance(reuse["scenario_ids"], list) and reuse["scenario_ids"], "场景复用闭集为空")
        for scenario_id in reuse["scenario_ids"]:
            rows = [row for row in previous["scenario_results"] if row["scenario_id"] == scenario_id and row["status"] == "pass"]
            require(scenario_id not in inherited and scenario_id in scenarios_by_id and previous_scenarios.get(scenario_id) == scenarios_by_id[scenario_id] and len(rows) == 1, "只允许承接身份/条件不变且已通过的场景")
            inherited[scenario_id] = {**rows[0], "reused_from_task_sha256": previous_task["task_sha256"]}
    require(not ({row["scenario_id"] for row in requests} & set(inherited)), "新运行不得重发已批准复用的场景")
    for scenario in task["scenarios"]:
        if scenario["id"] in inherited:
            scenario_results.append(inherited[scenario["id"]])
            continue
        matches = [row for row in requests if row["scenario_id"] == scenario["id"] and row["method"] == "POST" and str(row["path"]).split("?", 1)[0] in MODEL_ENDPOINTS]
        event_matches = [event for event in events if isinstance(event, dict) and event.get("session_id") == process["session_id"] and event.get("scenario_id") == scenario["id"]]
        passed = len(matches) >= scenario["minimum_requests"] and all(scenario["challenge"] in row["decoded_body"] and json.loads(row["decoded_body"]).get("model") == scenario["model"] for row in matches)
        passed = passed and any(scenario["response_marker"] in row["decoded_response"] for row in matches)
        passed = passed and any(event.get("kind") == "input" and scenario["challenge"] in str(event.get("text", "")) for event in event_matches) and any(event.get("kind") == "completed" and scenario["response_marker"] in str(event.get("text", "")) for event in event_matches)
        scenario_results.append({"scenario_id": scenario["id"], "status": "pass" if passed else "failed", "request_count": len(matches)})
    resolved = source["status"] != "truncated" and capture["status"] != "truncated"
    # 超预算已经发生的真实请求仍全部入账，不能为了拒绝验收而漏记。
    succeeded = resolved and len(result_requests) <= task["execution"]["request_budget"] and source["status"] == capture["status"] == "complete" and all(row["status"] == "pass" for row in scenario_results)
    return {"status": "verified" if succeeded else "failed", "accounting_status": "resolved" if resolved else "unresolved", "source_scope": source["source_scope"], "run_id": source["run_id"], "task_sha256": task["task_sha256"], "requests": result_requests, "auxiliary_requests": auxiliary, "scenario_results": scenario_results, "source_manifest": binding(root / "source-manifest.json", root), "capture_manifest": binding(capture_root / "capture-manifest.json", capture_root)}


def verify_origin(task: Mapping[str, Any], bundle_root: Path, capture_root: Path) -> None:
    """正式材料还须经已登记来源适配器验证。默认没有任何真实 recorder。"""
    name = str(task["execution"]["recorder_id"])
    adapter = SOURCE_ADAPTERS.get(name)
    require(adapter is not None, f"真实来源适配器尚未登记：{name}；离线结构通过不能签发真机成功")
    adapter.verify_origin(task, Path(bundle_root), Path(capture_root))


def verify_client_receipt(attempt_root: Path, client_id: str, path: Path) -> dict[str, Any]:
    """seal、封存读侧、accept 与驱动共用；旧六字段成功收据明确拒绝。"""
    root = Path(attempt_root)
    require(client_id in CLIENTS, "客户端不受支持")
    path = secure_file(root, Path(path).relative_to(root).as_posix())
    receipt = read_json(path)
    fields = {"schema_version", "client_id", *COORDINATES, "status", "task", "bundle", "verification", "accounting_binding", "issued_at_utc", "conclusion_scope"}
    require(set(receipt) == fields and receipt["schema_version"] == RECEIPT_SCHEMA, "拒绝占位成功：客户端收据缺少正式 schema 或附件合同")
    require(receipt["client_id"] == client_id and receipt["status"] == "success" and receipt["conclusion_scope"] == "formal", "客户端收据不是正式成功结论")
    task = validate_task(read_json(bound_file(root, receipt["task"])))
    require(all(receipt[key] == task[key] for key in (*COORDINATES, "client_id")), "收据与任务身份不符")
    verify_task_attempt(root, task)
    bundle_manifest = bound_file(root, receipt["bundle"])
    bundle_root = bundle_manifest.parent
    require(bundle_manifest.name == "source-manifest.json", "来源清单文件名不符")
    verified = verify_client_bundle(task, bundle_root, Path(task["execution"]["capture_root"]))
    require(task["execution"]["source_scope"] == "formal" and verified["status"] == "verified", "历史夹具或失败运行不能成为正式成功")
    verify_origin(task, bundle_root, Path(task["execution"]["capture_root"]))
    require(receipt["verification"] == verified, "收据验证结论与附件重放不符")
    accounting = read_json(bound_file(root, receipt["accounting_binding"]))
    keys = sorted(row["identity_key"] for row in verified["requests"])
    require(accounting.get("task_sha256") == task["task_sha256"] and accounting.get("identity_keys_sha256") == digest(keys) and accounting.get("status") == "accounted", "客户端请求尚未完成既有项目账务入账")
    from tools.official_client_capture import claude_upgrade_client_receipts

    claude_upgrade_client_receipts.verify_accounting(accounting, task, keys)
    return verified
