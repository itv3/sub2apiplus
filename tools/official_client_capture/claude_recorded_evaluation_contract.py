"""离线录制评估的分域合同；只消费冻结输入，不授予真实客户端或正式版本资格。

旧 v1 候选仍只观察 gateway。新视图必须由来源 state 显式绑定本合同：client_shared 保留官方原件，gateway
保持已有纯出站投影，两域按原连接互斥划分并只形成一个逻辑 probe。混合连接、未知端点和非夹具总账均失败关闭。
生产者、seal、compare、checker、逐规则投影与恢复共同调用本模块，不以标签或外部回调启用新语义。
"""

from __future__ import annotations

import copy
import hashlib
import http.client
import io
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

POLICY_SCHEMA = "claude-recorded-evaluation-policy/v1"
PROBE_SCHEMA = "claude-recorded-evaluation-probe/v1"
EVALUATION_SCHEMA = "claude-evaluation-capture-manifest/v2"
POLICY_KEY = "recorded_evaluation_policy"
PROBE_KEY = "recorded_evaluation_contract"
PROBE_FILENAME = "recorded-evaluation-contract.json"
ORIGINAL_DIRECTORY = "recorded-official-originals"
SHARED_REQUESTS = frozenset({"HEAD /api/hello", "GET /api/claude_code/policy_limits", "GET /api/claude_code/settings",
                             "GET /api/oauth/profile", "GET /v1/mcp_servers"})
GATEWAY_REQUESTS = frozenset({"POST /v1/messages", "POST /v1/messages/count_tokens", "POST /v1/oauth/token"})
QUALIFICATIONS = {"fixture_only": True, "current_client_executed": False, "formal_version_semantics_certified": False}
FROZEN_KEYS = ("scenarios", "labels", "version_facts")
SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")
OPERATION_RE = re.compile(r"(?:synthetic-job|job):([a-z0-9-]+):step-([1-9][0-9]*):attempt-([1-9][0-9]*)\Z")


class RecordedEvaluationError(RuntimeError):
    """显式录制分域输入没有闭合。"""


def _exact(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise RecordedEvaluationError(f"{label} 字段不闭合")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def policy_sha256(value: Mapping[str, Any]) -> str:
    """policy 自摘要只排除自身摘要字段；全部授权范围仍进入摘要。"""

    return hashlib.sha256(_canonical({key: item for key, item in value.items() if key != "policy_sha256"})).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordedEvaluationError("录制合同 JSON 有重复键")
        result[key] = value
    return result


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_pairs)
    except (OSError, ValueError) as error:
        raise RecordedEvaluationError(f"录制合同不能读取：{path}") from error
    if not isinstance(value, dict):
        raise RecordedEvaluationError("录制合同 JSON 必须是对象")
    return value


def binding(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def bound(value: Any, *, root: Path) -> Path:
    """同时核对闭集、坐标、逐段无链接、文件字节与摘要；不跟随被替换的路径。"""

    item = _exact(value, {"path", "sha256", "bytes"}, "录制文件绑定")
    path = Path(str(item["path"]))
    if not path.is_absolute():
        path = root / path
    if (".." in path.parts or path.resolve() != path or not path.is_relative_to(root)
            or not path.is_file() or path.is_symlink() or not SHA256_RE.fullmatch(str(item["sha256"]))
            or type(item["bytes"]) is not int or item["bytes"] < 0):
        raise RecordedEvaluationError("录制文件绑定路径或摘要非法")
    current = path
    while current != root:
        if current.is_symlink():
            raise RecordedEvaluationError("录制文件路径含符号链接")
        current = current.parent
    observed = binding(path)
    if any(observed[key] != item[key] for key in ("sha256", "bytes")):
        raise RecordedEvaluationError(f"录制文件摘要或字节数漂移：{path}")
    return path


def _root(state: Mapping[str, Any]) -> Path:
    root = Path(str(state.get("data_root", "")))
    if (not root.is_absolute() or root.resolve() != root or "staging" not in root.parts
            or state.get("schema_version") != "claude-recorded-chain-source/v1"
            or state.get("fixture_only") is not True or state.get("purpose") != "regression_fixture"
            or state.get("target_version") != "2.1.226"):
        raise RecordedEvaluationError("录制评估只接受隔离 staging 内的 2.1.226 夹具来源")
    return root


def _policy_inputs(state: Mapping[str, Any]) -> dict[str, Any]:
    inputs = state.get("inputs", {})
    return {key: copy.deepcopy(inputs["bindings"][key]) for key in FROZEN_KEYS}


def build_policy(source_state_path: Path, *, campaign_path: Path, project_plan_path: Path,
                 assertion_profile_path: Path) -> dict[str, Any]:
    """在真实 VC-3 之后形成唯一 policy 载荷；调用者负责只写一次，再创建仅补 policy 绑定的新 state。"""

    state = _read(source_state_path)
    root = _root(state)
    if POLICY_KEY in state["inputs"]["bindings"]:
        raise RecordedEvaluationError("policy 必须绑定尚未启用录制评估的原来源 state")
    payload = {"schema_version": POLICY_SCHEMA, "client": "claude", "purpose": "regression_fixture", **QUALIFICATIONS,
               "target_version": state["target_version"], "source_state": binding(source_state_path),
               "campaign": binding(campaign_path), "project_plan": binding(project_plan_path), "snapshot": state["snapshot"],
               "source_identity": {"platform": "linux/amd64", "version": state["target_version"],
                                   "binary_sha256": state["freeze"]["binary_sha256"]},
               "frozen_inputs": _policy_inputs(state),
               "jobs": {"official": state["inputs"]["jobs"], "candidate": state["inputs"]["candidate_jobs"]},
               "assertion_profile": binding(assertion_profile_path)}
    from tools.official_client_capture import claude_acceptance_contract as acceptance

    payload["acceptance_contract_sha256"] = acceptance.contract_sha256(acceptance.build_contract_payload(_read(assertion_profile_path)))
    payload["policy_sha256"] = policy_sha256(payload)
    _validate_policy(payload, root, state=state, activated=False)
    return payload


def _validate_policy(payload: Any, root: Path, *, state: Mapping[str, Any] | None = None,
                     activated: bool = True) -> dict[str, Any]:
    keys = {"schema_version", "client", "purpose", *QUALIFICATIONS, "target_version", "source_state", "campaign", "project_plan",
            "snapshot", "source_identity", "frozen_inputs", "jobs", "assertion_profile", "acceptance_contract_sha256", "policy_sha256"}
    policy = dict(_exact(payload, keys, "录制评估 policy"))
    if (policy["schema_version"] != POLICY_SCHEMA or policy["client"] != "claude" or policy["purpose"] != "regression_fixture"
            or any(policy[key] is not value for key, value in QUALIFICATIONS.items())
            or policy["target_version"] != "2.1.226" or policy["policy_sha256"] != policy_sha256(policy)):
        raise RecordedEvaluationError("录制评估 policy 的版本、夹具资格或自摘要非法")
    original = _read(bound(policy["source_state"], root=root))
    if _root(original) != root or POLICY_KEY in original["inputs"]["bindings"]:
        raise RecordedEvaluationError("policy 原来源 state 已启用或数据根不同")
    if state is not None:
        predecessor = copy.deepcopy(dict(state))
        if activated:
            predecessor["inputs"]["bindings"].pop(POLICY_KEY, None)
        if predecessor != original:
            raise RecordedEvaluationError("新来源 state 除唯一 policy 绑定外不得改变原来源合同")
    campaign_path = bound(policy["campaign"], root=root)
    campaign = _read(campaign_path)
    if (campaign_path != root / "evidence/campaigns" / str(campaign.get("campaign_id")) / "campaign.json"
            or campaign.get("client") != "claude" or campaign.get("target_version") != policy["target_version"]
            or campaign.get("target_sha256") != original["freeze"]["binary_sha256"]):
        raise RecordedEvaluationError("录制评估 policy 与当前 Claude Campaign 目标不同")
    plan_path = bound(policy["project_plan"], root=root)
    if plan_path != root / "upgrade-project-ledger-claude/plan.json":
        raise RecordedEvaluationError("录制评估必须绑定当前 Claude 项目总账")
    from tools.official_client_capture import codex_upgrade_project_ledger as ledger

    plan = _read(plan_path)
    try:
        ledger._validate_plan(plan)
    except ledger.ProjectLedgerError as error:
        raise RecordedEvaluationError(f"录制评估项目总账不合法：{error}") from error
    if plan.get("client") != "claude" or plan.get("fixture_only") is not True:
        raise RecordedEvaluationError("录制评估视图禁止用于正式非 fixture 项目总账")
    if policy["snapshot"] != original["snapshot"] or policy["frozen_inputs"] != _policy_inputs(original):
        raise RecordedEvaluationError("录制评估快照或冻结输入与原来源不同")
    bound(policy["snapshot"], root=root)
    _exact(policy["frozen_inputs"], set(FROZEN_KEYS), "录制冻结输入")
    for item in policy["frozen_inputs"].values():
        bound(item, root=root)
    identity = {"platform": "linux/amd64", "version": original["target_version"], "binary_sha256": original["freeze"]["binary_sha256"]}
    jobs = {"official": original["inputs"]["jobs"], "candidate": original["inputs"]["candidate_jobs"]}
    if policy["source_identity"] != identity or policy["jobs"] != jobs or original.get("jobs") != jobs["official"]:
        raise RecordedEvaluationError("录制评估平台、binary 或双侧 Job 来源漂移")
    profile_path = bound(policy["assertion_profile"], root=root)
    if profile_path != campaign_path.parent / "assertions/approved-assertion-profile.json":
        raise RecordedEvaluationError("录制评估必须使用当前 Campaign 真实冻结的批准画像")
    from tools.official_client_capture import claude_rule_assertion as assertion
    from tools.official_client_capture import claude_acceptance_contract as acceptance

    profile = assertion.validate_profile_document(_read(profile_path), expected_claude_version=policy["target_version"])
    digest = acceptance.contract_sha256(acceptance.build_contract_payload(profile))
    if digest != policy["acceptance_contract_sha256"] or digest != acceptance.FROZEN_CONTRACT_SHA256S[policy["target_version"]]:
        raise RecordedEvaluationError("录制评估不得改变原验收规则、模式或 check 全集")
    return policy


def optional_policy(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """只有显式输入绑定启用；标签、环境变量和调用者回调均不构成启用依据。"""

    item = state.get("inputs", {}).get("bindings", {}).get(POLICY_KEY)
    if item is None:
        return None
    root = _root(state)
    return _validate_policy(_read(bound(item, root=root)), root, state=state)


def response_spans(raw: bytes, methods: list[str], *, intervention: str | None) -> list[dict[str, Any]]:
    """按 HTTP 请求顺序划分响应；保留 HEAD、分块及信息响应的原字节，歧义拒绝。

    明确的断流／停滞允许最后一个响应为空；末尾截断只能归给最后一个请求，不能把下一响应吞进前一个。
    字节范围包括原始 Header 和 chunk framing，投影不会重编码响应或生成成功响应。
    """

    class Stream(io.BytesIO):
        def close(self) -> None:
            # HTTPResponse 结束一次读取会关闭 fp；共享原流仍须用于后续响应边界。
            pass

    class Socket:
        def makefile(self, _mode: str) -> Stream:
            return stream

    stream = Stream(raw)
    spans = []
    for number, method in enumerate(methods):
        start = stream.tell()
        last = number == len(methods) - 1
        if start == len(raw) and last and intervention in {"disconnect_without_response", "stall_without_response"}:
            spans.append({"start": start, "end": start, "complete": False, "reason": intervention})
            continue
        try:
            response = http.client.HTTPResponse(Socket(), method=method)
            response.begin()
            while 100 <= response.status < 200:
                if response.status == 101:
                    raise RecordedEvaluationError("录制响应升级了协议，不能投影为 HTTP 请求流")
                response = http.client.HTTPResponse(Socket(), method=method)
                response.begin()
            if response.will_close and response.length is None and not response.chunked and not last:
                raise RecordedEvaluationError("混合连接响应没有可判定的消息边界")
            response.read()
            spans.append({"start": start, "end": stream.tell(), "complete": True})
        except (http.client.IncompleteRead, EOFError):
            if not last:
                raise RecordedEvaluationError("非末尾响应截断，不能确定请求与响应配对") from None
            stream.seek(len(raw))
            spans.append({"start": start, "end": len(raw), "complete": False, "reason": "recorded_final_truncation"})
        except (http.client.HTTPException, OSError, ValueError) as error:
            raise RecordedEvaluationError(f"录制响应边界不能重验：{type(error).__name__}") from None
    if stream.tell() != len(raw):
        raise RecordedEvaluationError("录制响应有未配对的尾部字节")
    return spans


def project_segments(segments: list[dict[str, Any]], ranges: dict[str, list[tuple[int, int]]]) -> list[dict[str, Any]]:
    """按选中范围交集重算两方向的偏移，保留原分段顺序、时间与字节来源。"""

    result, offsets = [], {name: 0 for name in ranges}
    original_offsets = {name: 0 for name in ranges}
    for segment in segments:
        direction = segment["direction"]
        if (direction not in original_offsets or segment.get("offset") != original_offsets[direction]
                or not isinstance(segment.get("length"), int) or isinstance(segment["length"], bool)
                or segment["length"] < 0):
            raise RecordedEvaluationError("录制分段的方向或原偏移不连续")
        start, end = segment["offset"], segment["offset"] + segment["length"]
        original_offsets[direction] = end
        for left, right in ranges.get(direction, []):
            length = max(0, min(end, right) - max(start, left))
            if length:
                result.append({**segment, "offset": offsets[direction], "length": length})
                offsets[direction] += length
    if any(offsets[name] != sum(right - left for left, right in spans) for name, spans in ranges.items()):
        raise RecordedEvaluationError("录制分段不能覆盖投影字节范围")
    return result



def _origin(state: Mapping[str, Any], probe: str) -> dict[str, Any]:
    """重验快照行、原平台与 relay／宿主双绑定；保留 amd64 事实，不称 ARM64 原生。"""

    root = _root(state)
    snapshot_path = bound(state["snapshot"], root=root)
    index = _read(snapshot_path)
    if index.get("purpose") != "regression_fixture" or index.get("target_version") != state["target_version"]:
        raise RecordedEvaluationError("录制快照身份不符")
    rows = [row for row in index["probes"] if row["probe_id"] == probe]
    if len(rows) != 1:
        raise RecordedEvaluationError("录制 probe 必须唯一登记")
    row, snapshot_root = rows[0], snapshot_path.parent / "data"
    attempt = PurePosixPath(row["attempt"])
    if attempt.is_absolute() or ".." in attempt.parts:
        raise RecordedEvaluationError("录制 attempt 越界")
    expected_host = f"{row['source_id']}/runtime-receipts/{probe}-{attempt.name}.json"
    host_relative = row.get("host_receipt", expected_host)
    if host_relative != expected_host:
        raise RecordedEvaluationError("录制 attempt 与宿主收据不同")
    files = {}
    for relative, item in index["files"].items():
        if relative.startswith(row["attempt"] + "/") or relative == host_relative:
            files[relative] = bound({"path": relative, **item}, root=snapshot_root)
    manifest_path = files[row["attempt"] + "/relay-manifest.json"]
    expected_files = {str(path) for relative, path in files.items() if relative.startswith(row["attempt"] + "/")}
    if {str(path) for path in manifest_path.parent.rglob("*") if path.is_file() or path.is_symlink()} != expected_files:
        raise RecordedEvaluationError("录制官方 attempt 读集含未登记文件")
    manifest, host = _read(manifest_path), _read(files[host_relative])
    old, runtime = manifest["runtime"]["host_runtime_receipt"], manifest["runtime"]
    if (host.get("schema_version") != "official-client-runtime-host-receipt/v1"
            or old.get("sha256") != binding(files[host_relative])["sha256"] or old.get("run_nonce") != host.get("run_nonce")
            or str(manifest["client"].get("version")).split(" ")[0] != state["target_version"]
            or manifest["client"].get("sha256") != state["freeze"]["binary_sha256"] or manifest.get("probe_id") != probe
            or host.get("repo_digest_verified") is not True or old.get("repo_digest_verified") is not True
            or host.get("docker_server", {}).get("os") != "linux" or host.get("docker_server", {}).get("arch") != "amd64"
            or any(old.get(key) != host.get(key) for key in ("schema_version", "container", "docker_server", "runtime_image_reference", "runtime_image_id"))
            or runtime.get("runtime_image_reference") != host.get("runtime_image_reference")
            or old.get("capture_source_bundle_sha256") != host.get("capture_source_bundle", {}).get("sha256")
            or not SHA256_RE.fullmatch(str(host.get("capture_source_bundle", {}).get("sha256", "")))
            or runtime.get("capture_tools", {}).get("execution_sources") != host.get("capture_source_bundle")
            or old.get("container_runtime_binding", {}).get("verified") is not True
            or old.get("container_runtime_binding", {}).get("container_id") != host.get("container", {}).get("id")):
        raise RecordedEvaluationError("录制官方 relay、宿主、版本或平台双绑定断裂")
    return {"row": row, "index": {**index, "root": str(snapshot_root)}, "files": files, "manifest_path": manifest_path, "host": host,
            "root": manifest_path.parent, "manifest": manifest}


def connection_domains(state: Mapping[str, Any], origin: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按整连接互斥分域；未知请求与混合域连接在目的目录创建前拒绝。"""

    from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4

    root = Path(origin["manifest_path"]).parent
    relay = _read(root / "relay/relay.json")
    requests = v4._parse_wire_requests(root / "relay")
    by_connection: dict[int, list[Any]] = {}
    for request in requests:
        by_connection.setdefault(request["connection_id"], []).append(request)
    result = []
    seen = set()
    for connection in relay["connections"]:
        identifier = connection["connection_id"]
        if type(identifier) is not int or identifier < 1 or identifier in seen:
            raise RecordedEvaluationError("录制连接号重复或非法")
        seen.add(identifier)
        rows = by_connection.pop(identifier, [])
        domains = set()
        for request in rows:
            endpoint = f"{request['method']} {request['request_target'].split('?', 1)[0]}"
            domain = "client_shared" if endpoint in SHARED_REQUESTS else "gateway" if endpoint in GATEWAY_REQUESTS else None
            if domain is None:
                raise RecordedEvaluationError(f"录制来源含未知端点：{endpoint}")
            domains.add(domain)
        if len(domains) > 1:
            raise RecordedEvaluationError("录制来源含混合域连接，必须在冻结前拒绝")
        if rows and connection.get("valid") is not True:
            raise RecordedEvaluationError("录制请求属于无效连接")
        result.append({"connection_id": identifier, "domain": next(iter(domains)) if domains else "no_request", "request_count": len(rows)})
    if by_connection:
        raise RecordedEvaluationError("录制请求连接没有索引归属")
    return result


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def gateway_material(state: Mapping[str, Any], projection: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """从摘要绑定的原件与冻结标签形成唯一的网关字节投影；未知或未获准请求失败关闭。

    这是夹具输入形成函数，不写阶段、判通过或启动客户端。完整官方附件仍由调用方独立保留。
    """

    from tools.official_client_capture import claude_upgrade_evidence as evidence
    from tools.official_client_capture import claude_upgrade_plan as plan
    from tools.official_client_capture import claude_fw_e_relay as relay_capture
    from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4
    from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

    data, bindings = Path(state["data_root"]), state["inputs"]["bindings"]
    labels_path, scenarios_path = bound(bindings["labels"], root=data), bound(bindings["scenarios"], root=data)
    labels = plan.load_evidence_labels(labels_path, expected_version="2.1.226", scenario_payload=_read(scenarios_path))
    connection_plan = labels["plans"].get(projection["job_id"], {}).get(projection["probe_id"])
    if connection_plan is None or connection_plan["mode"] not in {"none", "unordered"}:
        raise RecordedEvaluationError("候选投影缺少冻结的网关连接规则")
    allowed = set(connection_plan.get("requests", ()))
    forwarded = ({"POST /v1/oauth/token"} if projection["candidate"]["trigger"] == evidence.CANDIDATE_TRIGGER_ACCOUNT_REFRESH
                 else {"POST /v1/messages", "POST /v1/messages/count_tokens"})
    if not allowed <= forwarded:
        raise RecordedEvaluationError("候选标签超出网关转发边界")
    index, row = origin["index"], origin["row"]
    root, prefix = Path(index["root"]), row["attempt"] + "/"
    originals = {relative[len(prefix):]: bound({"path": str(root / relative), **binding}, root=root)
                 for relative, binding in index["files"].items() if relative.startswith(prefix)}
    relay_root = root / row["attempt"] / "relay"
    if {"relay/" + path.name for path in relay_root.iterdir()} != {name for name in originals if name.startswith("relay/")}:
        raise RecordedEvaluationError("候选来源 relay 读集含未登记文件")
    probe = v5.get_probe(projection["probe_id"])
    requests = v4._parse_wire_requests(relay_root)
    by_connection: dict[int, list[dict[str, Any]]] = {}
    for request in requests:
        by_connection.setdefault(request["connection_id"], []).append(request)
    relay = copy.deepcopy(_read(originals["relay/relay.json"]))
    files, connections, mapping = {}, [], []
    for item in relay["connections"]:
        identifier = item["connection_id"]
        rows = by_connection.get(identifier, [])
        selected = []
        for number, request in enumerate(rows):
            identity = f"{request['method']} {request['request_target'].split('?', 1)[0]}"
            if identity in SHARED_REQUESTS:
                continue
            if identity not in forwarded or identity not in allowed:
                raise RecordedEvaluationError(f"候选来源含未知或未获标签允许的出站请求：{identity}")
            selected.append(number)
        if not selected:
            mapping.append({"connection_id": identifier, "kept_request_indices": [], "reason": "local_or_no_request"})
            continue
        if item.get("valid") is not True:
            raise RecordedEvaluationError("候选选中请求属于无效的原连接")
        request_name, response_name = (f"relay/conn{identifier:03d}.{direction}.bin" for direction in ("client_to_upstream", "upstream_to_client"))
        raw_request = originals[request_name].read_bytes()
        request_spans = [(request["stream_offset"], rows[number + 1]["stream_offset"] if number + 1 < len(rows) else len(raw_request))
                         for number, request in enumerate(rows)]
        if any(_sha(raw_request[left:right]) != request["raw_sha256"] for request, (left, right) in zip(rows, request_spans)):
            raise RecordedEvaluationError("原请求范围与真实解析摘要不同")
        raw_response = originals[response_name].read_bytes() if response_name in originals else b""
        one_sided = item.get("termination_reason") == relay_capture.ONE_SIDED_SHUTDOWN_TERMINATION_REASON
        if one_sided and response_name in originals:
            raise RecordedEvaluationError("单向停机原件意外含响应文件，不能丢弃其字节")
        response_ranges = ([{"start": 0, "end": 0, "complete": False, "reason": "recorded_one_sided_shutdown"}] if one_sided and not raw_response and len(rows) == 1
                          else response_spans(raw_response, [request["method"] for request in rows], intervention=item.get("intervention")))
        if len(selected) != len(rows) and item.get("intervention") is not None:
            raise RecordedEvaluationError("混合请求连接的合成干预无法按请求唯一投影")
        ranges = {"client_to_upstream": [request_spans[number] for number in selected],
                  "upstream_to_client": [(response_ranges[number]["start"], response_ranges[number]["end"]) for number in selected]}
        projected = {"client_to_upstream": b"".join(raw_request[left:right] for left, right in ranges["client_to_upstream"]),
                     "upstream_to_client": b"".join(raw_response[left:right] for left, right in ranges["upstream_to_client"])}
        current = copy.deepcopy(item)
        current["connect_request_line"] = f"CONNECT {probe.target_host}:443 HTTP/1.1"
        directions = list(item.get("bytes", {}))
        current["bytes"] = {direction: len(projected[direction]) for direction in directions}
        current["sha256"] = {direction: _sha(projected[direction]) for direction in directions}
        current["segments"] = project_segments(item.get("segments", []), ranges)
        for direction in directions:
            files[f"relay/conn{identifier:03d}.{direction}.bin"] = projected[direction]
        connections.append(current)
        mapping.append({"connection_id": identifier, "kept_request_indices": selected,
                        "request_source": binding(originals[request_name]),
                        "response_source": binding(originals[response_name]) if response_name in originals else None,
                        "requests": [{"source_range": list(request_spans[number]), "response_range": response_ranges[number],
                                      "request_sha256": rows[number]["raw_sha256"],
                                      "response_sha256": _sha(raw_response[response_ranges[number]["start"]:response_ranges[number]["end"]])} for number in selected]})
    relay.update(mode="connect", connections=connections)
    files["relay/relay.json"] = (json.dumps(relay, ensure_ascii=False, sort_keys=True) + "\n").encode()
    kept_ids = {item["connection_id"] for item in connections}
    if "relay/intervention.jsonl" in originals:
        events = [json.loads(line) for line in originals["relay/intervention.jsonl"].read_text().splitlines() if line.strip()]
        if any("connection_id" not in event for event in events):
            raise RecordedEvaluationError("合成干预原件缺少连接归属")
        files["relay/intervention.jsonl"] = b"".join((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode()
                                                    for event in events if event["connection_id"] in kept_ids)
    for name, path in originals.items():
        if name == "tls-clienthello.pcap" or name.startswith("results/"):
            files[name] = path.read_bytes()
    pcap = originals.get("tls-clienthello.pcap")
    if probe.require_pcap and pcap is None:
        raise RecordedEvaluationError("要求 P 通道的原件缺少已绑定 pcap")
    views = [{"connection_id": item["connection_id"], "requests": [f"{rows['method']} {rows['request_target'].split('?', 1)[0]}"
              for rows in by_connection[item["connection_id"]] if f"{rows['method']} {rows['request_target'].split('?', 1)[0]}" not in SHARED_REQUESTS],
              "intervention": item.get("intervention")} for item in connections]
    plan.match_relay_connections(connection_plan, views)
    receipt = {"schema_version": "claude-recorded-gateway-byte-projection/v1", "fixture_only": True, "current_client_executed": False,
               "formal_version_semantics_certified": False, "purpose": "recorded_gateway_byte_projection",
               "labels": bindings["labels"], "scenarios": bindings["scenarios"], "origin_manifest": binding(origin["manifest_path"]),
               "connections": mapping, "pcap_source": binding(pcap) if pcap else None,
               "pcap_scope": "完整官方 TLS 原字节的夹具观察；不是本轮网关 TLS 取证，也不按候选连接数伪造裁剪",
               "connect_scope": "明确的离线 connect 控制封套；不声明真实隧道建立",
               "projected_files": [{"path": name, "bytes": len(raw), "sha256": _sha(raw)} for name, raw in sorted(files.items())]}
    return {"files": files, "receipt": receipt}



def build_probe_contract(state_path: Path, projection_path: Path, destination: Path) -> dict[str, Any]:
    """原件与纯网关投影落盘后形成固定 proof；不写客户端或阶段判定。"""

    state, projection = _read(state_path), _read(projection_path)
    policy = optional_policy(state)
    if policy is None:
        raise RecordedEvaluationError("分域 proof 缺少显式 policy")
    root = _root(state)
    origin = _origin(state, projection["probe_id"])
    copied = []
    prefix = origin["row"]["attempt"] + "/"
    for relative, original in sorted(origin["files"].items()):
        if relative.startswith(prefix):
            target = destination / ORIGINAL_DIRECTORY / relative[len(prefix):]
            if target.read_bytes() != original.read_bytes():
                raise RecordedEvaluationError("分域 proof 的官方原件副本漂移")
            copied.append({"source_relative": relative, "copied": binding(target)})
    return {"schema_version": PROBE_SCHEMA, **QUALIFICATIONS, "policy": state["inputs"]["bindings"][POLICY_KEY],
            "source_state": binding(state_path), "projection": binding(projection_path),
            "logical_probe": {"directory": destination.relative_to(root).as_posix(), "probe_id": projection["probe_id"], "job_id": projection["job_id"]},
            "connections": connection_domains(state, origin), "copied_originals": copied,
            "gateway_byte_projection": binding(destination / "gateway-byte-projection.json")}


def verify_probe_contract(item: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """每个读侧重做来源、整连接分域与实际字节校验；不接受仅自洽的标签或重写摘要。"""

    proof_path = bound(item, root=root)
    proof = dict(_exact(_read(proof_path), {"schema_version", *QUALIFICATIONS, "policy", "source_state", "projection", "logical_probe",
                                          "connections", "copied_originals", "gateway_byte_projection"}, "录制 probe proof"))
    if proof["schema_version"] != PROBE_SCHEMA or any(proof[key] is not value for key, value in QUALIFICATIONS.items()):
        raise RecordedEvaluationError("分域 probe proof 夹具资格非法")
    state = _read(bound(proof["source_state"], root=root))
    if _root(state) != root or state["inputs"]["bindings"].get(POLICY_KEY) != proof["policy"]:
        raise RecordedEvaluationError("分域 probe proof 不能自行启用 policy")
    policy = optional_policy(state)
    projection_path = bound(proof["projection"], root=root)
    projection = _read(projection_path)
    logical = _exact(proof["logical_probe"], {"directory", "probe_id", "job_id"}, "逻辑 probe")
    destination = Path(str(projection.get("output_root", "")))
    if (not destination.is_absolute() or not destination.is_relative_to(root / "runs") or destination.resolve() != destination
            or proof_path != destination / PROBE_FILENAME or logical != {"directory": destination.relative_to(root).as_posix(),
                "probe_id": projection.get("probe_id"), "job_id": projection.get("job_id")}
            or projection.get("side") != "candidate" or projection.get("fixture_only") is not True):
        raise RecordedEvaluationError("分域 proof 与本次候选 probe 坐标不同")
    operation = OPERATION_RE.fullmatch(str(projection.get("operation", "")))
    jobs = policy["jobs"]["candidate"]
    if (operation is None or operation[1] != logical["job_id"] or logical["job_id"] not in jobs
            or int(operation[2]) > len(jobs[logical["job_id"]]) or jobs[logical["job_id"]][int(operation[2]) - 1] != logical["probe_id"]):
        raise RecordedEvaluationError("分域 proof 不是冻结的候选 Job 步骤")
    origin = _origin(state, logical["probe_id"])
    current = projection["runtime"]
    runtime = _read(bound(current["receipt"], root=root))
    reservation_path = bound(current["reservation"], root=root)
    reservation = _read(reservation_path)
    from tools.official_client_capture import claude_upgrade_capture as capture

    nonce = capture._step_run_nonce(reservation["run_nonce"], logical["job_id"], int(operation[3]), int(operation[2]))
    if (reservation.get("schema_version") != capture.CAPTURE_RESERVATION_SCHEMA or reservation.get("mode") != capture.MODE_FORMAL
            or reservation.get("phase") != "candidate" or reservation.get("candidate_id") != projection.get("candidate_id")
            or reservation_path.parent.name != current["attempt_id"] or reservation.get("attempt_id") != current["attempt_id"]
            or logical["job_id"] not in {job["id"] for job in reservation.get("planned_jobs", [])}
            or current["run_nonce"] != nonce or runtime.get("run_nonce") != nonce
            or runtime.get("schema_version") != capture.HOST_RUNTIME_RECEIPT_SCHEMA or runtime.get("repo_digest_verified") is not True
            or runtime.get("docker_server", {}).get("os") != "linux" or runtime.get("docker_server", {}).get("arch") != "arm64"
            or runtime.get("container", {}).get("name") != current["container_name"] or runtime.get("container", {}).get("id") != current["container_id"]
            or runtime.get("runtime_image_reference") != current["runtime_image"]
            or not capture.IMAGE_ID_RE.fullmatch(str(runtime.get("runtime_image_id", "")))
            or not SHA256_RE.fullmatch(str(runtime.get("capture_source_bundle", {}).get("sha256", "")))
            or nonce == origin["host"]["run_nonce"] or projection.get("origin", {}).get("platform") != "linux/amd64"
            or projection.get("origin", {}).get("binary_sha256") != state["freeze"]["binary_sha256"]
            or projection.get("origin", {}).get("source_id") != origin["row"]["source_id"]
            or projection.get("origin", {}).get("attempt") != origin["row"]["attempt"]
            or projection.get("origin", {}).get("run_nonce") != origin["host"]["run_nonce"]
            or _read(bound(projection["origin"]["receipt"], root=root)) != origin["host"]):
        raise RecordedEvaluationError("分域 proof 当前 runtime 与原官方来源身份断裂")
    if proof != build_probe_contract(bound(proof["source_state"], root=root), projection_path, destination):
        raise RecordedEvaluationError("分域 proof 与原件重算不一致")
    copied_root = destination / ORIGINAL_DIRECTORY
    expected_names = {row["copied"]["path"] for row in proof["copied_originals"]}
    if {str(path) for path in copied_root.rglob("*") if path.is_file() or path.is_symlink()} != expected_names:
        raise RecordedEvaluationError("分域官方原件副本闭集漂移")
    for row in proof["copied_originals"]:
        bound(row["copied"], root=root)
    receipt = _read(bound(proof["gateway_byte_projection"], root=root))
    material = gateway_material(state, projection, origin)
    if receipt != material["receipt"]:
        raise RecordedEvaluationError("分域网关投影范围、响应配对或来源绑定漂移")
    for relative, raw in material["files"].items():
        if (destination / relative).read_bytes() != raw:
            raise RecordedEvaluationError("分域网关投影原字节或固定封套漂移")
    if (receipt.get("schema_version") != "claude-recorded-gateway-byte-projection/v1"
            or any(receipt.get(key) is not value for key, value in QUALIFICATIONS.items())
            or receipt.get("origin_manifest") != binding(origin["manifest_path"])
            or receipt.get("labels") != state["inputs"]["bindings"]["labels"] or receipt.get("scenarios") != state["inputs"]["bindings"]["scenarios"]):
        raise RecordedEvaluationError("分域网关投影收据来源不符")
    original_index = _read(copied_root / "relay/relay.json")
    gateway_index = _read(destination / "relay/relay.json")
    domains = {row["connection_id"]: row["domain"] for row in proof["connections"]}
    expected_index = copy.deepcopy(original_index)
    expected_index.update(mode="connect", connections=[row for row in expected_index["connections"] if domains[row["connection_id"]] == "gateway"])
    from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

    expected_index = json.loads(material["files"]["relay/relay.json"])
    for row in expected_index["connections"]:
        row["connect_request_line"] = f"CONNECT {v5.get_probe(logical['probe_id']).target_host}:443 HTTP/1.1"
    if gateway_index != expected_index:
        raise RecordedEvaluationError("整连接网关投影的索引事实与官方原件不同")
    for row in expected_index["connections"]:
        for direction in row["bytes"]:
            relative = f"relay/conn{row['connection_id']:03d}.{direction}.bin"
            if (destination / relative).read_bytes() != (copied_root / relative).read_bytes():
                raise RecordedEvaluationError("整连接网关投影改变了请求或响应原字节")
    expected_relay_files = {"relay/relay.json", *(f"relay/conn{row['connection_id']:03d}.{direction}.bin"
                            for row in expected_index["connections"] for direction in row["bytes"])}
    if (copied_root / "relay/intervention.jsonl").exists():
        expected_relay_files.add("relay/intervention.jsonl")
        events = [json.loads(line) for line in (copied_root / "relay/intervention.jsonl").read_text().splitlines() if line.strip()]
        expected_events = [event for event in events if domains[event["connection_id"]] == "gateway"]
        actual_events = [json.loads(line) for line in (destination / "relay/intervention.jsonl").read_text().splitlines() if line.strip()]
        if actual_events != expected_events:
            raise RecordedEvaluationError("分域干预记录不属于原网关连接")
    if {"relay/" + path.name for path in (destination / "relay").iterdir()} != expected_relay_files:
        raise RecordedEvaluationError("分域网关 relay 文件闭集漂移")
    for row in receipt["projected_files"]:
        bound(row, root=destination)
    if receipt.get("pcap_source") != (binding(origin["root"] / "tls-clienthello.pcap") if (origin["root"] / "tls-clienthello.pcap").exists() else None):
        raise RecordedEvaluationError("分域 pcap 必须保留完整官方来源绑定")
    if (copied_root / "tls-clienthello.pcap").exists() and (destination / "tls-clienthello.pcap").read_bytes() != (copied_root / "tls-clienthello.pcap").read_bytes():
        raise RecordedEvaluationError("分域 pcap 不是完整官方原字节")
    wrapper = _read(destination / "relay-manifest.json")
    if (wrapper.get("fixture_projection", {}).get(PROBE_KEY) != dict(item)
            or wrapper.get("fixture_projection", {}).get("copied_origin_manifest") != binding(copied_root / "relay-manifest.json")
            or wrapper.get("fixture_projection", {}).get("gateway_byte_projection") != proof["gateway_byte_projection"]):
        raise RecordedEvaluationError("本次候选封套未绑定同一分域 proof")
    host_binding = {"schema_version": runtime["schema_version"], "path": str(bound(current["receipt"], root=root)),
                    "sha256": current["receipt"]["sha256"], "run_nonce": runtime["run_nonce"],
                    **{name: runtime[name] for name in ("container", "docker_server", "runtime_image_reference", "runtime_image_id", "repo_digest_verified")},
                    "capture_source_bundle_sha256": runtime["capture_source_bundle"]["sha256"],
                    "container_runtime_binding": {"verified": True, "container_id": runtime["container"]["id"]}}
    if (wrapper.get("client") != origin["manifest"]["client"] or wrapper.get("side") != "candidate"
            or wrapper.get("probe_id") != logical["probe_id"] or wrapper.get("run_id") != projection["candidate"]["run_id"]
            or wrapper.get("runtime", {}).get("host_runtime_receipt") != host_binding
            or wrapper.get("runtime", {}).get("capture_tools", {}).get("execution_sources") != runtime["capture_source_bundle"]
            or wrapper.get("fixture_only") is not True or wrapper.get("formal_version_semantics_certified") is not False
            or wrapper.get("fixture_projection", {}).get("current_client_executed") is not False
            or wrapper.get("fixture_projection", {}).get("original_manifest") != binding(origin["manifest_path"])):
        raise RecordedEvaluationError("录制逻辑 probe 的当前封套与双侧身份不一致")
    return {"proof": proof, "policy": policy, "state": state, "origin": origin, "directory": logical["directory"],
            "destination": destination, "original_index": original_index, "gateway_index": gateway_index}


def _shared_artifacts(view: Mapping[str, Any], anchor: Mapping[str, Any], root: Path,
                      inventory: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """共有请求、响应与原索引的固定伴随闭集；Job 归本次候选，origin 资格归原官方。"""

    shared_ids = {row["connection_id"] for row in view["proof"]["connections"] if row["domain"] == "client_shared"}
    if not shared_ids:
        return []
    result = []
    directory = Path(view["destination"]) / ORIGINAL_DIRECTORY
    common = {key: copy.deepcopy(anchor[key]) for key in ("probe_id", "scenario_ids", "labels", "job_id")}
    common["labels"].update(source_domain="client_shared", origin_platform="linux/amd64", origin_version=view["policy"]["target_version"])

    def add(path: Path, kind: str, parser: str, **extra: Any) -> None:
        relative = path.relative_to(root).as_posix()
        observed = binding(path)
        if inventory is not None and any(inventory.get(relative, {}).get(key) != observed[key] for key in ("sha256", "bytes")):
            raise RecordedEvaluationError("共有原件未进入当前 seal 的深度扫描绑定")
        result.append({**copy.deepcopy(common), "path": relative, "sha256": observed["sha256"], "bytes": observed["bytes"],
                       "kind": kind, "parser": parser, **extra})

    for ordinal, connection in enumerate(view["original_index"]["connections"], 1):
        identifier = connection["connection_id"]
        if identifier not in shared_ids:
            continue
        coordinate = {"ordinal": ordinal, "connection_id": identifier}
        add(directory / f"relay/conn{identifier:03d}.client_to_upstream.bin", "relay_binary", "opaque_bound_source",
            connection=coordinate, derive={"parser": "h1_request_stream", "kind": "claude_http_request"})
        response = directory / f"relay/conn{identifier:03d}.upstream_to_client.bin"
        if response.exists():
            add(response, "relay_response", "h1_response_stream", connection=coordinate)
    add(directory / "relay/relay.json", "relay_index", "claude_relay_index")
    return result


def bind_recorded_manifest(manifest: Mapping[str, Any], root: Path, inventory: Mapping[str, Any]) -> dict[str, Any]:
    """seal 在 v1 绑定上补严格录制视图；没有显式 proof 的原流程逐字段不变。"""

    anchors = [item for item in manifest["artifacts"] if item["kind"] == "relay_manifest"]
    selected = []
    for anchor in anchors:
        wrapper = _read(bound({key: anchor[key] for key in ("path", "sha256", "bytes")}, root=root))
        proof = wrapper.get("fixture_projection", {}).get(PROBE_KEY)
        selected.append((anchor, proof))
    if not any(proof is not None for _anchor, proof in selected):
        return dict(manifest)
    if not selected or any(proof is None for _anchor, proof in selected):
        raise RecordedEvaluationError("同次录制评估不能混用旧 v1 与新分域 proof")
    payload = copy.deepcopy(dict(manifest))
    proofs, policy_binding = [], None
    for anchor, item in selected:
        view = verify_probe_contract(item, root)
        if anchor.get("job_id") != view["proof"]["logical_probe"]["job_id"] or anchor["probe_id"] != view["proof"]["logical_probe"]["probe_id"]:
            raise RecordedEvaluationError("seal 锚点与分域 Job／probe 不同")
        if policy_binding is not None and view["proof"]["policy"] != policy_binding:
            raise RecordedEvaluationError("同次录制评估的 policy 不唯一")
        policy_binding = view["proof"]["policy"]
        proofs.append(dict(item))
        payload["artifacts"].extend(_shared_artifacts(view, anchor, root, inventory))
    payload.update(schema_version=EVALUATION_SCHEMA, recorded_evaluation={"policy": policy_binding, "probes": proofs})
    verify_recorded_manifest(payload, root)
    return payload


def validate_recorded_shape(manifest: Mapping[str, Any]) -> None:
    """schema 校验只管闭集；实际来源重验由 verify_recorded_manifest 在各读侧执行。"""

    value = _exact(manifest.get("recorded_evaluation"), {"policy", "probes"}, "录制评估视图")
    _exact(value["policy"], {"path", "sha256", "bytes"}, "录制 policy 绑定")
    if not isinstance(value["probes"], list) or not value["probes"]:
        raise RecordedEvaluationError("录制评估视图缺少 probe proof")
    for item in value["probes"]:
        _exact(item, {"path", "sha256", "bytes"}, "录制 probe proof 绑定")


def verify_recorded_manifest(manifest: Mapping[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    """读侧统一验真并给出唯一逻辑目录；旧 v1 不启用分域，也不能携带新 proof 伪装为旧语义。"""

    if manifest.get("schema_version") != EVALUATION_SCHEMA:
        return {}
    validate_recorded_shape(manifest)
    result, seen_probes = {}, set()
    for item in manifest["recorded_evaluation"]["probes"]:
        view = verify_probe_contract(item, root)
        logical = view["proof"]["logical_probe"]
        if (view["proof"]["policy"] != manifest["recorded_evaluation"]["policy"]
                or logical["directory"] in result or logical["probe_id"] in seen_probes):
            raise RecordedEvaluationError("逻辑 probe 重复或 policy 不一致")
        result[logical["directory"]] = view
        seen_probes.add(logical["probe_id"])
    by_directory = {directory: [] for directory in result}
    for artifact in manifest["artifacts"]:
        matches = [directory for directory in result if artifact["path"].startswith(directory + "/")]
        if len(matches) != 1:
            raise RecordedEvaluationError("录制 artifact 没有唯一逻辑 probe 归属")
        directory, = matches
        logical = result[directory]["proof"]["logical_probe"]
        if artifact["probe_id"] != logical["probe_id"] or artifact.get("job_id") != logical["job_id"]:
            raise RecordedEvaluationError("共有 artifact 必须归当前候选 Job／probe")
        bound({key: artifact[key] for key in ("path", "sha256", "bytes")}, root=root)
        by_directory[directory].append(artifact)
    for directory, view in result.items():
        members = by_directory[directory]
        anchors = [artifact for artifact in members if artifact["kind"] == "relay_manifest"]
        if len(anchors) != 1 or anchors[0]["path"] != directory + "/relay-manifest.json":
            raise RecordedEvaluationError("逻辑 probe 只能有一个当前 runtime 封套")
        expected = _shared_artifacts(view, anchors[0], root)
        shared = [artifact for artifact in members if artifact["path"].startswith(directory + "/" + ORIGINAL_DIRECTORY + "/")]
        if shared != expected:
            raise RecordedEvaluationError("共有 artifact 闭集、原字节或固定来源标签漂移")
        gateway = [artifact for artifact in members if artifact not in shared]
        request_ids = [artifact["connection"]["connection_id"] for artifact in gateway if artifact["kind"] == "relay_binary"]
        expected_ids = [row["connection_id"] for row in view["proof"]["connections"] if row["domain"] == "gateway"]
        if sorted(request_ids) != sorted(expected_ids):
            raise RecordedEvaluationError("gateway 请求连接分母与整连接分域合同不同")
        receipt = _read(bound(view["proof"]["gateway_byte_projection"], root=root))
        allowed = {directory + "/relay-manifest.json", *(directory + "/" + item["path"] for item in receipt["projected_files"])}
        if any(artifact["path"] not in allowed for artifact in gateway):
            raise RecordedEvaluationError("gateway artifact 不是原投影或当前封套")
    return result


def verify_profile(manifest: Mapping[str, Any], profile: Mapping[str, Any], root: Path) -> None:
    """当前逐规则消费者必须用同一真实冻结画像；验收摘要相同也不能替换画像字节。"""

    if manifest.get("schema_version") == EVALUATION_SCHEMA:
        policy = _validate_policy(_read(bound(manifest["recorded_evaluation"]["policy"], root=root)), root)
        if dict(profile) != _read(bound(policy["assertion_profile"], root=root)):
            raise RecordedEvaluationError("录制评估当前画像与 policy 冻结画像不同")
