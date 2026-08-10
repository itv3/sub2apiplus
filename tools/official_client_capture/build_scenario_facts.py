#!/usr/bin/env python3
"""SCN-REALITY-01 场景原始事实构建器。

从一次 capture job 的原始证据（relay 明文字节、pcap ClientHello、驱动事件日志）
提取目标协议分支是否真实成立的事实，并写出 `scenario-facts/<场景>-facts.json`。

设计约束（R0 §4.1）：

- **只提取，不推断**。字段值必须能追溯到某个原始证据文件的字节；每个用到的文件
  都进 `evidence_bindings`，由 run 阶段按 job 证据根复核路径、大小与 SHA-256。
- **失败即不产出**。任一必填字段缺失就退出非 0，只写不参与判定的失败诊断。
  编排器据此判 job 失败——「脚本退出 0 且证据目录非空」不再等于场景成立。
- **不含 attempt 身份**。campaign／attempt／run_nonce 是编排侧的权威事实，
  由外层 finalizer 注入，采集侧无从声明。

用法：

    python3 build_scenario_facts.py --scenario A13 \\
        --job-id official-relay-oauth-refresh \\
        --run-id <RUN_ID> --run-root <run 目录>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.pcap_clienthello import (
    iter_timed_packets,
    parse_client_hello,
    tcp_payload,
)
from tools.official_client_capture.scenario_receipts import (
    ScenarioReceiptError,
    SUPPORTED_SCENARIOS,
    build_facts_document,
    write_failure_diagnostic,
)


FACTS_DIR = "scenario-facts"
OBSERVATION_DIR = "scenario-observations"
RELAY_DIR = "relay"
PCAP_RELATIVE = ("direct/traffic.pcap",)
EVIDENCE_ROOT_ROLE = "job_evidence"
# pcap 全局头 24 字节；只有头没有包的文件不构成证据。
MIN_PCAP_BYTES = 25
MAX_EVIDENCE_BYTES = 512 * 1024 * 1024
REGIONAL_SNI_RE = re.compile(r"^[a-z0-9.-]+\.oaiusercontent\.com$")
# 真实观测形态：`Location: /v1/realtime/calls/rtc_u0_EBE4oHU6FYPaFejVfBpPW`。
CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class ScenarioFactsError(ValueError):
    """原始证据不足以证明目标协议分支成立。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class EvidenceSet:
    """收集本次提取真实读过的证据文件，形成可复核的绑定。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._bindings: dict[str, dict[str, Any]] = {}

    def bind(self, path: Path) -> Path:
        if path.is_symlink() or not path.is_file():
            raise ScenarioFactsError(f"证据不是普通文件：{path}")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ScenarioFactsError(f"证据越过 job 证据根：{path}")
        size = path.stat().st_size
        if size <= 0 or size > MAX_EVIDENCE_BYTES:
            raise ScenarioFactsError(f"证据大小不可用：{path}")
        relative = path.relative_to(self.root).as_posix()
        self._bindings[relative] = {
            "root_role": EVIDENCE_ROOT_ROLE,
            "path": relative,
            "sha256": _sha256(path),
            "bytes": size,
        }
        return path

    def bindings(self) -> list[dict[str, Any]]:
        if not self._bindings:
            raise ScenarioFactsError("没有任何证据被读取。")
        return [self._bindings[key] for key in sorted(self._bindings)]


def _load_observation(evidence: EvidenceSet, root: Path, name: str) -> dict[str, Any]:
    """读取采集脚本落下的场景观测事实。"""

    path = root / OBSERVATION_DIR / name
    if path.is_symlink() or not path.is_file():
        raise ScenarioFactsError(f"缺少场景观测记录：{OBSERVATION_DIR}/{name}")
    evidence.bind(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScenarioFactsError(f"场景观测记录不可读：{name}：{error}") from error
    if not isinstance(payload, dict):
        raise ScenarioFactsError(f"场景观测记录顶层必须是对象：{name}")
    return payload


def _require(payload: dict[str, Any], key: str, name: str) -> Any:
    if key not in payload:
        raise ScenarioFactsError(f"{name} 缺少字段 {key}")
    return payload[key]


# --------------------------------------------------------------------------
# HTTP/1.1 明文字节解析
#
# relay 落盘的是中继解密后的明文，可以直接切。这里不复用 relay_extract 的
# parse_h1_stream——那份实现只切请求、且对 body 做脱敏摘要，取不到 call_id 与
# upload_url 的精确值。
# --------------------------------------------------------------------------


def _split_head(payload: bytes, start: int) -> tuple[list[str], int] | None:
    end = payload.find(b"\r\n\r\n", start)
    if end < 0:
        return None
    head = payload[start:end].decode("latin-1", "replace")
    return head.split("\r\n"), end + 4


def _headers_of(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers.setdefault(name.strip().lower(), value.strip())
    return headers


def _body_of(payload: bytes, headers: dict[str, str], start: int) -> tuple[bytes, int]:
    """按 Content-Length 或 chunked 取出报文体，返回 (体, 下一条报文起点)。"""

    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = bytearray()
        cursor = start
        while True:
            line_end = payload.find(b"\r\n", cursor)
            if line_end < 0:
                break
            try:
                size = int(payload[cursor:line_end].split(b";")[0], 16)
            except ValueError:
                break
            cursor = line_end + 2
            if size == 0:
                cursor = payload.find(b"\r\n\r\n", cursor - 2)
                cursor = cursor + 4 if cursor >= 0 else len(payload)
                break
            body.extend(payload[cursor : cursor + size])
            cursor += size + 2
        return bytes(body), cursor
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        length = 0
    return payload[start : start + length], start + length


def _iter_requests(payload: bytes) -> Iterator[dict[str, Any]]:
    cursor = 0
    while cursor < len(payload):
        parsed = _split_head(payload, cursor)
        if parsed is None:
            return
        lines, body_start = parsed
        if not lines or " HTTP/1." not in lines[0]:
            return
        parts = lines[0].split(" ")
        if len(parts) < 3:
            return
        headers = _headers_of(lines)
        body, cursor = _body_of(payload, headers, body_start)
        yield {
            "method": parts[0],
            "target": parts[1],
            "headers": headers,
            "body": body,
        }
        if cursor <= body_start:
            cursor = body_start


def _iter_responses(payload: bytes) -> Iterator[dict[str, Any]]:
    cursor = 0
    while cursor < len(payload):
        parsed = _split_head(payload, cursor)
        if parsed is None:
            return
        lines, body_start = parsed
        if not lines or not lines[0].startswith("HTTP/1."):
            return
        parts = lines[0].split(" ")
        if len(parts) < 2 or not parts[1].isdigit():
            return
        headers = _headers_of(lines)
        body, cursor = _body_of(payload, headers, body_start)
        yield {"status": int(parts[1]), "headers": headers, "body": body}
        if cursor <= body_start:
            cursor = body_start


def _relay_streams(evidence: EvidenceSet, root: Path, direction: str) -> list[bytes]:
    """按连接序读出某个方向的全部明文字节。"""

    relay_root = root / RELAY_DIR
    if not relay_root.is_dir() or relay_root.is_symlink():
        raise ScenarioFactsError("缺少 relay 证据目录。")
    streams: list[bytes] = []
    for path in sorted(relay_root.glob(f"conn*.{direction}.bin")):
        evidence.bind(path)
        streams.append(path.read_bytes())
    if not streams:
        raise ScenarioFactsError(f"relay 未记录任何 {direction} 字节。")
    return streams


def _path_of(target: str) -> str:
    return target.split("?", 1)[0]


def _find_exchange(
    evidence: EvidenceSet,
    root: Path,
    method: str,
    path_predicate,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """在同一条连接上定位请求及其对应响应。

    relay 按连接分文件，同一连接的第 N 个请求对应第 N 个响应，因此可以按序配对。
    """

    relay_root = root / RELAY_DIR
    for request_path in sorted(relay_root.glob("conn*.client_to_upstream.bin")):
        response_path = request_path.with_name(
            request_path.name.replace("client_to_upstream", "upstream_to_client")
        )
        if response_path.is_symlink() or not response_path.is_file():
            continue
        evidence.bind(request_path)
        evidence.bind(response_path)
        requests = list(_iter_requests(request_path.read_bytes()))
        responses = list(_iter_responses(response_path.read_bytes()))
        for index, request in enumerate(requests):
            if request["method"] != method or not path_predicate(
                _path_of(request["target"])
            ):
                continue
            if index >= len(responses):
                raise ScenarioFactsError(
                    f"{method} {_path_of(request['target'])} 没有对应的响应字节。"
                )
            return request, responses[index]
    raise ScenarioFactsError(f"relay 字节中没有 {method} 目标请求。")


# --------------------------------------------------------------------------
# pcap ClientHello
# --------------------------------------------------------------------------


def _client_hellos(evidence: EvidenceSet, root: Path) -> list[tuple[str, float]]:
    """返回 (SNI, 捕获时刻) 列表。缺少可解析的 ClientHello 即失败。"""

    found: list[tuple[str, float]] = []
    seen_pcap = False
    for relative in PCAP_RELATIVE:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size < MIN_PCAP_BYTES:
            raise ScenarioFactsError(f"pcap 只有全局头，没有数据包：{relative}")
        seen_pcap = True
        evidence.bind(path)
        for link, captured_at, data in iter_timed_packets(path):
            segment = tcp_payload(link, data)
            if segment is None:
                continue
            hello = parse_client_hello(segment[2])
            if hello and hello[0]:
                found.append((hello[0], captured_at))
    if not seen_pcap:
        raise ScenarioFactsError("缺少 pcap 证据，无法取得 SNI。")
    if not found:
        raise ScenarioFactsError("pcap 中没有可解析的 ClientHello。")
    return found


def _require_sni(hellos: list[tuple[str, float]], expected: str) -> float:
    times = [when for name, when in hellos if name == expected]
    if not times:
        raise ScenarioFactsError(f"pcap 中没有 {expected} 的 ClientHello。")
    return min(times)


# --------------------------------------------------------------------------
# 逐场景提取
# --------------------------------------------------------------------------


def _facts_a11(evidence: EvidenceSet, root: Path) -> dict[str, Any]:
    request, response = _find_exchange(
        evidence,
        root,
        "POST",
        lambda path: path.endswith("/backend-api/codex/realtime/calls"),
    )
    del request
    if not 200 <= response["status"] <= 299:
        raise ScenarioFactsError(
            f"realtime call-create 返回 {response['status']}，目标分支未成立。"
        )
    # WebRTC 的 call-create 返回 201 + text/plain 的 SDP answer，**响应体不是 JSON**；
    # call_id 由 `Location: /v1/realtime/calls/{call_id}` 给出，随后 V3 sideband 用
    # 同一个 id 走 `/v1/live/{call_id}`。这是 k37 真实采集观测到的形态。
    location = response["headers"].get("location", "")
    call_id = location.rstrip("/").rsplit("/", 1)[-1] if "/" in location else ""
    if not call_id or not CALL_ID_RE.fullmatch(call_id):
        raise ScenarioFactsError(
            f"call-create 响应的 Location 未给出 call_id：{location[:120]!r}"
        )

    events = _load_observation(evidence, root, "A11-realtime-events.json")
    notifications = _require(events, "notifications", "A11 事件日志")
    if not isinstance(notifications, list):
        raise ScenarioFactsError("A11 事件日志 notifications 必须是数组。")
    errors = [item for item in notifications if _method_of(item) == "thread/realtime/error"]
    if errors:
        raise ScenarioFactsError(f"realtime 出现 {len(errors)} 次异步 error。")
    final_event = None
    for item in notifications:
        method = _method_of(item)
        if method == "thread/realtime/started":
            final_event = "thread_realtime_started"
        elif method == "thread/realtime/sdp" and final_event is None:
            final_event = "sdp_answer"
    if final_event is None:
        raise ScenarioFactsError("realtime 没有 started／SDP 最终事件。")
    if not _sideband_joins_call(evidence, root, call_id):
        raise ScenarioFactsError("relay 字节中没有与 call-create 同 call_id 的 sideband 连接。")

    hellos = _client_hellos(evidence, root)
    _require_sni(hellos, "api.openai.com")
    return {
        "call_create_status": response["status"],
        "call_id_sha256": hashlib.sha256(call_id.encode("utf-8")).hexdigest(),
        "sdp_or_started_event": final_event,
        "async_error_count": 0,
        "sideband_sni": "api.openai.com",
        "sideband_call_id_linked": True,
    }


def _method_of(item: Any) -> str:
    return item.get("method", "") if isinstance(item, dict) else ""


def _sideband_joins_call(evidence: EvidenceSet, root: Path, call_id: str) -> bool:
    """在 relay 字节里找出与 call-create 同 call_id 的 sideband WS 升级请求。

    V3（FramelessBidi）把 call_id 拼在**路径末段**（`/v1/live/{call_id}`），
    V1／V2 才用 query `?call_id=`（`codex-api/.../methods.rs:985-993`）。两种形态都
    接受，但都必须是本轮 call-create 返回的那个 call_id——这正是 sideband 与第一跳
    真实关联的证明，而不是驱动自己声称的。
    """

    relay_root = root / RELAY_DIR
    for path in sorted(relay_root.glob("conn*.client_to_upstream.bin")):
        evidence.bind(path)
        for request in _iter_requests(path.read_bytes()):
            if request["headers"].get("upgrade", "").lower() != "websocket":
                continue
            target = request["target"]
            path_part = _path_of(target)
            query = target[len(path_part) + 1 :] if "?" in target else ""
            if path_part.rstrip("/").endswith(f"/{call_id}"):
                return True
            if f"call_id={call_id}" in query:
                return True
    return False


def _facts_a13(evidence: EvidenceSet, root: Path) -> dict[str, Any]:
    request, response = _find_exchange(
        evidence, root, "POST", lambda path: path.endswith("/oauth/token")
    )
    del response
    hellos = _client_hellos(evidence, root)
    _require_sni(hellos, "auth.openai.com")

    observation = _load_observation(evidence, root, "A13-jwt-exp.json")
    restore = _load_observation(evidence, root, "A13-credential-restore.json")
    before = _require(restore, "before_sha256", "A13 凭据记录")
    after = _require(restore, "after_sha256", "A13 凭据记录")
    if _require(restore, "capture_side_wrote_auth", "A13 凭据记录") is not False:
        raise ScenarioFactsError("采集侧写过 auth.json，A13 不接受受控篡改。")
    if before == after:
        raise ScenarioFactsError(
            "auth.json 采集前后一致，CLI 没有真正刷新并落盘。"
        )
    return {
        "token_request_method": request["method"],
        "token_request_path": _path_of(request["target"]),
        "oauth_sni": "auth.openai.com",
        "jwt_exp_observation": {
            "exp_at_utc": _require(observation, "exp_at_utc", "A13 JWT 观测"),
            "observed_at_utc": _require(
                observation, "observed_at_utc", "A13 JWT 观测"
            ),
            "trigger": _require(observation, "trigger", "A13 JWT 观测"),
            "token_sha256": _require(observation, "token_sha256", "A13 JWT 观测"),
        },
        "credential_restore": {
            "before_sha256": before,
            "after_sha256": after,
            "capture_side_wrote_auth": False,
            "rotated_by_refresh": True,
        },
    }


def _facts_a14(evidence: EvidenceSet, root: Path) -> dict[str, Any]:
    create_request, create_response = _find_exchange(
        evidence, root, "POST", lambda path: path == "/backend-api/files"
    )
    del create_request
    if not 200 <= create_response["status"] <= 299:
        raise ScenarioFactsError(
            f"file create 返回 {create_response['status']}，上传链未开始。"
        )
    try:
        document = json.loads(create_response["body"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScenarioFactsError(f"file create 响应体不可解析：{error}") from error
    upload_url = document.get("upload_url") or (document.get("file") or {}).get(
        "upload_url"
    )
    if not isinstance(upload_url, str) or "://" not in upload_url:
        raise ScenarioFactsError("file create 响应没有 upload_url。")
    host = upload_url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    if not REGIONAL_SNI_RE.fullmatch(host):
        raise ScenarioFactsError(f"upload_url 主机不是区域上传主机：{host}")

    uploaded_request, uploaded_response = _find_exchange(
        evidence, root, "POST", lambda path: path.endswith("/uploaded")
    )
    if not 200 <= uploaded_response["status"] <= 299:
        raise ScenarioFactsError(
            f"uploaded 返回 {uploaded_response['status']}，上传链未闭合。"
        )

    hellos = _client_hellos(evidence, root)
    # 区域主机必须由本轮响应派生：预列域名凑出的 SNI 不满足这一条。
    regional_times = [when for name, when in hellos if name == host]
    if not regional_times:
        raise ScenarioFactsError(
            f"pcap 中没有响应返回的区域主机 {host} 的 ClientHello。"
        )

    tool = _load_observation(evidence, root, "A14-tool-call.json")
    # 三跳顺序从原始证据推导，不读脚本写的顺序声明：relay.json 的连接墙钟时刻与
    # pcap 的捕获时刻同为 Unix 时间，可直接比较。
    create_at, uploaded_at = _upload_chain_times(evidence, root)
    first_seen = min(regional_times)
    last_seen = max(regional_times)
    if not create_at <= first_seen:
        raise ScenarioFactsError("create 不早于区域连接，上传顺序不成立。")
    if not last_seen <= uploaded_at:
        raise ScenarioFactsError("区域连接不早于 uploaded，上传顺序不成立。")

    return {
        "tool_name": _require(tool, "tool_name", "A14 工具调用记录"),
        "tool_call_id": _require(tool, "tool_call_id", "A14 工具调用记录"),
        "create_request": {
            "method": "POST",
            "path": "/backend-api/files",
            "status_2xx": True,
        },
        "upload_url_source_event": {
            "event": "file_create_response",
            "host": host,
            "url_sha256": hashlib.sha256(upload_url.encode("utf-8")).hexdigest(),
        },
        "put_destination": {
            "host": host,
            "sni": host,
            "first_seen_at_utc": _utc(first_seen),
            "last_seen_at_utc": _utc(last_seen),

        },
        "uploaded_event": {
            "method": uploaded_request["method"],
            "path_suffix": "/uploaded",
            "status_2xx": True,
        },
        "regional_sni": host,
        "regional_host_from_response": True,
        "upload_sequence": {
            "create_before_regional": True,
            "regional_before_uploaded": True,
        },
    }


def _upload_chain_times(evidence: EvidenceSet, root: Path) -> tuple[float, float]:
    """从 relay.json 取出 create 与 uploaded 所在连接的墙钟时刻（Unix 秒）。

    relay 的 segments 用相对 monotonic 毫秒，无法与 pcap 的捕获时间比较；连接记录
    额外带 `opened_at_unix_ms`／`closed_at_unix_ms` 才能跨两侧排序。区域 PUT 直连
    不经中继，只在 pcap 里可见，所以这个共同基准是三跳判据成立的前提。
    """

    manifest_path = root / RELAY_DIR / "relay.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ScenarioFactsError("缺少 relay.json，无法取得连接时刻。")
    evidence.bind(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScenarioFactsError(f"relay.json 不可解析：{error}") from error
    times: dict[str, float] = {}
    for connection in manifest.get("connections") or []:
        identifier = connection.get("connection_id")
        opened = connection.get("opened_at_unix_ms")
        closed = connection.get("closed_at_unix_ms")
        if not isinstance(identifier, int) or not isinstance(opened, (int, float)):
            continue
        stem = f"conn{identifier:03d}.client_to_upstream.bin"
        path = root / RELAY_DIR / stem
        if path.is_symlink() or not path.is_file():
            continue
        for request in _iter_requests(path.read_bytes()):
            target = _path_of(request["target"])
            if request["method"] != "POST":
                continue
            if target == "/backend-api/files" and "create" not in times:
                times["create"] = float(opened) / 1000.0
            elif target.endswith("/uploaded"):
                # uploaded 取连接关闭时刻的上界；缺失时退回打开时刻。
                times["uploaded"] = float(
                    closed if isinstance(closed, (int, float)) else opened
                ) / 1000.0
    if "create" not in times or "uploaded" not in times:
        raise ScenarioFactsError(
            "relay.json 缺少 create 或 uploaded 连接的墙钟时刻，无法证明三跳顺序。"
        )
    return times["create"], times["uploaded"]


def _ordered(earlier: str, later: str) -> bool:
    try:
        first = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
        second = datetime.fromisoformat(str(later).replace("Z", "+00:00"))
    except ValueError as error:
        raise ScenarioFactsError(f"时间不可比较：{earlier} / {later}") from error
    if first.tzinfo is None or second.tzinfo is None:
        raise ScenarioFactsError("上传顺序时间必须带时区。")
    return first <= second


EXTRACTORS = {
    "A11": _facts_a11,
    "A13": _facts_a13,
    "A14": _facts_a14,
}


def build(
    scenario_id: str,
    job_id: str,
    run_id: str,
    run_root: Path,
    output: Path | None = None,
) -> dict[str, Any]:
    """提取一个场景的原始事实；证据不足即抛 ScenarioFactsError。"""

    if scenario_id not in EXTRACTORS:
        raise ScenarioFactsError(f"R0 未登记场景：{scenario_id}")
    if run_root.is_symlink() or not run_root.is_dir():
        raise ScenarioFactsError(f"run 目录不可用：{run_root}")
    evidence = EvidenceSet(run_root)
    facts = EXTRACTORS[scenario_id](evidence, run_root)
    destination = output or (run_root / FACTS_DIR / f"{scenario_id}-facts.json")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return build_facts_document(
            scenario_id=scenario_id,
            job_id=job_id,
            run_id=run_id,
            facts=facts,
            evidence_bindings=evidence.bindings(),
            observed_at_utc=_utc(datetime.now(tz=timezone.utc).timestamp()),
            approved_roots={EVIDENCE_ROOT_ROLE: run_root},
            output=destination,
        )
    except ScenarioReceiptError as error:
        raise ScenarioFactsError(f"原始事实未通过契约校验：{error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=list(SUPPORTED_SCENARIOS))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        document = build(
            args.scenario, args.job_id, args.run_id, args.run_root, args.output
        )
    except (ScenarioFactsError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        # 诊断不进收据体系、不参与判定，只为排障保留失败形态。
        try:
            diagnostic = args.run_root / FACTS_DIR / f"{args.scenario}-scenario-failure.json"
            diagnostic.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_failure_diagnostic(
                diagnostic, scenario_id=args.scenario, reason=str(error)
            )
        except OSError:
            pass
        return 2
    print(
        json.dumps(
            {
                "scenario_id": document["scenario_id"],
                "final_state": document["final_state"],
                "evidence_bindings": len(document["evidence_bindings"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
