#!/usr/bin/env python3
"""Claude Code 升级的逐请求 provenance（Claude 升级控制面设计 B1-11），输出 ``live-request-provenance/v2``。

计量单位只有一种：客户端发往 ``api.anthropic.com`` 的一次 HTTP ``POST`` 模型端点（:data:`MODEL_ENDPOINTS`，
比较时去掉 query）。``platform.claude.com`` 的 OAuth token refresh 记入 ``auxiliary_requests``，单独计数、不进
模型请求预算；GET／HEAD 辅助端点（``/api/hello``、``policy_limits``、``settings``、``oauth/profile``、
``mcp_servers``）不计。计的是官方客户端发出的请求，中继是否转发到生产只记在分支元数据里。

来源与原生坐标（身份键只由 ``producer_run_id + source_kind + 原生坐标`` 生成，与 Codex 同构；真实路径、
文件摘要、记录偏移只作完整性字段）。``producer_run_id`` 取证据根相对宿主数据根的路径（Campaign 核算），或相对
所核算各根公共父目录的路径（离线核算一组根）：Codex 取根目录名，依赖其布局保证根名唯一；Claude 的录制目录
（如 ``attempts/<probe>/attempt-001``）根名并不唯一，取相对路径才不撞键。两个不同证据根得到同一
``producer_run_id`` 时失败关闭。

* ``claude_relay``：证据根下 ``relay/connNNN.client_to_upstream.bin``，按 Content-Length／chunked 严格切分
  HTTP/1.1 请求，坐标 ``{connection, transport, message_ordinal, direction}``。``relay/relay.json`` 登记为有效的
  连接缺少原始字节、或字节无法切分时，该分支未决。
  计数只看客户端字节：``relay.json`` 记为零字节形态（``bytes``、``sha256`` 为空对象、``segments`` 为空数组，且两个
  方向都没有字节文件）的连接没有发出任何请求，计零，逐条留证在分支的 ``zero_client_byte_connections`` 里——
  中继完整性登记为握手前断开的（运行根 ``relay-manifest.json`` 的 ``relay_integrity.excluded_connections``，其中
  live 空握手在 ``relay.json`` 里也记 ``valid: true``）分类 ``relay_excluded``，没有登记的分类 ``unexcluded``。
  中继登记与字节互相矛盾（登记的连接有客户端字节、登记的连接不在 ``relay.json``、登记的连接不是零字节形态、
  零客户端字节却有上游字节）或登记了未知原因时，该分支未决。
* ``claude_mitm``：capture 类证据根（``manifest.json`` 为 ``official-client-capture/v1``）的 mitm 分支，读
  ``mitm/<subject>/<scenario>/`` 下 mitm 插件写出的 ``claude-http.jsonl`` 与 ``misc-http.jsonl``，坐标
  ``{evidence, subject, scenario, file, record_index, transport, flow_id}``。
* ``claude_direct``：capture 类的 direct 分支只有 TLS 加密的 pcap，按项目总账冻结的估计政策处理：同一证据根
  内同 subject／scenario 有 mitm 分支时取其精确数作上界估计；Claude 没有逐 turn 完成事件，倍率估计不可用，
  其余情况未决。

三态与 Codex 相同：``resolved``（精确）、``estimated``（上界估计）、``unresolved``（未决，挡该 Campaign 的
账务）。续跑取代（证据根同级出现 ``<根名>.superseded-<attempt>`` 归档）后第 n 代（n≥1）的证据根，全部请求的
身份键并入世代号：Claude 没有按旧口径入账的历史键要保持不变，不必像 Codex 计数规则 v3 那样只换撞键的请求。

Campaign 布局与 Codex 共用（设计 D3）：``campaign.json``／``campaign.sha256``、``official/attempts/<attempt>``
与 ``candidates/<id>/attempts/<attempt>`` 下的 ``reservation.json``、``job-<id>.json``、``logs/``，以及
``recovery/ar<k>`` 恢复段。宿主机路径与可信读取调用 ``codex_upgrade_vc0_closeout`` 的函数，只调用不修改。

Claude 的 Job 证据根（B2-05／B2-06 布局）：一个 Job 一个证据根 ``<runs>/claude-official/<campaign>/<job>``，它自身
不是运行根，每步的 FW-E 中继运行根是它下面以 probe 命名的子目录。这样的根按 ``relay_runs`` 展开成逐 probe 的
relay 分支，``producer_run_id`` 取 ``<Job 根的 run id>/<probe>``（即子目录相对同一基准的路径，不同 probe 的同坐标
请求不撞键），世代号沿用 Job 根（取代归档与失败归档都在 Job 根这一级）。Job 根下不是中继运行根的条目使该根未决；
唯一例外是候选 Job 收据绑定且重新核过合同、候选坐标、执行步骤与摘要的恢复报告。目录名本身不构成信任依据，
离线核算没有 Job 收据上下文时仍然未决，报告目录出现任何未绑定文件也仍然未决。
总计划没有冻结任何 Job、attempt 却登记了 Job 证据根时，这些 Job 未决（失败关闭）；两者都没有时才是零请求的 complete。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    import codex_upgrade_evidence_permissions as evidence_permissions
    import codex_upgrade_vc0_closeout as closeout
else:
    from . import codex_upgrade_evidence_permissions as evidence_permissions
    from . import codex_upgrade_vc0_closeout as closeout

SCHEMA_VERSION = "live-request-provenance/v2"
CLIENT = "claude"
COUNTING_RULE = "claude_model_requests/v1"
COUNTING_UNIT = "http_post_anthropic_model_endpoint"
MODEL_HOST = "api.anthropic.com"
MODEL_ENDPOINTS = frozenset({"/v1/messages", "/v1/messages/count_tokens", "/v1/messages/batches"})
# (host, method, 去掉 query 的 path) → 辅助请求类别；只计数，不进模型请求预算。
AUXILIARY_ENDPOINTS: Mapping[tuple[str, str, str], str] = {
    ("platform.claude.com", "POST", "/v1/oauth/token"): "token_refresh",
}
# 与 Codex 同名：项目总账冻结的估计政策取值相同。Claude 没有逐 turn 完成事件，第三项只能退化为第二项。
ESTIMATION_POLICIES = ("none", "upper_bound_from_sibling", "upper_bound_from_sibling_or_turn_ratio")
SOURCE_RELAY = "claude_relay"
SOURCE_MITM = "claude_mitm"
SOURCE_DIRECT = "claude_direct"
CAPTURE_MANIFEST_SCHEMA = "official-client-capture/v1"
RELAY_MANIFEST_SCHEMA = "byte-relay/v1"
# FW-E 中继运行根上的中继清单（claude_fw_e_relay 的 SCHEMA_RUN）：只读其中的中继完整性登记。
RELAY_RUN_MANIFEST_FILENAME = "relay-manifest.json"
RELAY_RUN_MANIFEST_SCHEMA = "claude-code-fw-e-relay-run/v1"
# 中继完整性登记的握手前断开原因（与 claude_fw_e_relay 的四个 *_TERMINATION_REASON 逐字相同，由测试锁定）：
# 这些连接在 TLS 握手阶段就结束，客户端没有发出任何应用数据。中继新增原因时这里必须同步登记，否则失败关闭。
RELAY_HANDSHAKE_EXCLUSION_REASONS = frozenset(
    {
        "client_tls_handshake_terminated_before_application_data",
        "relay_shutdown_terminated_handshake_before_application_data",
        "client_reset_transport_before_tls_handshake",
        "live_client_closed_tls_handshake_before_application_data",
    }
)
ZERO_BYTE_RELAY_EXCLUDED = "relay_excluded"
ZERO_BYTE_UNEXCLUDED = "unexcluded"
# Claude Job 证据根（自身不是运行根、probe 子目录是 FW-E 中继运行根）展开后的证据根类别。
RELAY_RUNS_KIND = "relay_runs"
# 与候选采集的恢复报告合同同值，由直接回归锁定；不导入采集编排器，避免把控制闭包并入请求计量。
CANDIDATE_RESTORATION_DIRNAME = "candidate-gateway"
CANDIDATE_RESTORATION_SCHEMA = "claude-candidate-gateway-restoration/v1"
CANDIDATE_RESTORATION_EQUAL_FIELDS = (
    "account_proxy_restored_equal", "account_gate_restored_equal", "hosts_sha256_equal",
    "ca_bundle_sha256_equal", "runtime_secrets_removed",
)
CAMPAIGN_SCHEMA = "official-client-upgrade-campaign/v1"
# mitm 插件按路径分类写出的 HTTP 记录文件（addons/mitm_capture._classify）：模型端点在 claude，token refresh 在 misc。
MITM_HTTP_FILES = ("claude-http.jsonl", "misc-http.jsonl")
MAX_JSONL_BYTES = 256 * 1024 * 1024
MAX_RELAY_BYTES = 512 * 1024 * 1024
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CONN_FILE_RE = re.compile(r"^(conn(\d+))\.client_to_upstream\.bin$")
REQUEST_LINE_RE = re.compile(rb"^([A-Z]+) (\S+) HTTP/1\.[01]$")
RECOVERY_REVISION_RE = re.compile(r"^ar[1-9][0-9]*$")
FAILED_ATTEMPT_ROOT_RE = re.compile(
    r"^(?P<base>.+)\.failed-attempt(?P<attempt>[1-9][0-9]*)(?:-(?P<collision>[1-9][0-9]*))?$"
)
# 续跑取代归档的命名与编排器同一来源：``<原名>.superseded-<新 attempt>[-<k>]``。
SUPERSEDED_ROOT_INFIX = evidence_permissions.SUPERSEDED_ROOT_INFIX


class ProvenanceError(ValueError):
    """证据不可信、身份不一致或输入非法；无法精确计数但证据可信时不抛错，记为未决。"""


class _StreamError(ValueError):
    """一条连接的客户端字节无法严格切分为 HTTP/1.1 请求。"""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def identity_key(
    producer_run_id: str,
    source_kind: str,
    native_coordinate: Mapping[str, Any],
    *,
    supersession_generation: int | None = None,
) -> str:
    """身份键；同一请求被复制到别处仍是同一个键。生成方式与 Codex 的 ``identity_key`` 逐字相同。"""

    identity: dict[str, Any] = {
        "producer_run_id": producer_run_id,
        "source_kind": source_kind,
        "native_coordinate": dict(native_coordinate),
    }
    if supersession_generation is not None:
        if (
            not isinstance(supersession_generation, int)
            or isinstance(supersession_generation, bool)
            or supersession_generation < 1
        ):
            raise ProvenanceError("续跑取代世代号必须是正整数")
        identity["supersession_generation"] = supersession_generation
    return _sha256(_canonical(identity))


def endpoint_category(host: str, method: str, path: str) -> str | None:
    """请求的计量类别：``model``、辅助类别名，或不计（None）。``path`` 已去掉 query。"""

    if host == MODEL_HOST and method == "POST" and path in MODEL_ENDPOINTS:
        return "model"
    return AUXILIARY_ENDPOINTS.get((host, method, path))


def _normalized_host(value: Any) -> str:
    host = str(value or "").strip().lower()
    return host[: -len(":443")] if host.endswith(":443") else host


def _body_model(body: bytes, encoding: str) -> str | None:
    """从请求正文取顶层 ``model``；取不到一律 None，不影响计数。"""

    raw = body
    try:
        if encoding == "gzip" and raw:
            raw = gzip.decompress(raw)
        elif encoding not in {"", "identity"}:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, EOFError, UnicodeError, ValueError):
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    return model if isinstance(model, str) else None


# ---------------------------------------------------------------------------
# HTTP/1.1 请求切分
# ---------------------------------------------------------------------------


def _dechunk(raw: bytes, start: int) -> tuple[bytes, int]:
    body = bytearray()
    cursor = start
    while True:
        line_end = raw.find(b"\r\n", cursor)
        if line_end < 0:
            raise _StreamError("chunked 正文缺少块长度行")
        size_text = raw[cursor:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as error:
            raise _StreamError("chunked 块长度非法") from error
        cursor = line_end + 2
        if size == 0:
            trailer_end = raw.find(b"\r\n\r\n", cursor - 2)
            if trailer_end < 0:
                raise _StreamError("chunked 正文缺少结尾")
            return bytes(body), trailer_end + 4
        if cursor + size + 2 > len(raw) or raw[cursor + size: cursor + size + 2] != b"\r\n":
            raise _StreamError("chunked 块越过流结尾或缺少分隔")
        body.extend(raw[cursor: cursor + size])
        cursor += size + 2


def split_http_requests(raw: bytes) -> list[dict[str, Any]]:
    """把一条连接的客户端字节严格切成 HTTP/1.1 请求，无法切分即抛 ``_StreamError``。

    keep-alive 连接里下一个请求紧跟在上一个请求正文之后，不在行首，只能按 Content-Length（或 chunked）
    逐个切分；按行首匹配请求行会漏数。
    """

    requests: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(raw):
        head_end = raw.find(b"\r\n\r\n", cursor)
        if head_end < 0:
            raise _StreamError(f"偏移 {cursor} 起缺少 Header 结束")
        lines = raw[cursor:head_end].split(b"\r\n")
        match = REQUEST_LINE_RE.fullmatch(lines[0])
        if match is None:
            raise _StreamError(f"偏移 {cursor} 的请求行非法")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            key = name.decode("latin-1").strip().lower()
            if not separator or not key:
                raise _StreamError(f"偏移 {cursor} 的 Header 行非法")
            if key in headers and key in {"content-length", "transfer-encoding", "host"}:
                raise _StreamError(f"偏移 {cursor} 重复 Header：{key}")
            headers[key] = value.decode("latin-1").strip()
        body_start = head_end + 4
        if "transfer-encoding" in headers:
            if headers["transfer-encoding"].lower() != "chunked" or "content-length" in headers:
                raise _StreamError(f"偏移 {cursor} 的 Transfer-Encoding 不受支持")
            body, end = _dechunk(raw, body_start)
        else:
            length = headers.get("content-length", "0")
            if not length.isdigit():
                raise _StreamError(f"偏移 {cursor} 的 Content-Length 非法")
            end = body_start + int(length)
            if end > len(raw):
                raise _StreamError(f"偏移 {cursor} 的 Content-Length 越过流结尾")
            body = raw[body_start:end]
        if "upgrade" in headers and end < len(raw):
            raise _StreamError("连接升级之后仍有客户端字节，Claude 不应出现 WebSocket 等升级")
        target = match.group(2).decode("latin-1")
        requests.append(
            {
                "method": match.group(1).decode("ascii"),
                "target": target,
                "path": target.split("?", 1)[0],
                "host": _normalized_host(headers.get("host")),
                "content_encoding": headers.get("content-encoding", "").strip().lower(),
                "body": body,
                "offset": cursor,
            }
        )
        cursor = end
    return requests


# ---------------------------------------------------------------------------
# 证据根解析
# ---------------------------------------------------------------------------


def _record(
    *,
    producer_run_id: str,
    source_kind: str,
    coordinate: Mapping[str, Any],
    generation: int,
    source_file: Path,
    source_sha256: str,
    record_offset: int,
    host: str,
    method: str,
    path: str,
    category: str,
    model: str | None,
) -> dict[str, Any]:
    return {
        "identity_key": identity_key(
            producer_run_id, source_kind, coordinate, supersession_generation=generation or None
        ),
        "producer_run_id": producer_run_id,
        "source_kind": source_kind,
        "native_coordinate": dict(coordinate),
        "source_file": str(source_file),
        "source_sha256": source_sha256,
        "record_offset": record_offset,
        "host": host,
        "method": method,
        "endpoint": path,
        "category": category,
        "model": model,
    }


def _relay_handshake_exclusions(root: Path) -> dict[int, str]:
    """运行根 ``relay-manifest.json`` 里中继完整性登记的握手前断开连接：连接号 → 原因。

    没有中继清单（非 FW-E 运行根）、或中继完整性没有通过（失败 run 的 ``relay_integrity`` 为 null，或结果不是
    passed）时没有可用的登记，返回空表；清单存在但合同名或登记形态非法即失败关闭。原因是否已登记由调用方判定。
    """

    path = root / RELAY_RUN_MANIFEST_FILENAME
    if not path.exists() and not path.is_symlink():
        return {}
    manifest, _raw = closeout._load_json(path, "中继清单")
    if manifest.get("schema_version") != RELAY_RUN_MANIFEST_SCHEMA:
        raise ProvenanceError(f"中继清单 schema 非预期：{path}")
    integrity = manifest.get("relay_integrity")
    if not isinstance(integrity, Mapping) or integrity.get("result") != "passed":
        return {}
    rows = integrity.get("excluded_connections")
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise ProvenanceError(f"中继清单 excluded_connections 不是数组：{path}")
    exclusions: dict[int, str] = {}
    for row in rows:
        connection_id = row.get("connection_id") if isinstance(row, Mapping) else None
        reason = row.get("reason") if isinstance(row, Mapping) else None
        if (
            not isinstance(connection_id, int)
            or isinstance(connection_id, bool)
            or connection_id <= 0
            or not isinstance(reason, str)
            or not reason
        ):
            raise ProvenanceError(f"中继清单的握手前断开登记形态非法：{path}")
        if connection_id in exclusions:
            raise ProvenanceError(f"中继清单重复登记握手前断开连接 {connection_id}：{path}")
        exclusions[connection_id] = reason
    return exclusions


def _zero_client_byte_record(item: Mapping[str, Any]) -> bool:
    """``relay.json`` 的连接记录是否为零字节形态：中继记下的字节数、摘要与分段全部为空。"""

    return item.get("bytes") == {} and item.get("sha256") == {} and item.get("segments") == []


def _classify_connections_without_client_bytes(
    relay_root: Path,
    connections: list[Any],
    present: set[int],
    exclusions: Mapping[int, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """逐连接核对中继登记与客户端字节：返回 (零客户端字节连接的留证, 使分支未决的问题)。

    计数只看客户端字节：零字节形态且两个方向都没有字节文件的连接没有发出任何请求，计零并留证（有中继完整性登记的
    分类 ``relay_excluded``，没有的分类 ``unexcluded``）；``valid`` 为真却缺少字节（未知或非零字节数）仍是未决。
    中继登记与字节互相矛盾、或登记了未知原因时，不据登记剔除，也不只信字节，整个分支未决。
    """

    registered: dict[int, Mapping[str, Any]] = {}
    duplicated: set[int] = set()
    for item in connections:
        if isinstance(item, Mapping) and isinstance(item.get("connection_id"), int) and not isinstance(item.get("connection_id"), bool):
            connection_id = int(item["connection_id"])
            if connection_id in registered:
                duplicated.add(connection_id)
            registered.setdefault(connection_id, item)
    zero_bytes: list[dict[str, Any]] = []
    problems: list[str] = [f"relay.json 重复登记连接号：{sorted(duplicated)}"] if duplicated else []
    missing: list[int] = []
    for connection_id in sorted(registered):
        item = registered[connection_id]
        reason = exclusions.get(connection_id)
        if connection_id in present:
            if reason is not None:
                problems.append(f"中继清单登记为握手前断开的连接 {connection_id} 存在客户端字节")
            continue
        if not _zero_client_byte_record(item):
            if reason is not None:
                problems.append(f"中继清单登记为握手前断开的连接 {connection_id} 不是零字节形态，缺少客户端原始字节")
            elif item.get("valid") is True:
                missing.append(connection_id)
            continue
        if (relay_root / f"conn{connection_id:03d}.upstream_to_client.bin").exists():
            problems.append(f"连接 {connection_id} 没有客户端字节却有上游字节")
            continue
        if reason is not None and reason not in RELAY_HANDSHAKE_EXCLUSION_REASONS:
            problems.append(f"中继清单登记了未登记的握手前断开原因：{reason}（连接 {connection_id}）")
            continue
        zero_bytes.append(
            {
                "connection_id": connection_id,
                "valid": item.get("valid"),
                "classification": ZERO_BYTE_RELAY_EXCLUDED if reason is not None else ZERO_BYTE_UNEXCLUDED,
                "reason": reason,
            }
        )
    if missing:
        problems.append(f"relay 登记为有效的连接缺少客户端原始字节：{missing}")
    stray = sorted(set(exclusions) - set(registered))
    if stray:
        problems.append(f"中继清单登记的握手前断开连接 {stray} 不在 relay.json")
    return zero_bytes, problems


def _relay_branches(root: Path, *, producer_run_id: str, generation: int) -> list[dict[str, Any]]:
    relay_root = root / "relay"
    if relay_root.is_symlink() or not relay_root.is_dir():
        raise ProvenanceError(f"relay 证据根不可信：{relay_root}")
    manifest: dict[str, Any] | None = None
    manifest_path = relay_root / "relay.json"
    if manifest_path.exists():
        manifest, _raw = closeout._load_json(manifest_path, "relay manifest")
        if manifest.get("schema_version") != RELAY_MANIFEST_SCHEMA:
            raise ProvenanceError(f"relay manifest schema 非预期：{manifest_path}")
    branch: dict[str, Any] = {
        "branch": "relay",
        "kind": SOURCE_RELAY,
        "evidence": "relay",
        "subject": "relay",
        "scenario": "",
        "production_forwarding_enabled": manifest.get("production_forwarding_enabled") if manifest else None,
        "requests": [],
        "auxiliary_requests": [],
    }
    conn_paths = sorted(path for path in relay_root.iterdir() if CONN_FILE_RE.fullmatch(path.name))
    connections = manifest.get("connections") if manifest is not None else None
    if manifest is not None and not isinstance(connections, list):
        raise ProvenanceError("relay manifest connections 不是数组")
    present = {int(CONN_FILE_RE.fullmatch(path.name).group(2)) for path in conn_paths}  # type: ignore[union-attr]
    zero_bytes, problems = _classify_connections_without_client_bytes(
        relay_root, connections or [], present, _relay_handshake_exclusions(root)
    )
    branch["zero_client_byte_connections"] = zero_bytes
    if problems:
        branch.update(status="unresolved", reason="；".join(problems))
        return [branch]
    if not conn_paths:
        if connections == []:
            branch.update(status="resolved", authority_source="relay_zero_connections")
        elif isinstance(connections, list) and len(zero_bytes) == len(connections):
            # 登记的每条连接都是零客户端字节：这个运行根没有发出任何请求。
            branch.update(status="resolved", authority_source="relay_zero_client_bytes")
        else:
            branch.update(status="unresolved", reason="relay 没有客户端原始字节，且 relay.json 未登记零连接")
        return [branch]
    for path in conn_paths:
        resolved = closeout._trusted_file(path, "relay 客户端原始字节", maximum=MAX_RELAY_BYTES)
        raw = resolved.read_bytes()
        digest = _sha256(raw)
        connection = CONN_FILE_RE.fullmatch(resolved.name).group(1)  # type: ignore[union-attr]
        try:
            messages = split_http_requests(raw)
        except _StreamError as error:
            branch.update(status="unresolved", reason=f"{connection} 无法切分为 HTTP/1.1 请求：{error}")
            branch["requests"], branch["auxiliary_requests"] = [], []
            return [branch]
        for ordinal, message in enumerate(messages, start=1):
            category = endpoint_category(message["host"], message["method"], message["path"])
            if category is None:
                continue
            record = _record(
                producer_run_id=producer_run_id,
                source_kind=SOURCE_RELAY,
                coordinate={
                    "connection": connection,
                    "transport": "http",
                    "message_ordinal": ordinal,
                    "direction": "client_to_upstream",
                },
                generation=generation,
                source_file=resolved,
                source_sha256=digest,
                record_offset=int(message["offset"]),
                host=message["host"],
                method=message["method"],
                path=message["path"],
                category=category,
                model=_body_model(message["body"], message["content_encoding"]) if category == "model" else None,
            )
            branch["requests" if category == "model" else "auxiliary_requests"].append(record)
    branch.update(status="resolved", authority_source=SOURCE_RELAY)
    return [branch]


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


def _mitm_body_model(body: Any) -> str | None:
    """mitm 插件把正文记成摘要对象：模型在 ``body.json`` 或 ``body.text`` 里。"""

    if not isinstance(body, Mapping):
        return None
    payload = body.get("json")
    if not isinstance(payload, Mapping):
        try:
            payload = json.loads(body["text"]) if isinstance(body.get("text"), str) else None
        except json.JSONDecodeError:
            payload = None
    model = payload.get("model") if isinstance(payload, Mapping) else None
    return model if isinstance(model, str) else None


def _capture_cases(root: Path) -> list[dict[str, str]]:
    payload, _raw = closeout._load_json(root / "manifest.json", "官方 capture manifest")
    if payload.get("schema_version") != CAPTURE_MANIFEST_SCHEMA:
        raise ProvenanceError("官方 capture manifest schema 非预期")
    cases = payload.get("case_results")
    if not isinstance(cases, list):
        raise ProvenanceError("官方 capture manifest 缺少 case_results")
    normalized: list[dict[str, str]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ProvenanceError(f"capture case_results[{index}] 不是对象")
        evidence, subject, scenario = case.get("evidence"), case.get("subject"), case.get("scenario")
        if (
            evidence not in {"direct", "mitm"}
            or not isinstance(subject, str)
            or not SAFE_ID_RE.fullmatch(subject)
            or not isinstance(scenario, str)
            or not SAFE_ID_RE.fullmatch(scenario)
        ):
            raise ProvenanceError(f"capture case_results[{index}] 坐标非法")
        normalized.append({"evidence": str(evidence), "subject": subject, "scenario": scenario})
    return normalized


def _capture_branches(root: Path, *, producer_run_id: str, generation: int) -> list[dict[str, Any]]:
    """capture 类每个 case 是一个执行分支；direct 分支留给 Campaign 级估计。"""

    branches: list[dict[str, Any]] = []
    for case in _capture_cases(root):
        branch: dict[str, Any] = {
            "branch": f"{case['evidence']}/{case['subject']}/{case['scenario']}",
            "kind": SOURCE_MITM if case["evidence"] == "mitm" else SOURCE_DIRECT,
            **case,
            "requests": [],
            "auxiliary_requests": [],
        }
        if case["evidence"] == "direct":
            branch["status"] = "pending_estimate"
            branches.append(branch)
            continue
        directory = root / "mitm" / case["subject"] / case["scenario"]
        paths = [directory / name for name in MITM_HTTP_FILES if (directory / name).is_file()]
        if not paths:
            branch.update(status="unresolved", reason="mitm 分支没有 claude-http.jsonl 或 misc-http.jsonl")
            branches.append(branch)
            continue
        for path in paths:
            resolved = closeout._trusted_file(path, "mitm HTTP 记录", maximum=MAX_JSONL_BYTES)
            digest = closeout._sha256_file(resolved)
            for index, record in _read_jsonl(resolved, "mitm HTTP 记录"):
                request = record.get("request")
                if not isinstance(request, Mapping):
                    continue
                host = _normalized_host(request.get("host"))
                method = str(request.get("method") or "")
                path_value = str(request.get("path") or "").split("?", 1)[0]
                category = endpoint_category(host, method, path_value)
                if category is None:
                    continue
                entry = _record(
                    producer_run_id=producer_run_id,
                    source_kind=SOURCE_MITM,
                    coordinate={
                        "evidence": "mitm",
                        "subject": case["subject"],
                        "scenario": case["scenario"],
                        "file": resolved.name,
                        "record_index": index,
                        "transport": "http",
                        "flow_id": record.get("_flow_id"),
                    },
                    generation=generation,
                    source_file=resolved,
                    source_sha256=digest,
                    record_offset=index,
                    host=host,
                    method=method,
                    path=path_value,
                    category=category,
                    model=_mitm_body_model(request.get("body")) if category == "model" else None,
                )
                branch["requests" if category == "model" else "auxiliary_requests"].append(entry)
        branch.update(status="resolved", authority_source=SOURCE_MITM)
        branches.append(branch)
    return branches


def root_branches(
    root: Path, *, producer_run_id: str, generation: int = 0,
    candidate_restoration_reports: Mapping[str, str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """识别证据根类别并返回其执行分支；一个根只能属于一个类别，都不是时为 ``unsupported``。"""

    kinds: list[str] = []
    manifest_path = root / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        kinds.append("capture")
    if (root / "relay").exists():
        kinds.append("relay")
    if len(kinds) > 1:
        raise ProvenanceError(f"证据根 {root} 同时具备多种权威来源：{kinds}")
    if not kinds:
        nested = _child_relay_run_roots(root, candidate_restoration_reports=candidate_restoration_reports)
        if nested is None:
            return "unsupported", []
        return RELAY_RUNS_KIND, _relay_run_branches(root, nested, producer_run_id=producer_run_id, generation=generation)
    if kinds[0] == "relay":
        return "relay", _relay_branches(root, producer_run_id=producer_run_id, generation=generation)
    return "capture", _capture_branches(root, producer_run_id=producer_run_id, generation=generation)


def _child_relay_run_roots(
    root: Path, *, candidate_restoration_reports: Mapping[str, str] | None = None,
) -> tuple[list[Path], list[str]] | None:
    """Claude Job 证据根的直接子目录里的 FW-E 中继运行根（含 ``relay/``），与不是运行根的条目名。

    一个子运行根都没有时返回 None：整个根仍按 ``unsupported`` 处理（由日志判定是否请求前失败）。子目录名进入
    ``producer_run_id``，必须是安全标识符；子运行根同时具备 capture 来源、或出现符号链接都失败关闭。capture 类子根
    不在这里展开（同根估计按 subject／scenario 匹配，嵌套后会跨子根错配），作为不是运行根的条目处理。
    """

    if root.is_symlink() or not root.is_dir():
        return None
    runs: list[Path] = []
    others: list[str] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            raise ProvenanceError(f"Job 证据根下有符号链接：{child}")
        if child.name == CANDIDATE_RESTORATION_DIRNAME:
            if not _restoration_directory_matches(child, candidate_restoration_reports):
                others.append(child.name)
        elif child.is_dir() and (child / "relay").exists():
            if not SAFE_ID_RE.fullmatch(child.name):
                raise ProvenanceError(f"Job 证据根下的中继运行根名称非法：{child.name!r}")
            if (child / "manifest.json").exists():
                raise ProvenanceError(f"中继运行根 {child} 同时具备 capture 来源")
            runs.append(child)
        else:
            others.append(child.name)
    if not runs:
        return None
    return runs, others


def _restoration_directory_matches(directory: Path, reports: Mapping[str, str] | None) -> bool:
    """只接纳已核验报告的完整文件集合；再读一次摘要，未知文件、子目录和读取间漂移都不能被排除。"""

    if not reports or directory.is_symlink() or not directory.is_dir():
        return False
    try:
        files = list(directory.iterdir())
        return {path.name for path in files} == set(reports) and all(
            _sha256(closeout._read_stable_file(path, "候选恢复报告", maximum=closeout.MAX_JSON_BYTES)) == reports[path.name]
            for path in files
        )
    except (OSError, ValueError, closeout.VC0CloseoutError):
        return False


def _relay_run_branches(
    root: Path,
    nested: tuple[list[Path], list[str]],
    *,
    producer_run_id: str,
    generation: int,
) -> list[dict[str, Any]]:
    """逐 probe 展开 Job 证据根：每个子运行根一个 relay 分支（带 ``run_root`` 与它自己的 ``producer_run_id``）。"""

    runs, others = nested
    branches: list[dict[str, Any]] = []
    for child in runs:
        child_run_id = f"{producer_run_id}/{child.name}"
        for branch in _relay_branches(child, producer_run_id=child_run_id, generation=generation):
            branches.append({**branch, "run_root": child.name, "producer_run_id": child_run_id})
    if others:
        branches.append(
            {
                "branch": "relay",
                "kind": SOURCE_RELAY,
                "evidence": "relay",
                "subject": "relay",
                "scenario": "",
                "production_forwarding_enabled": None,
                "requests": [],
                "auxiliary_requests": [],
                "zero_client_byte_connections": [],
                "run_root": None,
                "producer_run_id": producer_run_id,
                "status": "unresolved",
                "reason": f"Job 证据根 {root.name} 下的条目不是中继运行根：{others}",
            }
        )
    return branches


def _producer_run_ids(roots: list[Path], base: Path) -> dict[Path, str]:
    """证据根 → ``producer_run_id``（相对 ``base`` 的 POSIX 路径）；不同的根得到同一个值即失败关闭。"""

    result: dict[Path, str] = {}
    owners: dict[str, Path] = {}
    for root in roots:
        try:
            run_id = root.relative_to(base).as_posix()
        except ValueError as error:
            raise ProvenanceError(f"证据根 {root} 不在 {base} 之内") from error
        if run_id in {"", "."}:
            raise ProvenanceError(f"证据根 {root} 与相对基准目录相同，无法生成 producer_run_id")
        if owners.setdefault(run_id, root) != root:
            raise ProvenanceError(f"证据根 {owners[run_id]} 与 {root} 的 producer_run_id 相同，身份键会撞键")
        result[root] = run_id
    return result


def _apply_estimation(roots: list[tuple[Path, str, list[dict[str, Any]]]], *, estimation_policy: str) -> None:
    """为 direct 分支求上界估计：只有同一证据根内同 subject／scenario 的 mitm 精确数可用。"""

    for _root, _kind, branches in roots:
        precise = {
            (branch["subject"], branch["scenario"]): (len(branch["requests"]), branch["branch"])
            for branch in branches
            if branch["status"] == "resolved" and branch.get("evidence") == "mitm"
        }
        for branch in branches:
            if branch["status"] != "pending_estimate":
                continue
            sibling = precise.get((branch["subject"], branch["scenario"]))
            if estimation_policy == "none":
                branch.update(status="unresolved", reason="direct 分支无法解析请求，估计政策为 none")
            elif sibling is not None:
                branch.update(
                    status="estimated",
                    estimation="upper_bound_from_sibling",
                    estimated_count=sibling[0],
                    estimation_basis=sibling[1],
                )
            else:
                branch.update(
                    status="unresolved",
                    reason="direct 分支无法解析请求，且没有同根同场景 mitm 分支；Claude 没有逐 turn 完成事件，倍率估计不可用",
                )


def _superseded_generation(root: Path) -> int:
    """逻辑根当前目录的世代号：同级 ``<根名>.superseded-…`` 归档的个数（首代为 0）。"""

    if FAILED_ATTEMPT_ROOT_RE.fullmatch(root.name) or SUPERSEDED_ROOT_INFIX in root.name:
        return 0
    parent = root.parent
    prefix = f"{root.name}{SUPERSEDED_ROOT_INFIX}"
    return sum(1 for path in parent.iterdir() if path.name.startswith(prefix)) if parent.is_dir() else 0


def collect_root_provenance(roots: list[Path], *, estimation_policy: str = "none") -> dict[str, Any]:
    """核算一组证据根（不经 Campaign）：录制夹具验收与人工复核用，口径与 Campaign 核算相同。"""

    if estimation_policy not in ESTIMATION_POLICIES:
        raise ProvenanceError(f"估计政策非法：{estimation_policy!r}")
    resolved_roots: list[Path] = []
    for root in roots:
        resolved = closeout._trusted_directory(root, "证据根")
        if resolved not in resolved_roots:
            resolved_roots.append(resolved)
    if not resolved_roots:
        raise ProvenanceError("没有给出证据根")
    run_ids = _producer_run_ids(resolved_roots, Path(os.path.commonpath([str(root.parent) for root in resolved_roots])))
    collected: list[tuple[Path, str, list[dict[str, Any]]]] = []
    for resolved in resolved_roots:
        kind, branches = root_branches(
            resolved, producer_run_id=run_ids[resolved], generation=_superseded_generation(resolved)
        )
        collected.append((resolved, kind, branches))
    _apply_estimation(collected, estimation_policy=estimation_policy)
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "client": CLIENT,
        "counting_rule": COUNTING_RULE,
        "estimation_policy": estimation_policy,
        "roots": [],
        "model_requests_by_endpoint": {},
        "auxiliary_by_category": {},
        "unresolved_roots": [],
    }
    keys: set[str] = set()
    for root, kind, branches in collected:
        statuses = sorted({str(branch["status"]) for branch in branches}) or ["unsupported"]
        zero_bytes = Counter(
            str(item["classification"]) for branch in branches for item in branch.get("zero_client_byte_connections", [])
        )
        summary["roots"].append(
            {
                "root": str(root),
                "producer_run_id": run_ids[root],
                "kind": kind,
                "branch_statuses": statuses,
                "zero_client_byte_connections": dict(sorted(zero_bytes.items())),
            }
        )
        if kind == "unsupported" or "unresolved" in statuses:
            summary["unresolved_roots"].append(str(root))
        for branch in branches:
            for request in branch["requests"]:
                if request["identity_key"] in keys:
                    continue
                keys.add(request["identity_key"])
                counts = summary["model_requests_by_endpoint"]
                counts[request["endpoint"]] = counts.get(request["endpoint"], 0) + 1
            for request in branch["auxiliary_requests"]:
                counts = summary["auxiliary_by_category"]
                counts[request["category"]] = counts.get(request["category"], 0) + 1
    summary["precise_total"] = len(keys)
    summary["estimated_total"] = sum(
        int(branch.get("estimated_count", 0)) for _root, _kind, branches in collected for branch in branches
        if branch["status"] == "estimated"
    )
    return summary


# ---------------------------------------------------------------------------
# Campaign 级核算
# ---------------------------------------------------------------------------


def _attempt_directories(campaign_dir: Path, phase: str) -> list[Path]:
    """返回指定阶段已经发布 reservation 的 attempt 目录（含已发布段预约的恢复段目录）。"""

    if phase == "official":
        attempt_roots = [campaign_dir / "official" / "attempts"]
    else:
        candidates_root = campaign_dir / "candidates"
        if not candidates_root.exists():
            return []
        if candidates_root.is_symlink() or not candidates_root.is_dir():
            raise ProvenanceError("Formal Candidate 根不可信")
        attempt_roots = []
        for candidate in candidates_root.iterdir():
            if candidate.is_symlink() or not candidate.is_dir() or not SAFE_ID_RE.fullmatch(candidate.name):
                raise ProvenanceError("Formal Candidate 目录不可信或 ID 非法")
            attempt_roots.append(candidate / "attempts")
    attempts: list[Path] = []
    for root in attempt_roots:
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise ProvenanceError(f"Formal {phase} attempts 根不可信")
        for attempt in root.iterdir():
            if attempt.is_symlink() or not attempt.is_dir() or not SAFE_ID_RE.fullmatch(attempt.name):
                raise ProvenanceError(f"Formal {phase} attempt 不可信或 ID 非法")
            if (attempt / "reservation.json").is_file():
                attempts.append(attempt.resolve(strict=True))
            recovery_root = attempt / "recovery"
            if not recovery_root.exists():
                continue
            if recovery_root.is_symlink() or not recovery_root.is_dir():
                raise ProvenanceError(f"Formal {phase} attempt 恢复段根不可信")
            for segment in recovery_root.iterdir():
                if segment.is_symlink() or not segment.is_dir() or not RECOVERY_REVISION_RE.fullmatch(segment.name):
                    raise ProvenanceError(f"Formal {phase} attempt 恢复段不可信或编号非法")
                if (segment / "recovery-reservation.json").is_file():
                    attempts.append(segment.resolve(strict=True))
    return sorted(attempts)


def _all_attempt_roots(base: Path) -> list[Path]:
    """同一逻辑运行根的当前目录与全部失败重试归档（``<根名>.failed-attemptN[-k]``）。"""

    match = FAILED_ATTEMPT_ROOT_RE.fullmatch(base.name)
    logical = base.with_name(match.group("base")) if match is not None else base
    candidates: list[tuple[int, int, Path]] = []
    if logical.exists() or logical.is_symlink():
        candidates.append((0, 0, logical))
    if logical.parent.is_dir() and not logical.parent.is_symlink():
        for path in logical.parent.iterdir():
            archive = FAILED_ATTEMPT_ROOT_RE.fullmatch(path.name)
            if archive is not None and archive.group("base") == logical.name:
                candidates.append((int(archive.group("attempt")), int(archive.group("collision") or 0), path))
    roots: list[Path] = []
    for _attempt, _collision, path in sorted(candidates):
        if path.is_symlink() or not path.is_dir():
            raise ProvenanceError(f"遇到不可信 evidence root：{path}")
        resolved = path.resolve(strict=True)
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _map_root(value: Any, *, configuration: Mapping[str, Any], host_data_root: Path) -> Path:
    candidate = Path(str(value))
    if candidate.is_absolute() and (candidate == host_data_root or host_data_root in candidate.parents):
        return candidate
    capture_root = Path(str(configuration.get("capture_root", "")))
    if not capture_root.is_absolute() or capture_root == Path("/"):
        raise ProvenanceError(f"证据根 {value} 不在宿主数据根内，且 Campaign 没有冻结 CAPTURE_ROOT")
    return closeout._map_container_evidence_root(value, capture_root=capture_root, host_data_root=host_data_root)


def _candidate_restoration_bindings(
    campaign_dir: Path, attempt: Path, payload: Mapping[str, Any], *,
    configuration: Mapping[str, Any], host_data_root: Path,
) -> tuple[Path, dict[str, str]] | None:
    """从候选 Job 收据重新验证恢复报告；缺少任何绑定只是不授予目录例外，账务继续保留未决分支。

    报告绑定只能用于收据明确登记的那个根，不能传播到同级失败归档。候选身份取收据所在路径，运行身份按
    Campaign／候选／probe 的既有展开合同复核；执行步骤、报告路径和报告内容三者必须一致。
    """

    def mapped(value: Any) -> Path:
        if not isinstance(value, str) or ".." in Path(value).parts:
            raise ProvenanceError("候选恢复报告路径不规范")
        return _map_root(value, configuration=configuration, host_data_root=host_data_root)

    def argument(argv: list[str], name: str) -> str:
        if argv.count(name) != 1 or argv.index(name) + 1 >= len(argv):
            raise ProvenanceError("候选执行步骤缺少唯一参数")
        return argv[argv.index(name) + 1]

    try:
        reused_from = payload.get("reused_from")
        producer = reused_from.get("producer") if isinstance(reused_from, Mapping) else None
        if isinstance(producer, Mapping) and "campaign" in producer:
            if payload.get("disposition") != "reused":
                return None
            campaign_dir, attempt, configuration = _restoration_producer_context(
                campaign_dir, payload, producer, configuration=configuration, host_data_root=host_data_root
            )
        parts = attempt.relative_to(campaign_dir).parts
        if len(parts) not in {4, 6} or parts[0] != "candidates" or parts[2] != "attempts":
            return None
        candidate_id = parts[1]
        if not SAFE_ID_RE.fullmatch(candidate_id) or payload.get("phase") != "candidate" or payload.get("mode") != "formal":
            return None
        if any(not re.fullmatch(r"[1-9][0-9]{0,9}", str(configuration.get(name, "")))
               for name in ("claude_account_id", "api_key_id")):
            return None
        roots, steps, checks = payload.get("evidence_roots"), payload.get("steps"), payload.get("restoration_checks")
        if not isinstance(roots, list) or len(roots) != 1 or not isinstance(steps, list) or not isinstance(checks, list):
            return None
        root = mapped(roots[0])
        run_prefix = f"{campaign_dir.name}-{candidate_id}"
        logical_name = root.name.split(SUPERSEDED_ROOT_INFIX, 1)[0]
        archive = FAILED_ATTEMPT_ROOT_RE.fullmatch(logical_name)
        if archive is not None:
            logical_name = archive.group("base")
        if root.parent != host_data_root / "runs" / "claude-candidate" / run_prefix or logical_name != payload.get("id"):
            return None
        directory = root / CANDIDATE_RESTORATION_DIRNAME
        if directory.is_symlink() or not directory.is_dir() or root.resolve(strict=True) != root:
            return None
        executed = {}
        for step in steps:
            if not isinstance(step, Mapping):
                return None
            if not step.get("argv"):
                continue
            probe = step.get("probe_id")
            if not isinstance(probe, str) or not SAFE_ID_RE.fullmatch(probe) or probe in executed:
                return None
            executed[probe] = step
        if len(checks) != len(executed) or {row.get("probe_id") for row in checks if isinstance(row, Mapping)} != set(executed):
            return None
        reports: dict[str, str] = {}
        for row in checks:
            probe = row["probe_id"]
            step = executed[probe]
            if (row.get("status") == "not_started" and row.get("report") is None
                    and type(step.get("return_code")) is int and step["return_code"] in {1, 2}):
                continue
            binding, argv, code = row.get("report"), step.get("argv"), step.get("return_code")
            if row.get("status") != "passed" or not isinstance(binding, Mapping) or not isinstance(argv, list):
                return None
            if not all(isinstance(value, str) for value in argv) or type(code) is not int or code == 97:
                return None
            scripts = [index for index, value in enumerate(argv) if Path(value).name == "claude_fw_e_relay.py"]
            if len(scripts) != 1 or argv[scripts[0] + 1:scripts[0] + 2] != ["capture"]:
                return None
            relay_probe = argument(argv, "--probe-id")
            if probe != relay_probe and probe != f"{argument(argv, '--model')}-{relay_probe}":
                return None
            run_id = f"{run_prefix}-{probe}"
            path = directory / f"{probe}.json"
            if (mapped(binding.get("path")) != path or mapped(argument(argv, "--output-root")) != root / probe
                    or argument(argv, "--run-id") != run_id):
                return None
            raw = closeout._read_stable_file(path, "候选恢复报告", maximum=closeout.MAX_JSON_BYTES)
            report = json.loads(raw)
            if not isinstance(report, Mapping) or _sha256(raw) != binding.get("sha256"):
                return None
            wanted = {
                "schema_version": CANDIDATE_RESTORATION_SCHEMA, "run_id": run_id, "probe_id": probe,
                "status": "complete" if code == 0 else "capture_failed", "capture_exit_code": code,
                "account_id": int(configuration["claude_account_id"]), "api_key_id": int(configuration["api_key_id"]),
                **{name: True for name in CANDIDATE_RESTORATION_EQUAL_FIELDS},
            }
            if any(type(report.get(name)) is not type(value) or report.get(name) != value for name, value in wanted.items()):
                return None
            reports[path.name] = str(binding["sha256"])
        return (root, reports) if reports else None
    except (OSError, ValueError, TypeError, KeyError, closeout.VC0CloseoutError, evidence_permissions.EvidencePermissionError):
        return None


def _restoration_producer_context(
    campaign_dir: Path, payload: Mapping[str, Any], producer: Mapping[str, Any], *,
    configuration: Mapping[str, Any], host_data_root: Path,
) -> tuple[Path, Path, Mapping[str, Any]]:
    """后继候选沿原生产者读取：总计划摘要、候选坐标、闭环摘要及权限收口全部通过才采用来源坐标。

    这里只复核请求核算需要的不可变来源，复用许可仍由正式复用入口验证。不能仅凭 producer 自述的 Campaign ID
    改写当前上下文，也不能把一个有效闭环用于它没有收口的外部证据根。
    """

    origin, binding = producer.get("campaign"), producer.get("job_closure")
    if not isinstance(origin, Mapping) or not isinstance(binding, Mapping):
        raise ProvenanceError("候选原生产者缺少总计划或闭环绑定")
    for name, value in (("campaign_id", origin.get("campaign_id")), ("candidate_id", origin.get("candidate_id")),
                        ("attempt_id", producer.get("attempt_id"))):
        if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
            raise ProvenanceError(f"候选原生产者 {name} 非法")
    source = Path(str(origin.get("campaign_dir", "")))
    if source != campaign_dir.parent / str(origin["campaign_id"]) or source.resolve(strict=True) != source:
        raise ProvenanceError("候选原生产者不在同一宿主 Campaign 根")
    manifest, raw = closeout._load_json(source / "campaign.json", "候选原生产者总计划")
    digest = closeout._read_stable_file(source / "campaign.sha256", "候选原生产者总计划摘要", maximum=128)
    source_configuration = manifest.get("configuration")
    if (manifest.get("schema_version") != CAMPAIGN_SCHEMA or manifest.get("client") != CLIENT
            or manifest.get("campaign_mode") != "formal" or manifest.get("campaign_id") != origin["campaign_id"]
            or _sha256(raw) != origin.get("campaign_manifest_sha256") or digest.decode("ascii").strip() != _sha256(raw)
            or not isinstance(source_configuration, Mapping)
            or source_configuration.get("capture_root") != configuration.get("capture_root")):
        raise ProvenanceError("候选原生产者总计划身份或摘要漂移")
    source_jobs = manifest.get("jobs")
    if not isinstance(source_jobs, list) or not any(
        isinstance(row, Mapping) and row.get("id") == payload.get("id") and row.get("phase") == "candidate"
        for row in source_jobs
    ):
        raise ProvenanceError("候选原生产者总计划未冻结该候选 Job")
    attempt = source / "candidates" / str(origin["candidate_id"]) / "attempts" / str(producer["attempt_id"])
    job_root = attempt / "evidence" / "jobs" / str(payload["id"])
    closure_path = job_root / "job-closure.json"
    if binding.get("path") != closure_path.relative_to(source).as_posix() or closure_path.resolve(strict=True) != closure_path:
        raise ProvenanceError("候选原生产者闭环路径不一致")
    closure, closure_raw = closeout._load_json(closure_path, "候选原生产者 Job 闭环")
    wanted = {
        "schema_version": "claude-upgrade-job-closure/v1", "client": CLIENT, "mode": "formal",
        "campaign_id": origin["campaign_id"], "attempt_id": producer["attempt_id"], "job_id": payload["id"],
        "execution_sha256": producer.get("execution_sha256"), "status": "complete", "environment_contaminated": False,
        "evidence_roots": payload.get("evidence_roots"),
    }
    if (_sha256(closure_raw) != binding.get("sha256")
            or not re.fullmatch(r"[0-9a-f]{64}", str(producer.get("execution_sha256", "")))
            or any(type(closure.get(name)) is not type(value) or closure.get(name) != value for name, value in wanted.items())):
        raise ProvenanceError("候选原生产者闭环身份、范围或摘要不一致")
    _verify_restoration_receipt_chain(campaign_dir, payload, attempt / f"job-{payload['id']}.json", producer)
    permission = closure.get("evidence_permission_closeout")
    filename = evidence_permissions.RECEIPT_FILENAME
    if not isinstance(permission, Mapping) or permission.get("path") != (job_root / filename).relative_to(attempt).as_posix():
        raise ProvenanceError("候选原生产者权限收口绑定非法")
    external = closure.get("host_evidence_roots")
    if not isinstance(external, list) or any(not isinstance(value, str) for value in external):
        raise ProvenanceError("候选原生产者外部证据根非法")
    mapped_roots = [_map_root(value, configuration=source_configuration, host_data_root=host_data_root)
                    for value in payload.get("evidence_roots") or []]
    if not mapped_roots or not set(mapped_roots).issubset({Path(value) for value in external}):
        raise ProvenanceError("候选原生产者没有收口当前证据根")
    evidence_permissions.replay_evidence_permission_closeout(
        job_root, [job_root / "evidence", job_root / "logs", *(Path(value) for value in external)],
        {"path": filename, "sha256": permission.get("sha256"), "bytes": permission.get("bytes")},
        managed_data_root=host_data_root, logical_runs_roots=(host_data_root / "runs",),
    )
    return source, attempt, source_configuration


def _verify_restoration_receipt_chain(
    campaign_dir: Path, payload: Mapping[str, Any], expected: Path, producer: Mapping[str, Any],
) -> None:
    """逐跳重放 Job 收据摘要，直到原生产者；每跳取证步骤与报告绑定都必须原样保留，禁止自造 producer 旁路。"""

    current, directory = payload, campaign_dir
    seen: set[Path] = set()
    while current.get("disposition") == "reused":
        reused = current.get("reused_from")
        if not isinstance(reused, Mapping) or not isinstance(reused.get("job_receipt"), Mapping):
            raise ProvenanceError("候选复用链缺少来源 Job 收据")
        origin = reused.get("campaign")
        if origin is not None:
            if not isinstance(origin, Mapping):
                raise ProvenanceError("候选复用链来源 Campaign 非法")
            directory = Path(str(origin.get("campaign_dir", "")))
            if directory != campaign_dir.parent / str(origin.get("campaign_id", "")) or directory.resolve(strict=True) != directory:
                raise ProvenanceError("候选复用链来源越过同一宿主 Campaign 根")
            manifest_raw = closeout._read_stable_file(directory / "campaign.json", "候选复用链总计划", maximum=closeout.MAX_JSON_BYTES)
            if _sha256(manifest_raw) != origin.get("campaign_manifest_sha256"):
                raise ProvenanceError("候选复用链总计划摘要漂移")
        binding = reused["job_receipt"]
        relative = Path(str(binding.get("path", "")))
        path = directory / relative
        if (relative.is_absolute() or ".." in relative.parts or path.name != f"job-{payload['id']}.json"
                or path.resolve(strict=True) != path or path in seen or len(seen) >= 128):
            raise ProvenanceError("候选复用链 Job 收据路径非法或成环")
        seen.add(path)
        previous, raw = closeout._load_json(path, "候选复用链 Job 收据")
        if (_sha256(raw) != binding.get("sha256") or previous.get("id") != payload["id"]
                or previous.get("phase") != "candidate" or previous.get("mode") != "formal" or previous.get("status") != "complete"
                or any(previous.get(name) != current.get(name) for name in ("steps", "restoration_checks", "evidence_roots"))):
            raise ProvenanceError("候选复用链取证步骤、恢复绑定或收据摘要漂移")
        current = previous
    closure = current.get("job_closure")
    if (not seen or path != expected or current.get("disposition") != "executed"
            or current.get("execution_sha256") != producer.get("execution_sha256")
            or not isinstance(closure, Mapping)
            or closure.get("path") != "evidence/jobs/" + str(payload["id"]) + "/job-closure.json"
            or closure.get("sha256") != producer["job_closure"].get("sha256")):
        raise ProvenanceError("候选复用链未闭合到原生产者 Job")


def _attempt_job_facts(
    campaign_dir: Path,
    *,
    configuration: Mapping[str, Any],
    host_data_root: Path,
    job_phases: Mapping[str, str],
) -> tuple[dict[str, list[Path]], dict[str, list[Path]], dict[str, bool], dict[Path, dict[str, str]]]:
    """从不可变 Job 收据读取实际证据根与日志，不用 Campaign 里的模板路径。"""

    roots_by_job: dict[str, list[Path]] = {}
    logs_by_job: dict[str, list[Path]] = {}
    attempts_present = {"official": False, "candidate": False}
    restoration_reports: dict[Path, dict[str, str]] = {}
    for phase in ("official", "candidate"):
        attempts = _attempt_directories(campaign_dir, phase)
        attempts_present[phase] = bool(attempts)
        for attempt in attempts:
            logs_root = attempt / "logs"
            if logs_root.exists():
                if logs_root.is_symlink() or not logs_root.is_dir():
                    raise ProvenanceError(f"Formal {phase} attempt logs 根不可信")
                for job_id, job_phase in job_phases.items():
                    if job_phase != phase:
                        continue
                    pattern = re.compile(re.escape(job_id) + r"(?:-retry[0-9]+)?-[0-9]+\.log")
                    for path in logs_root.iterdir():
                        if pattern.fullmatch(path.name):
                            logs_by_job.setdefault(job_id, []).append(
                                closeout._trusted_file(path, f"Formal {phase} Job 日志", allow_empty=True)
                            )
            for job_path in sorted(attempt.glob("job-*.json")):
                if job_path.is_symlink() or not job_path.is_file():
                    raise ProvenanceError(f"Formal {phase} Job 收据不可信")
                payload, _raw = closeout._load_json(job_path, f"Formal {phase} Job 收据")
                job_id = payload.get("id")
                if (
                    not isinstance(job_id, str)
                    or not SAFE_ID_RE.fullmatch(job_id)
                    or job_path.name != f"job-{job_id}.json"
                    or job_phases.get(job_id) != phase
                ):
                    raise ProvenanceError(f"Formal {phase} Job 收据身份不一致")
                evidence_roots = payload.get("evidence_roots")
                if not isinstance(evidence_roots, list) or any(not isinstance(value, str) for value in evidence_roots):
                    raise ProvenanceError(f"{job_id} Job 收据 evidence_roots 非字符串数组")
                resolved_roots = roots_by_job.setdefault(job_id, [])
                for value in evidence_roots:
                    mapped = _map_root(value, configuration=configuration, host_data_root=host_data_root)
                    for root in _all_attempt_roots(mapped):
                        if root not in resolved_roots:
                            resolved_roots.append(root)
                if phase == "candidate":
                    binding = _candidate_restoration_bindings(
                        campaign_dir, attempt, payload, configuration=configuration, host_data_root=host_data_root
                    )
                    if binding is not None:
                        restoration_reports[binding[0]] = binding[1]
    return roots_by_job, logs_by_job, attempts_present, restoration_reports


def _undeclared_attempt_jobs(campaign_dir: Path) -> list[dict[str, Any]]:
    """总计划没有冻结 Job 时，两个阶段的 attempt（含恢复段）里登记了证据根的 Job 收据，按 (阶段, Job) 合并。"""

    found: dict[tuple[str, str], dict[str, Any]] = {}
    for phase in ("official", "candidate"):
        for attempt in _attempt_directories(campaign_dir, phase):
            for job_path in sorted(attempt.glob("job-*.json")):
                if job_path.is_symlink() or not job_path.is_file():
                    raise ProvenanceError(f"Formal {phase} Job 收据不可信")
                payload, _raw = closeout._load_json(job_path, f"Formal {phase} Job 收据")
                job_id = payload.get("id")
                if not isinstance(job_id, str) or not SAFE_ID_RE.fullmatch(job_id) or job_path.name != f"job-{job_id}.json":
                    raise ProvenanceError(f"Formal {phase} Job 收据身份不一致")
                evidence_roots = payload.get("evidence_roots")
                if not isinstance(evidence_roots, list) or any(not isinstance(value, str) for value in evidence_roots):
                    raise ProvenanceError(f"{job_id} Job 收据 evidence_roots 非字符串数组")
                if not evidence_roots:
                    continue
                entry = found.setdefault((phase, job_id), {"phase": phase, "job_id": job_id, "attempts": []})
                entry["attempts"].append(attempt.relative_to(campaign_dir).as_posix())
    return [found[key] for key in sorted(found)]


def collect_campaign_provenance(
    formal_campaign_dir: Path,
    *,
    formal_campaign_id: str,
    estimation_policy: str = "none",
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """按统一计量单位核算一个 Claude Formal Campaign 的全部正式请求；接口与 Codex 同名同参。"""

    if estimation_policy not in ESTIMATION_POLICIES:
        raise ProvenanceError(f"估计政策非法：{estimation_policy!r}")
    campaign_dir = closeout._trusted_directory(formal_campaign_dir, "Formal Campaign")
    manifest, raw = closeout._load_json(campaign_dir / "campaign.json", "Formal Campaign")
    if (
        manifest.get("schema_version") != CAMPAIGN_SCHEMA
        or manifest.get("client") != CLIENT
        or manifest.get("campaign_id") != formal_campaign_id
        or manifest.get("campaign_mode") != "formal"
    ):
        raise ProvenanceError("provenance 的 Claude Formal Campaign 身份不一致")
    digest_path = closeout._trusted_file(campaign_dir / "campaign.sha256", "Campaign 摘要")
    if digest_path.read_text(encoding="ascii").strip() != _sha256(raw):
        raise ProvenanceError("provenance 发现 Campaign 摘要漂移")
    jobs = manifest.get("jobs", [])
    if not isinstance(jobs, list):
        raise ProvenanceError("Formal Campaign jobs 非数组")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "client": CLIENT,
        "campaign_id": formal_campaign_id,
        "observed_at_utc": observed_at_utc or _utc_now(),
        "counting_rule": COUNTING_RULE,
        "counting_unit": COUNTING_UNIT,
        "model_endpoints": sorted(MODEL_ENDPOINTS),
        "estimation_policy": estimation_policy,
        "jobs": [],
        "requests": [],
        "auxiliary_requests": [],
        "precise_total": 0,
        "estimated_total": 0,
        "auxiliary_total": 0,
        "unresolved_job_ids": [],
        "pending_job_ids": [],
        "pre_request_zero_job_ids": [],
    }
    if not jobs:
        # 总计划没有冻结 Job 却有 attempt 登记了证据根：无法按 Job 核算，失败关闭；两者都没有才是零请求的 complete。
        for item in _undeclared_attempt_jobs(campaign_dir):
            receipt["jobs"].append(
                {
                    "job_id": item["job_id"],
                    "phase": item["phase"],
                    "roots": [],
                    "attempts": item["attempts"],
                    "precise_count": 0,
                    "estimated_count": 0,
                    "auxiliary_count": 0,
                    "status": "unresolved",
                    "reason": "总计划没有冻结任何 Job，attempt 却登记了这个 Job 的证据根",
                }
            )
            receipt["unresolved_job_ids"].append(item["job_id"])
        receipt["status"] = "accounting_unresolved" if receipt["unresolved_job_ids"] else "complete"
        receipt["identity_keys_sha256"] = _sha256(_canonical([]))
        return receipt
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ProvenanceError("Formal Campaign 缺少 configuration")
    host_data_root = closeout._formal_host_data_root(campaign_dir)
    job_phases: dict[str, str] = {}
    for item in jobs:
        job_id = item.get("id") if isinstance(item, Mapping) else None
        phase = item.get("phase") if isinstance(item, Mapping) else None
        if (
            not isinstance(job_id, str)
            or not SAFE_ID_RE.fullmatch(job_id)
            or phase not in {"official", "candidate"}
            or job_id in job_phases
        ):
            raise ProvenanceError("Formal Campaign Job 身份、阶段或唯一性非法")
        job_phases[job_id] = str(phase)
    actual_roots, attempt_logs, attempts_present, restoration_reports = _attempt_job_facts(
        campaign_dir, configuration=configuration, host_data_root=host_data_root, job_phases=job_phases
    )

    # 第一遍：发现每个 Job 的证据根与执行分支；估计在全部根发现后统一做。
    job_plans: list[dict[str, Any]] = []
    root_cache: dict[Path, tuple[str, list[dict[str, Any]], int]] = {}
    for item in jobs:
        job_id, phase = str(item["id"]), str(item["phase"])
        if phase == "candidate" and not attempts_present["candidate"]:
            # 尚未进入 Candidate 阶段时不把未来计划误记为未决账务。
            continue
        if attempts_present[phase]:
            roots = list(actual_roots.get(job_id, []))
            logs = list(attempt_logs.get(job_id, []))
            # B2-08：被强杀的 attempt 没有正在执行那个 Job 的收据，它写到一半的证据只在冻结的证据根路径下；路径存在而没有任何
            # 收据登记时一并核算（中断前已经发出的请求不能因为缺收据而漏计，续跑取代归档之后也不再重算它）。
            templates = item.get("evidence_roots")
            if isinstance(templates, list):
                for value in templates:
                    for root in _all_attempt_roots(_map_root(value, configuration=configuration, host_data_root=host_data_root)):
                        if root not in roots:
                            roots.append(root)
        else:
            templates = item.get("evidence_roots")
            if not isinstance(templates, list):
                raise ProvenanceError(f"{job_id} evidence_roots 非数组")
            roots = []
            for value in templates:
                roots.extend(_all_attempt_roots(_map_root(value, configuration=configuration, host_data_root=host_data_root)))
            logs = closeout._job_logs(campaign_dir, job_id)
        for root in roots:
            if root not in root_cache:
                root_cache[root] = ("pending", [], _superseded_generation(root))
        job_plans.append({"job_id": job_id, "phase": phase, "roots": roots, "logs": logs})
    run_ids = _producer_run_ids(list(root_cache), host_data_root)
    official_roots = {root for plan in job_plans if plan["phase"] == "official" for root in plan["roots"]}
    for root, (_kind, _branches, generation) in list(root_cache.items()):
        kind, branches = root_branches(
            root, producer_run_id=run_ids[root], generation=generation,
            candidate_restoration_reports=None if root in official_roots else restoration_reports.get(root),
        )
        root_cache[root] = (kind, branches, generation)
    _apply_estimation(
        [(root, kind, branches) for root, (kind, branches, _generation) in root_cache.items() if kind != "unsupported"],
        estimation_policy=estimation_policy,
    )

    # 第二遍：按 Job 汇总，同一证据根首次归属的 Job 拥有其请求与估计。
    seen_keys: set[str] = set()
    seen_auxiliary: set[str] = set()
    seen_roots: dict[Path, str] = {}
    for plan in job_plans:
        job_id = plan["job_id"]
        entry: dict[str, Any] = {
            "job_id": job_id,
            "phase": plan["phase"],
            "roots": [],
            "precise_count": 0,
            "estimated_count": 0,
            "auxiliary_count": 0,
        }
        if not plan["roots"] and not plan["logs"]:
            entry["status"] = "pending"
            receipt["pending_job_ids"].append(job_id)
            receipt["jobs"].append(entry)
            continue
        supported = unresolved = estimated = False
        for root in plan["roots"]:
            kind, branches, generation = root_cache[root]
            if kind == "unsupported":
                continue
            supported = True
            owner = seen_roots.setdefault(root, job_id)
            root_entry: dict[str, Any] = {
                "root": str(root),
                "producer_run_id": run_ids[root],
                "kind": kind,
                "first_owner_job_id": owner,
                "branches": [],
            }
            if generation:
                # 估计来源按世代区分（对账器 _estimated_source_id）：Claude 没有旧口径来源，一律视为新来源。
                root_entry["supersession"] = {"generation": generation, "prior_estimate_branches": True}
            for branch in branches:
                slim = {key: value for key, value in branch.items() if key not in {"requests", "auxiliary_requests"}}
                slim["request_count"] = len(branch["requests"])
                slim["auxiliary_count"] = len(branch["auxiliary_requests"])
                root_entry["branches"].append(slim)
                if branch["status"] == "unresolved":
                    unresolved = True
                elif branch["status"] == "estimated":
                    estimated = True
                    if owner == job_id:
                        entry["estimated_count"] += int(branch["estimated_count"])
                for request in branch["requests"]:
                    if request["identity_key"] not in seen_keys:
                        seen_keys.add(request["identity_key"])
                        receipt["requests"].append({**request, "job_id": job_id})
                        entry["precise_count"] += 1
                for request in branch["auxiliary_requests"]:
                    if request["identity_key"] not in seen_auxiliary:
                        seen_auxiliary.add(request["identity_key"])
                        receipt["auxiliary_requests"].append({**request, "job_id": job_id})
                        entry["auxiliary_count"] += 1
            entry["roots"].append(root_entry)
        if not supported:
            if closeout._logs_prove_pre_request_failure(plan["logs"]):
                entry["status"] = "pre_request_zero"
                receipt["pre_request_zero_job_ids"].append(job_id)
            else:
                entry["status"] = "unresolved"
                entry["reason"] = "没有可识别的权威来源且日志不能证明请求前失败"
                receipt["unresolved_job_ids"].append(job_id)
        elif unresolved:
            entry["status"] = "unresolved"
            receipt["unresolved_job_ids"].append(job_id)
        else:
            entry["status"] = "estimated" if estimated else "resolved"
        receipt["jobs"].append(entry)
    receipt["precise_total"] = len(receipt["requests"])
    receipt["estimated_total"] = sum(int(job["estimated_count"]) for job in receipt["jobs"])
    receipt["auxiliary_total"] = len(receipt["auxiliary_requests"])
    receipt["status"] = "accounting_unresolved" if receipt["unresolved_job_ids"] else "complete"
    receipt["identity_keys_sha256"] = _sha256(_canonical(sorted(item["identity_key"] for item in receipt["requests"])))
    return receipt


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    roots = commands.add_parser("roots", help="核算一组证据根（录制夹具验收与人工复核）")
    roots.add_argument("--estimation-policy", choices=ESTIMATION_POLICIES, default="none")
    roots.add_argument("root", type=Path, nargs="+")
    campaign = commands.add_parser("campaign", help="核算一个 Claude Formal Campaign")
    campaign.add_argument("--campaign-dir", type=Path, required=True)
    campaign.add_argument("--campaign-id", required=True)
    campaign.add_argument("--estimation-policy", choices=ESTIMATION_POLICIES, default="none")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "roots":
            result = collect_root_provenance(list(arguments.root), estimation_policy=arguments.estimation_policy)
        else:
            result = collect_campaign_provenance(
                arguments.campaign_dir,
                formal_campaign_id=arguments.campaign_id,
                estimation_policy=arguments.estimation_policy,
            )
    except (ProvenanceError, closeout.VC0CloseoutError, OSError) as error:
        print(f"provenance 失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
