"""逐请求 provenance v2：以统一计量单位从不可变证据核算模型请求。

背景：旧计数规则 ``codex_model_turns_and_responses_requests/v1`` 对 capture 与
compact 类按“完成 turn”计数，对 relay 类按“Responses 请求”计数。0.154 的
现场证明二者不等价：一个 turn 内 Codex 可以发出多个 ``POST /responses``，
一次 WS turn 可以包含多条 ``response.create``。混用口径得到的历史数字不能
作为项目账务基准。

本模块只承认一种计量单位：

* 一次 HTTP ``POST`` 到模型端点（:data:`MODEL_ENDPOINTS`），或
* 一条 client 方向的 WS 业务消息，``type == "response.create"``。

来源与原生坐标（身份键只由 ``producer_run_id + source_kind + 原生坐标`` 生成，
真实路径、文件摘要、记录偏移只作完整性字段）：

* capture 类（``manifest.json`` schema ``official-client-capture/v1``）：每个
  case 有 ``direct`` 与 ``mitm`` 两次独立执行。mitm 分支读
  ``mitm/<subject>/<scenario>/codex-http.jsonl`` 与 ``codex-ws.jsonl`` 精确计数；
  direct 分支只有 TLS 加密的 pcap 与 ``turn<N>-events.jsonl``，无法解析请求，
  只能按估计政策处理；``turn.completed`` 数只作下界核对。
* compact 类（``result/*/summary.json`` schema ``codex-compact-capture/v1``）：
  同样分 direct 与 mitm 两次执行，mitm 分支读 ``mitm/<subject>/codex-http.jsonl``。
* relay 类（``relay/connNNN.client_to_upstream.bin``）：逐连接解析 HTTP 请求与
  WS 帧，坐标为连接编号加消息序号。

估计政策（由项目总账冻结，本模块只执行不决定）：

* ``none``：无法解析的分支进入未决；
* ``upper_bound_from_sibling``：direct 分支按同一证据根内同 subject／scenario 的
  mitm 分支精确数计为上界估计并标记；
* ``upper_bound_from_sibling_or_turn_ratio``：没有同根 sibling 时，再用同一
  Campaign 内同 subject 的全部 mitm 分支观测到的最大“请求数／完成 turn 数”倍率
  乘以本分支完成 turn 数作上界。只跑 direct 的 ws-handshake-repeat 这类证据根
  只能靠这一级；本分支 turn 数为零或同 subject 没有 mitm 观测时仍未决。

每个执行分支只允许一个权威计数来源，重复来源失败关闭。v1 审计收据与其
严格字段集合原样保留，本模块只产生 v2 schema。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import model_condition_receipts

SCHEMA_VERSION = "live-request-provenance/v2"
PROJECT_SCHEMA_VERSION = "project-live-request-audit/v2"
COUNTING_RULE = "codex_model_requests/v2"
COUNTING_UNIT = "http_post_model_endpoint_or_client_ws_response_create"
MODEL_ENDPOINTS = frozenset(
    {
        "/backend-api/codex/responses",
        "/backend-api/codex/responses/compact",
    }
)
ESTIMATION_POLICIES = (
    "none",
    "upper_bound_from_sibling",
    "upper_bound_from_sibling_or_turn_ratio",
)
CAPTURE_MANIFEST_SCHEMA = "official-client-capture/v1"
COMPACT_SUMMARY_SCHEMA = "codex-compact-capture/v1"
RELAY_MANIFEST_SCHEMA = "byte-relay/v1"
MAX_JSONL_BYTES = 256 * 1024 * 1024
MAX_RELAY_BYTES = 512 * 1024 * 1024
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CONN_FILE_RE = re.compile(r"^(conn\d+)\.client_to_upstream\.bin$")
TURN_EVENTS_RE = re.compile(r"^turn(\d+)-events\.jsonl$")


class ProvenanceError(ValueError):
    """证据结构、来源唯一性或计数自洽性被破坏。"""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def identity_key(
    producer_run_id: str, source_kind: str, native_coordinate: Mapping[str, Any]
) -> str:
    """身份键只由三项生成；同一请求被复制到别处仍是同一个键。"""

    return _sha256(
        _canonical(
            {
                "producer_run_id": producer_run_id,
                "source_kind": source_kind,
                "native_coordinate": dict(native_coordinate),
            }
        )
    )


def _read_jsonl(path: Path, label: str) -> list[tuple[int, dict[str, Any]]]:
    raw = closeout._read_stable_file(path, label, maximum=MAX_JSONL_BYTES)
    rows: list[tuple[int, dict[str, Any]]] = []
    for index, line in enumerate(raw.split(b"\n")):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProvenanceError(f"{label} 第 {index} 行不是 JSON") from error
        if not isinstance(record, dict):
            raise ProvenanceError(f"{label} 第 {index} 行不是对象")
        rows.append((index, record))
    return rows


def _path_without_query(target: Any) -> str:
    if not isinstance(target, str):
        return ""
    return target.split("?", 1)[0]


def _request(
    *,
    producer_run_id: str,
    source_kind: str,
    native_coordinate: Mapping[str, Any],
    source_file: Path,
    source_sha256: str,
    record_offset: int,
    model: str | None,
) -> dict[str, Any]:
    return {
        "identity_key": identity_key(producer_run_id, source_kind, native_coordinate),
        "producer_run_id": producer_run_id,
        "source_kind": source_kind,
        "native_coordinate": dict(native_coordinate),
        "source_file": str(source_file),
        "source_sha256": source_sha256,
        "record_offset": record_offset,
        "model": model,
    }


# ---------------------------------------------------------------------------
# mitm 侧 JSONL（capture 与 compact 共用同一 addon 输出格式）
# ---------------------------------------------------------------------------


def _mitm_http_requests(
    path: Path,
    *,
    producer_run_id: str,
    source_kind: str,
    coordinate_prefix: Mapping[str, Any],
) -> list[dict[str, Any]]:
    resolved = closeout._trusted_file(path, "mitm HTTP 记录", maximum=MAX_JSONL_BYTES)
    digest = closeout._sha256_file(resolved)
    requests: list[dict[str, Any]] = []
    for index, record in _read_jsonl(resolved, "mitm HTTP 记录"):
        request = record.get("request")
        if not isinstance(request, Mapping):
            continue
        if request.get("method") != "POST":
            continue
        if _path_without_query(request.get("path")) not in MODEL_ENDPOINTS:
            continue
        body = request.get("body")
        model = None
        if isinstance(body, Mapping):
            candidate = body.get("model")
            model = candidate if isinstance(candidate, str) else None
        coordinate = {
            **coordinate_prefix,
            "file": resolved.name,
            "record_index": index,
            "transport": "http",
            "flow_id": record.get("_flow_id"),
        }
        requests.append(
            _request(
                producer_run_id=producer_run_id,
                source_kind=source_kind,
                native_coordinate=coordinate,
                source_file=resolved,
                source_sha256=digest,
                record_offset=index,
                model=model,
            )
        )
    return requests


def _mitm_ws_requests(
    path: Path,
    *,
    producer_run_id: str,
    source_kind: str,
    coordinate_prefix: Mapping[str, Any],
) -> list[dict[str, Any]]:
    resolved = closeout._trusted_file(path, "mitm WS 记录", maximum=MAX_JSONL_BYTES)
    digest = closeout._sha256_file(resolved)
    requests: list[dict[str, Any]] = []
    for index, record in _read_jsonl(resolved, "mitm WS 记录"):
        if record.get("from_client") is not True:
            continue
        if _path_without_query(record.get("path")) not in MODEL_ENDPOINTS:
            continue
        payload = record.get("json")
        if not isinstance(payload, Mapping):
            text = record.get("text")
            try:
                payload = json.loads(text) if isinstance(text, str) else None
            except json.JSONDecodeError:
                payload = None
        if not isinstance(payload, Mapping) or payload.get("type") != "response.create":
            continue
        model = payload.get("model")
        coordinate = {
            **coordinate_prefix,
            "file": resolved.name,
            "record_index": index,
            "transport": "ws",
        }
        requests.append(
            _request(
                producer_run_id=producer_run_id,
                source_kind=source_kind,
                native_coordinate=coordinate,
                source_file=resolved,
                source_sha256=digest,
                record_offset=index,
                model=model if isinstance(model, str) else None,
            )
        )
    return requests


# ---------------------------------------------------------------------------
# capture 类
# ---------------------------------------------------------------------------


def _capture_cases(root: Path) -> list[dict[str, Any]]:
    manifest_path = root / "manifest.json"
    payload, _raw = closeout._load_json(manifest_path, "官方 capture manifest")
    if payload.get("schema_version") != CAPTURE_MANIFEST_SCHEMA:
        raise ProvenanceError("官方 capture manifest schema 非预期")
    cases = payload.get("case_results")
    if not isinstance(cases, list):
        raise ProvenanceError("官方 capture manifest 缺少 case_results")
    normalized: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ProvenanceError(f"capture case_results[{index}] 不是对象")
        evidence = case.get("evidence")
        subject = case.get("subject")
        scenario = case.get("scenario")
        result = case.get("scenario_result")
        turns = result.get("turn_count") if isinstance(result, Mapping) else None
        if (
            evidence not in {"direct", "mitm"}
            or not isinstance(subject, str)
            or not SAFE_ID_RE.fullmatch(subject)
            or not isinstance(scenario, str)
            or not SAFE_ID_RE.fullmatch(scenario)
            or not isinstance(turns, int)
            or isinstance(turns, bool)
            or turns < 0
        ):
            raise ProvenanceError(f"capture case_results[{index}] 坐标或 turn_count 非法")
        normalized.append(
            {
                "evidence": evidence,
                "subject": subject,
                "scenario": scenario,
                "turn_count": turns,
            }
        )
    return normalized


def _capture_turn_completed(root: Path, evidence: str, subject: str, scenario: str) -> int:
    directory = root / "results" / evidence / subject / scenario
    if not directory.is_dir() or directory.is_symlink():
        return 0
    count = 0
    for path in sorted(directory.iterdir()):
        if not TURN_EVENTS_RE.fullmatch(path.name):
            continue
        resolved = closeout._trusted_file(
            path, "capture turn 事件", maximum=MAX_JSONL_BYTES, allow_empty=True
        )
        for _index, record in _read_jsonl(resolved, "capture turn 事件"):
            if record.get("type") == "turn.completed":
                count += 1
    return count


def _capture_branches(root: Path) -> list[dict[str, Any]]:
    """capture 类每个 case 就是一个执行分支；direct 分支留给 Campaign 级估计。"""

    run_id = root.name
    branches: list[dict[str, Any]] = []
    for case in _capture_cases(root):
        branch: dict[str, Any] = {
            "branch": f"{case['evidence']}/{case['subject']}/{case['scenario']}",
            "kind": "capture",
            "evidence": case["evidence"],
            "subject": case["subject"],
            "scenario": case["scenario"],
            "turn_completed": _capture_turn_completed(
                root, case["evidence"], case["subject"], case["scenario"]
            ),
            "declared_turn_count": case["turn_count"],
            "requests": [],
        }
        if case["evidence"] == "direct":
            branch["status"] = "pending_estimate"
            branches.append(branch)
            continue
        directory = root / "mitm" / case["subject"] / case["scenario"]
        prefix = {
            "evidence": "mitm",
            "subject": case["subject"],
            "scenario": case["scenario"],
        }
        http_path = directory / "codex-http.jsonl"
        ws_path = directory / "codex-ws.jsonl"
        if not http_path.is_file() and not ws_path.is_file():
            branch["status"] = "unresolved"
            branch["reason"] = "mitm 分支没有 codex-http.jsonl 或 codex-ws.jsonl"
            branches.append(branch)
            continue
        requests: list[dict[str, Any]] = []
        if http_path.is_file():
            requests.extend(
                _mitm_http_requests(
                    http_path,
                    producer_run_id=run_id,
                    source_kind="capture_mitm",
                    coordinate_prefix=prefix,
                )
            )
        if ws_path.is_file():
            requests.extend(
                _mitm_ws_requests(
                    ws_path,
                    producer_run_id=run_id,
                    source_kind="capture_mitm",
                    coordinate_prefix=prefix,
                )
            )
        branch["status"] = "resolved"
        branch["authority_source"] = "capture_mitm"
        branch["requests"] = requests
        branches.append(branch)
    return branches


# ---------------------------------------------------------------------------
# compact 类
# ---------------------------------------------------------------------------


def _compact_summary(root: Path, evidence: str) -> dict[str, Any] | None:
    path = root / "result" / evidence / "summary.json"
    if not path.is_file() or path.is_symlink():
        return None
    payload, _raw = closeout._load_json(path, "官方 compact summary")
    turns = payload.get("turn_completed_count")
    if (
        payload.get("schema_version") != COMPACT_SUMMARY_SCHEMA
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or turns < 0
    ):
        raise ProvenanceError("官方 compact summary schema 或 turn 数非法")
    return payload


def _compact_turn_completed(root: Path, evidence: str) -> int | None:
    payload = _compact_summary(root, evidence)
    return None if payload is None else int(payload["turn_completed_count"])


def _compact_driver_proves_pre_request_failure(payload: Mapping[str, Any]) -> bool:
    """compact 驱动自己记录的事实：没有任何 app-server 协议记录、没有完成 turn、
    并带错误类型，说明客户端在进入对话前就失败，不可能发出模型请求。
    这是驱动的确定性记录，不依赖 TLS 解析。"""

    records = payload.get("protocol_record_count")
    return (
        isinstance(records, int)
        and not isinstance(records, bool)
        and records == 0
        and int(payload.get("turn_completed_count") or 0) == 0
        and isinstance(payload.get("error_type"), str)
        and bool(payload.get("error_type"))
    )


def _compact_branches(root: Path) -> list[dict[str, Any]]:
    run_id = root.name
    branches: list[dict[str, Any]] = []
    mitm_turns = _compact_turn_completed(root, "mitm")
    direct_turns = _compact_turn_completed(root, "direct")
    if mitm_turns is not None:
        mitm_root = root / "mitm"
        subjects = (
            sorted(p.name for p in mitm_root.iterdir() if p.is_dir() and not p.is_symlink())
            if mitm_root.is_dir()
            else []
        )
        requests: list[dict[str, Any]] = []
        found_any = False
        for subject in subjects:
            http_path = mitm_root / subject / "codex-http.jsonl"
            if not http_path.is_file():
                continue
            found_any = True
            requests.extend(
                _mitm_http_requests(
                    http_path,
                    producer_run_id=run_id,
                    source_kind="compact_mitm",
                    coordinate_prefix={"evidence": "mitm", "subject": subject},
                )
            )
        branch: dict[str, Any] = {
            "branch": "mitm",
            "kind": "compact",
            "evidence": "mitm",
            "subject": "codex-compact",
            "scenario": "",
            "turn_completed": mitm_turns,
            "requests": requests if found_any else [],
        }
        if found_any:
            branch["status"] = "resolved"
            branch["authority_source"] = "compact_mitm"
        else:
            branch["status"] = "unresolved"
            branch["reason"] = "mitm 分支没有 codex-http.jsonl"
        branches.append(branch)
    if direct_turns is not None:
        direct_branch: dict[str, Any] = {
            "branch": "direct",
            "kind": "compact",
            "evidence": "direct",
            "subject": "codex-compact",
            "scenario": "",
            "turn_completed": direct_turns,
            "requests": [],
            "status": "pending_estimate",
        }
        direct_summary = _compact_summary(root, "direct") or {}
        if _compact_driver_proves_pre_request_failure(direct_summary):
            direct_branch["status"] = "resolved"
            direct_branch["authority_source"] = "compact_driver_pre_request_failure"
            direct_branch["driver_error_type"] = direct_summary.get("error_type")
        branches.append(direct_branch)
    return branches


# ---------------------------------------------------------------------------
# relay 类
# ---------------------------------------------------------------------------


def _ws_client_messages(raw: bytes) -> Iterator[dict[str, Any]]:
    """逐条产出 Upgrade 之后的完整客户端文本消息，带消息序号。

    解帧与 ``model_condition_receipts._ws_request_models`` 逐字一致：客户端帧
    带 mask，permessage-deflate 上下文接管要求整条连接共用一个解压器。
    这里额外记录序号与 ``type``，供 provenance 生成原生坐标。
    """

    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    head_end = raw.find(b"\r\n\r\n")
    if head_end < 0:
        return
    data = raw[head_end + 4 :]
    pos = 0
    buffer = bytearray()
    compressed = False
    collecting = False
    ordinal = 0

    def flush() -> dict[str, Any] | None:
        nonlocal collecting, compressed, ordinal
        if not collecting:
            return None
        body = bytes(buffer)
        buffer.clear()
        collecting = False
        was_compressed = compressed
        compressed = False
        if was_compressed:
            try:
                body = inflater.decompress(body + b"\x00\x00\xff\xff")
            except zlib.error:
                return None
        try:
            obj = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            obj = None
        ordinal += 1
        return {"ordinal": ordinal, "payload": obj if isinstance(obj, dict) else None}

    while pos + 2 <= len(data):
        b0, b1 = data[pos], data[pos + 1]
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        rsv1 = bool(b0 & 0x40)
        masked, length = bool(b1 & 0x80), b1 & 0x7F
        cur = pos + 2
        if length == 126:
            if cur + 2 > len(data):
                break
            length = int.from_bytes(data[cur : cur + 2], "big")
            cur += 2
        elif length == 127:
            if cur + 8 > len(data):
                break
            length = int.from_bytes(data[cur : cur + 8], "big")
            cur += 8
        mask = b""
        if masked:
            if cur + 4 > len(data):
                break
            mask = data[cur : cur + 4]
            cur += 4
        if cur + length > len(data):
            break
        payload = data[cur : cur + length]
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x1:
            buffer.clear()
            buffer.extend(payload)
            compressed = rsv1
            collecting = True
            if fin:
                message = flush()
                if message is not None:
                    yield message
        elif opcode == 0x0 and collecting:
            buffer.extend(payload)
            if fin:
                message = flush()
                if message is not None:
                    yield message
        pos = cur + length


def _relay_branches(root: Path) -> list[dict[str, Any]]:
    relay_root = root / "relay"
    if not relay_root.exists():
        return []
    if relay_root.is_symlink() or not relay_root.is_dir():
        raise ProvenanceError("relay evidence root 不可信")
    run_id = root.name
    conn_paths = sorted(
        p for p in relay_root.iterdir() if CONN_FILE_RE.fullmatch(p.name)
    )
    if not conn_paths:
        manifest_path = relay_root / "relay.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return []
        manifest, _raw = closeout._load_json(manifest_path, "relay 零连接 manifest")
        if (
            manifest.get("schema_version") != RELAY_MANIFEST_SCHEMA
            or manifest.get("connections") != []
        ):
            raise ProvenanceError("relay 没有请求字节且零连接 manifest 非法")
        return [
            {
                "branch": "relay",
                "evidence": "relay",
                "status": "resolved",
                "authority_source": "relay_zero_connections",
                "requests": [],
                "turn_completed": 0,
            }
        ]
    requests: list[dict[str, Any]] = []
    for path in conn_paths:
        resolved = closeout._trusted_file(
            path, "relay 客户端原始字节", maximum=MAX_RELAY_BYTES
        )
        raw = resolved.read_bytes()
        digest = _sha256(raw)
        connection = CONN_FILE_RE.fullmatch(resolved.name).group(1)  # type: ignore[union-attr]
        http_ordinal = 0
        for message in model_condition_receipts._iter_messages(raw, response=False):
            http_ordinal += 1
            target = _path_without_query(message.get("target"))
            if message.get("method") != "POST" or target not in MODEL_ENDPOINTS:
                continue
            body = model_condition_receipts._json_body(message)
            model = body.get("model") if isinstance(body, dict) else None
            coordinate = {
                "connection": connection,
                "transport": "http",
                "message_ordinal": http_ordinal,
                "direction": "client_to_upstream",
            }
            requests.append(
                _request(
                    producer_run_id=run_id,
                    source_kind="relay",
                    native_coordinate=coordinate,
                    source_file=resolved,
                    source_sha256=digest,
                    record_offset=http_ordinal,
                    model=model if isinstance(model, str) else None,
                )
            )
        for message in _ws_client_messages(raw):
            payload = message["payload"]
            if not isinstance(payload, dict) or payload.get("type") != "response.create":
                continue
            model = payload.get("model")
            coordinate = {
                "connection": connection,
                "transport": "ws",
                "message_ordinal": message["ordinal"],
                "direction": "client_to_upstream",
            }
            requests.append(
                _request(
                    producer_run_id=run_id,
                    source_kind="relay",
                    native_coordinate=coordinate,
                    source_file=resolved,
                    source_sha256=digest,
                    record_offset=message["ordinal"],
                    model=model if isinstance(model, str) else None,
                )
            )
    return [
        {
            "branch": "relay",
            "evidence": "relay",
            "status": "resolved",
            "authority_source": "relay",
            "requests": requests,
            "turn_completed": None,
        }
    ]


# ---------------------------------------------------------------------------
# Campaign 级
# ---------------------------------------------------------------------------


def _apply_estimation(
    roots: list[tuple[Path, str, list[dict[str, Any]]]],
    *,
    estimation_policy: str,
) -> None:
    """在 Campaign 级为无法解析的 direct 分支求上界估计，并做 turn 下界核对。

    第一级：同一证据根内同 subject／scenario 的 mitm 分支精确数。
    第二级（需政策允许）：同一 Campaign 内同 subject 的全部 mitm 分支观测到的
    最大“请求数／完成 turn 数”倍率，乘以本分支的完成 turn 数。ws-handshake-repeat
    这类只跑 direct 的证据根没有同根 sibling，只能靠第二级；turn 数为零时仍未决。
    """

    ratio_by_subject: dict[str, float] = {}
    for _root, _kind, branches in roots:
        for branch in branches:
            if branch["status"] != "resolved" or branch["evidence"] != "mitm":
                continue
            turns = int(branch.get("turn_completed") or 0)
            if turns <= 0:
                continue
            ratio = len(branch["requests"]) / turns
            subject = str(branch["subject"])
            ratio_by_subject[subject] = max(ratio_by_subject.get(subject, 0.0), ratio)
    for _root, _kind, branches in roots:
        precise_in_root: dict[tuple[str, str], int] = {}
        for branch in branches:
            if branch["status"] == "resolved" and branch["evidence"] == "mitm":
                precise_in_root[(branch["subject"], branch["scenario"])] = len(branch["requests"])
        for branch in branches:
            if branch["status"] != "pending_estimate":
                continue
            key = (branch["subject"], branch["scenario"])
            turns = int(branch.get("turn_completed") or 0)
            if estimation_policy == "none":
                branch["status"] = "unresolved"
                branch["reason"] = "direct 分支无法解析请求，估计政策为 none"
                continue
            if key in precise_in_root:
                branch["status"] = "estimated"
                branch["estimation"] = "upper_bound_from_sibling"
                branch["estimated_count"] = precise_in_root[key]
                branch["estimation_basis"] = f"mitm/{key[0]}/{key[1]}"
                continue
            ratio = ratio_by_subject.get(branch["subject"])
            if (
                estimation_policy == "upper_bound_from_sibling_or_turn_ratio"
                and ratio is not None
                and turns > 0
            ):
                estimated = int(-(-ratio * turns // 1))
                branch["status"] = "estimated"
                branch["estimation"] = "upper_bound_from_turn_ratio"
                branch["estimated_count"] = estimated
                branch["estimation_basis"] = (
                    f"max_ratio({branch['subject']})={ratio:.4f} x turn_completed={turns}"
                )
                continue
            branch["status"] = "unresolved"
            branch["reason"] = (
                "direct 分支无法解析请求，且没有同根同场景 mitm 分支；"
                + (
                    "本分支完成 turn 数为零或同 subject 无 mitm 观测，倍率估计不可用"
                    if estimation_policy == "upper_bound_from_sibling_or_turn_ratio"
                    else "估计政策不允许倍率估计"
                )
            )
    for _root, _kind, branches in roots:
        for branch in branches:
            turns = branch.get("turn_completed")
            if not isinstance(turns, int):
                continue
            if branch["status"] == "resolved":
                counted = len(branch["requests"])
            elif branch["status"] == "estimated":
                counted = int(branch["estimated_count"])
            else:
                continue
            if counted < turns:
                raise ProvenanceError(
                    f"分支 {branch['branch']} 的请求数 {counted} 小于完成 turn 数 {turns}，"
                    "计数规则不自洽"
                )


def _root_branches(root: Path) -> tuple[str, list[dict[str, Any]]]:
    """识别证据根的类别并返回其执行分支；一个根只能属于一个类别。"""

    kinds: list[str] = []
    manifest_path = root / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        kinds.append("capture")
    if (root / "result" / "mitm" / "summary.json").is_file() or (
        root / "result" / "direct" / "summary.json"
    ).is_file():
        kinds.append("compact")
    if (root / "relay").exists():
        kinds.append("relay")
    if len(kinds) > 1:
        raise ProvenanceError(f"证据根 {root} 同时具备多种权威来源：{kinds}")
    if not kinds:
        return "unsupported", []
    kind = kinds[0]
    if kind == "capture":
        return kind, _capture_branches(root)
    if kind == "compact":
        return kind, _compact_branches(root)
    return kind, _relay_branches(root)


def collect_campaign_provenance(
    formal_campaign_dir: Path,
    *,
    formal_campaign_id: str,
    estimation_policy: str = "none",
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """按统一计量单位核算一个 Formal Campaign 的全部官方请求。"""

    if estimation_policy not in ESTIMATION_POLICIES:
        raise ProvenanceError(f"估计政策非法：{estimation_policy!r}")
    campaign_dir = closeout._trusted_directory(formal_campaign_dir, "Formal Campaign")
    manifest, raw = closeout._load_json(campaign_dir / "campaign.json", "Formal Campaign")
    if (
        manifest.get("campaign_id") != formal_campaign_id
        or manifest.get("campaign_mode") != "formal"
    ):
        raise ProvenanceError("provenance 的 Formal Campaign 身份不一致")
    digest_path = closeout._trusted_file(campaign_dir / "campaign.sha256", "Campaign 摘要")
    if digest_path.read_text(encoding="ascii").strip() != _sha256(raw):
        raise ProvenanceError("provenance 发现 Campaign 摘要漂移")
    jobs = manifest.get("jobs", [])
    if not isinstance(jobs, list):
        raise ProvenanceError("Formal Campaign jobs 非数组")
    configuration = manifest.get("configuration")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": formal_campaign_id,
        "observed_at_utc": observed_at_utc or _utc_now(),
        "counting_rule": COUNTING_RULE,
        "counting_unit": COUNTING_UNIT,
        "model_endpoints": sorted(MODEL_ENDPOINTS),
        "estimation_policy": estimation_policy,
        "jobs": [],
        "requests": [],
        "precise_total": 0,
        "estimated_total": 0,
        "unresolved_job_ids": [],
        "pending_job_ids": [],
        "pre_request_zero_job_ids": [],
    }
    if not jobs:
        receipt["status"] = "complete"
        return receipt
    if not isinstance(configuration, Mapping):
        raise ProvenanceError("Formal Campaign 缺少 configuration")
    host_data_root = closeout._formal_host_data_root(campaign_dir)
    capture_root = Path(str(configuration.get("capture_root", "")))
    if not capture_root.is_absolute() or capture_root == Path("/"):
        raise ProvenanceError("Formal Campaign CAPTURE_ROOT 非法")

    # 第一遍：发现每个 Job 的证据根与执行分支，不做估计。
    job_plans: list[dict[str, Any]] = []
    collected_roots: list[tuple[Path, str, list[dict[str, Any]]]] = []
    root_cache: dict[Path, tuple[str, list[dict[str, Any]]]] = {}
    for item in jobs:
        if not isinstance(item, Mapping) or item.get("phase") != "official":
            continue
        job_id = str(item.get("id", ""))
        if not SAFE_ID_RE.fullmatch(job_id):
            raise ProvenanceError("Formal official Job ID 非法")
        evidence_roots = item.get("evidence_roots")
        if not isinstance(evidence_roots, list):
            raise ProvenanceError(f"{job_id} evidence_roots 非数组")
        roots: list[Path] = []
        for value in evidence_roots:
            base = closeout._map_container_evidence_root(
                value, capture_root=capture_root, host_data_root=host_data_root
            )
            roots.extend(closeout._failed_attempt_roots(base))
        for root in roots:
            if root not in root_cache:
                kind, branches = _root_branches(root)
                root_cache[root] = (kind, branches)
                if kind != "unsupported":
                    collected_roots.append((root, kind, branches))
        job_plans.append({"job_id": job_id, "roots": roots, "logs": closeout._job_logs(campaign_dir, job_id)})
    _apply_estimation(collected_roots, estimation_policy=estimation_policy)

    # 第二遍：按 Job 汇总，同一证据根首次归属的 Job 拥有其请求与估计。
    seen_keys: dict[str, str] = {}
    seen_roots: dict[Path, str] = {}
    for plan in job_plans:
        job_id = plan["job_id"]
        roots = plan["roots"]
        logs = plan["logs"]
        job_entry: dict[str, Any] = {
            "job_id": job_id,
            "roots": [],
            "precise_count": 0,
            "estimated_count": 0,
        }
        if not roots and not logs:
            job_entry["status"] = "pending"
            receipt["pending_job_ids"].append(job_id)
            receipt["jobs"].append(job_entry)
            continue
        supported = False
        unresolved = False
        estimated = False
        for root in roots:
            kind, branches = root_cache[root]
            if kind == "unsupported":
                continue
            supported = True
            owner = seen_roots.setdefault(root, job_id)
            root_entry: dict[str, Any] = {
                "root": str(root),
                "producer_run_id": root.name,
                "kind": kind,
                "first_owner_job_id": owner,
                "branches": [],
            }
            for branch in branches:
                slim = {k: v for k, v in branch.items() if k != "requests"}
                slim["request_count"] = len(branch["requests"])
                root_entry["branches"].append(slim)
                if branch["status"] == "unresolved":
                    unresolved = True
                elif branch["status"] == "estimated":
                    estimated = True
                    if owner == job_id:
                        job_entry["estimated_count"] += int(branch["estimated_count"])
                for request in branch["requests"]:
                    key = request["identity_key"]
                    if key in seen_keys:
                        continue
                    seen_keys[key] = job_id
                    receipt["requests"].append({**request, "job_id": job_id})
                    job_entry["precise_count"] += 1
            job_entry["roots"].append(root_entry)
        if not supported:
            if job_id == "official-http-fallback" or closeout._logs_prove_pre_request_failure(logs):
                job_entry["status"] = "pre_request_zero"
                receipt["pre_request_zero_job_ids"].append(job_id)
            else:
                job_entry["status"] = "unresolved"
                job_entry["reason"] = "没有可识别的权威来源且日志不能证明请求前失败"
                receipt["unresolved_job_ids"].append(job_id)
        elif unresolved:
            job_entry["status"] = "unresolved"
            receipt["unresolved_job_ids"].append(job_id)
        elif estimated:
            job_entry["status"] = "estimated"
        else:
            job_entry["status"] = "resolved"
        receipt["jobs"].append(job_entry)
    receipt["precise_total"] = len(receipt["requests"])
    receipt["estimated_total"] = sum(int(j["estimated_count"]) for j in receipt["jobs"])
    receipt["status"] = (
        "accounting_unresolved" if receipt["unresolved_job_ids"] else "complete"
    )
    receipt["identity_keys_sha256"] = _sha256(
        _canonical(sorted(r["identity_key"] for r in receipt["requests"]))
    )
    return receipt


def audit_project(
    project_root: Path,
    *,
    since_utc: str,
    estimation_policy: str = "none",
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """扫描宿主数据根下全部 Formal Campaign，按身份键全局去重。"""

    root = closeout._trusted_directory(project_root, "宿主数据根")
    campaigns_root = root / "evidence" / "campaigns"
    if not campaigns_root.is_dir() or campaigns_root.is_symlink():
        raise ProvenanceError("宿主数据根缺少 evidence/campaigns")
    since = closeout._timestamp(since_utc, "since")
    entries: list[dict[str, Any]] = []
    global_keys: dict[str, str] = {}
    duplicates: list[dict[str, str]] = []
    unresolved: list[str] = []
    precise_total = 0
    estimated_total = 0
    for campaign_dir in sorted(p for p in campaigns_root.iterdir() if p.is_dir()):
        manifest_path = campaign_dir / "campaign.json"
        if not manifest_path.is_file():
            continue
        manifest, _raw = closeout._load_json(manifest_path, "Campaign")
        if manifest.get("campaign_mode") != "formal":
            continue
        created = manifest.get("created_at_utc")
        if not isinstance(created, str):
            continue
        if closeout._timestamp(created, "created_at_utc") < since:
            continue
        campaign_id = str(manifest.get("campaign_id", ""))
        receipt = collect_campaign_provenance(
            campaign_dir,
            formal_campaign_id=campaign_id,
            estimation_policy=estimation_policy,
            observed_at_utc=observed_at_utc,
        )
        unique = 0
        for request in receipt["requests"]:
            key = request["identity_key"]
            if key in global_keys:
                duplicates.append(
                    {
                        "identity_key": key,
                        "first_campaign_id": global_keys[key],
                        "duplicate_campaign_id": campaign_id,
                    }
                )
                continue
            global_keys[key] = campaign_id
            unique += 1
        precise_total += unique
        estimated_total += int(receipt["estimated_total"])
        if receipt["status"] == "accounting_unresolved":
            unresolved.append(campaign_id)
        entries.append(
            {
                "campaign_id": campaign_id,
                "campaign_dir": str(campaign_dir),
                "created_at_utc": created,
                "status": receipt["status"],
                "precise_count": receipt["precise_total"],
                "precise_count_after_dedup": unique,
                "estimated_count": receipt["estimated_total"],
                "unresolved_job_ids": receipt["unresolved_job_ids"],
                "pending_job_ids": receipt["pending_job_ids"],
                "identity_keys_sha256": receipt["identity_keys_sha256"],
            }
        )
    return {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "observed_at_utc": observed_at_utc or _utc_now(),
        "since_utc": since_utc,
        "counting_rule": COUNTING_RULE,
        "counting_unit": COUNTING_UNIT,
        "estimation_policy": estimation_policy,
        "status": "accounting_unresolved" if unresolved else "complete",
        "campaigns": entries,
        "duplicates": duplicates,
        "unresolved_campaign_ids": unresolved,
        "precise_total": precise_total,
        "estimated_total": estimated_total,
        "identity_keys_sha256": _sha256(_canonical(sorted(global_keys))),
        "identity_key_count": len(global_keys),
    }


def write_receipt(payload: Mapping[str, Any], path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise ProvenanceError(f"收据已存在，不得覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="以统一计量单位从不可变证据核算模型请求（只读）。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    campaign = subparsers.add_parser("collect-campaign", help="核算单个 Formal Campaign")
    campaign.add_argument("--campaign-dir", type=Path, required=True)
    campaign.add_argument("--campaign-id", required=True)
    campaign.add_argument("--estimation-policy", choices=ESTIMATION_POLICIES, default="none")
    campaign.add_argument("--output", type=Path, required=True)
    project = subparsers.add_parser("audit-project", help="扫描宿主数据根下全部 Formal Campaign")
    project.add_argument("--project-root", type=Path, required=True)
    project.add_argument("--since", required=True, help="RFC3339，只统计此后创建的 Campaign")
    project.add_argument("--estimation-policy", choices=ESTIMATION_POLICIES, default="none")
    project.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "collect-campaign":
            payload = collect_campaign_provenance(
                arguments.campaign_dir,
                formal_campaign_id=arguments.campaign_id,
                estimation_policy=arguments.estimation_policy,
            )
        else:
            payload = audit_project(
                arguments.project_root,
                since_utc=arguments.since,
                estimation_policy=arguments.estimation_policy,
            )
        write_receipt(payload, arguments.output)
    except (ProvenanceError, closeout.VC0CloseoutError, OSError) as error:
        print(f"provenance 失败：{error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": payload["status"],
                "precise_total": payload["precise_total"],
                "estimated_total": payload["estimated_total"],
                "output": str(arguments.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["status"] == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())
