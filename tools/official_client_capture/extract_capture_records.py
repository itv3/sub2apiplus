#!/usr/bin/env python3
"""从等长脱敏后的中继字节生成采集记录的最小可审计证据。

本工具只接受已经由 ``scrub_raw_bytes.py --verify`` 处理过的目录。它不保留
Authorization、账号 ID、对话文本或工具参数，只输出连接完整性、固定枚举、请求行和
字节哈希。三个子命令分别服务于：

* ``body007``：真实编码工作流中的 ``input[].type`` 完整计数；
* ``ep024``：同一条 ``codex-tui`` WS 连接上的 ``compaction_trigger``；
* ``ep024-negative``：``codex exec '/compact'`` 只形成普通消息的洁净负例；
* ``conn-retry``：受控 retry 的 attempt → TCP connection 归属。
"""

from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import os
import re
from pathlib import Path

try:
    # 直接按脚本运行时，所在目录已在 sys.path。
    from relay_extract import parse_h1_stream
    from relay_extract import parse_ws_frames
    from extract_compaction_reason import http_json_objects
    from extract_compaction_reason import ws_json_objects
except ModuleNotFoundError:
    # 作为仓库内模块导入时使用完整包名，方便测试与交互复核。
    from tools.official_client_capture.relay_extract import parse_h1_stream
    from tools.official_client_capture.relay_extract import parse_ws_frames
    from tools.official_client_capture.extract_compaction_reason import http_json_objects
    from tools.official_client_capture.extract_compaction_reason import ws_json_objects


def target_h1_requests(data: bytes, target: str) -> list[bytes]:
    """切出目标 h1 请求的完整 header+body，支持同连接上的多个 retry。"""
    requests: list[bytes] = []
    position = 0
    while position < len(data):
        head_end = data.find(b"\r\n\r\n", position)
        if head_end < 0:
            break
        head_end += 4
        head = data[position:head_end]
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " HTTP/1." not in request_line:
            break
        length_match = re.search(rb"\r\ncontent-length:\s*(\d+)\r\n", head.lower())
        body_length = int(length_match.group(1)) if length_match else 0
        request_end = head_end + body_length
        if request_end > len(data):
            raise ValueError(f"请求体不完整：{request_line}")
        matched = (
            request_line.startswith("GET /backend-api/codex/models?")
            if target == "models"
            else request_line == "POST /backend-api/codex/responses HTTP/1.1"
        )
        if matched:
            requests.append(data[position:request_end])
        position = request_end
    return requests


def codex_version(value: str) -> str:
    """校验写入采集记录的 Codex CLI 版本。"""

    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise argparse.ArgumentTypeError("Codex 版本必须是三段数字")
    return value


def client_label(args: argparse.Namespace) -> str:
    """兼容旧的纯函数调用；命令行入口始终显式提供该字段。"""

    return f"codex-cli {getattr(args, 'codex_version', '0.145.0')}"


def load_manifest(relay_dir: Path) -> dict | None:
    path = relay_dir / "relay.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def connection_integrity(relay_dir: Path, manifest: dict | None) -> dict:
    sizes: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for path_text in glob.glob(str(relay_dir / "conn*.bin")):
        path = Path(path_text)
        match = re.fullmatch(
            r"(conn\d+)\.(client_to_upstream|upstream_to_client)\.bin",
            path.name,
        )
        if match:
            sizes[match.group(1)][match.group(2)] = path.stat().st_size
    if not sizes:
        raise ValueError(f"{relay_dir} 中没有连接字节")

    expected_upstream_only: set[str] = set()
    manifest_ids: set[str] = set()
    invalid_ids: list[int] = []
    if manifest:
        for connection in manifest.get("connections", []):
            connection_id = connection.get("connection_id")
            if not isinstance(connection_id, int):
                continue
            name = f"conn{connection_id:03d}"
            manifest_ids.add(name)
            if connection.get("expected_upstream_only") is True:
                expected_upstream_only.add(name)
            if connection.get("valid") is not True:
                invalid_ids.append(connection_id)
        if manifest_ids != set(sizes):
            raise ValueError(
                "relay.json 与字节文件的连接集合不一致："
                f"manifest={sorted(manifest_ids)} bytes={sorted(sizes)}"
            )

    both: list[str] = []
    upstream_only: list[str] = []
    downstream_only: list[str] = []
    idle: list[str] = []
    for name, directions in sorted(sizes.items()):
        up = directions.get("client_to_upstream", 0)
        down = directions.get("upstream_to_client", 0)
        if up and down:
            both.append(name)
        elif up:
            upstream_only.append(name)
        elif down:
            downstream_only.append(name)
        else:
            idle.append(name)

    unexpected_upstream_only = sorted(set(upstream_only) - expected_upstream_only)
    clean = not downstream_only and not idle and not unexpected_upstream_only and not invalid_ids
    return {
        "total": len(sizes),
        "both": len(both),
        "upstream_only": upstream_only,
        "expected_upstream_only": sorted(expected_upstream_only),
        "unexpected_upstream_only": unexpected_upstream_only,
        "downstream_only": downstream_only,
        "idle": idle,
        "invalid_manifest_connections": invalid_ids,
        "clean_after_declared_intervention": clean,
    }


def parsed_h1_connections(relay_dir: Path) -> list[dict]:
    connections: list[dict] = []
    for path in sorted(relay_dir.glob("conn*.client_to_upstream.bin")):
        data = path.read_bytes()
        if not data:
            continue
        requests = parse_h1_stream(data)
        record = {
            "connection": path.name.split(".", 1)[0],
            "data": data,
            "requests": requests,
            "ws_frames": [],
        }
        if any(
            name.lower() == "upgrade"
            for request in requests
            for name in request.get("header_names_in_order", [])
        ):
            head_end = data.find(b"\r\n\r\n")
            if head_end >= 0:
                record["ws_frames"] = parse_ws_frames(data[head_end + 4 :])
        connections.append(record)
    return connections


def header_value(request: dict, name: str) -> str | None:
    target = name.lower()
    for item in request.get("headers", []):
        if item.get("name", "").lower() == target:
            return item.get("value")
    return None


def write_json(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)


def extract_body007(args: argparse.Namespace) -> dict:
    relay_dir = Path(args.relay_dir)
    manifest = load_manifest(relay_dir)
    integrity = connection_integrity(relay_dir, manifest)
    if not integrity["clean_after_declared_intervention"]:
        raise ValueError(f"BODY-007 样本不完整：{integrity}")

    counts: collections.Counter[str] = collections.Counter()
    input_lengths: list[int] = []
    frames = 0
    for connection in parsed_h1_connections(relay_dir):
        for frame in connection["ws_frames"]:
            input_shape = frame.get("shape", {}).get("input")
            if not isinstance(input_shape, dict):
                continue
            frame_counts = input_shape.get("_types_count")
            if not isinstance(frame_counts, dict):
                continue
            frames += 1
            counts.update({str(key): int(value) for key, value in frame_counts.items()})
            input_lengths.append(int(input_shape.get("_array_len", 0)))

    required = {"message", "reasoning", "custom_tool_call", "custom_tool_call_output"}
    missing = sorted(required - {key for key, value in counts.items() if value > 0})
    if not frames or missing:
        raise ValueError(f"不是完整编码工具工作流：frames={frames} missing={missing}")
    if sum(input_lengths) != sum(counts.values()):
        raise ValueError("input 数组长度之和与 type 计数之和不一致")

    return {
        "schema_version": "codex-capture-record/body007/v1",
        "run_id": args.run_id,
        "status": "complete",
        "client": client_label(args),
        "boundary": "official_cli_to_official_platform",
        "authentication": "OpenAI OAuth",
        "workflow": "十轮真实编码任务（读文件、运行命令、修改代码、测试与复核）",
        "connection_integrity": integrity,
        "input_observation": {
            "frames_with_input": frames,
            "input_items_total": sum(counts.values()),
            "max_input_length": max(input_lengths),
            "type_counts": dict(counts.most_common()),
            "tool_workflow_present": True,
        },
        "evidence_boundary": "完整计数仅适用于本次固定十轮采样场景，不外推为协议封闭集合",
    }


def extract_ep024(args: argparse.Namespace) -> dict:
    relay_dir = Path(args.relay_dir)
    integrity = connection_integrity(relay_dir, load_manifest(relay_dir))
    if not integrity["clean_after_declared_intervention"]:
        raise ValueError(f"EP-024 样本不完整：{integrity}")

    matches: list[dict] = []
    response_create_frames = 0
    for connection in parsed_h1_connections(relay_dir):
        originators = {
            header_value(request, "originator")
            for request in connection["requests"]
        }
        for frame in connection["ws_frames"]:
            if frame.get("event_type") != "response.create":
                continue
            response_create_frames += 1
            input_shape = frame.get("shape", {}).get("input")
            types = input_shape.get("_types", []) if isinstance(input_shape, dict) else []
            if "compaction_trigger" in types:
                matches.append({
                    "connection": connection["connection"],
                    "originators": sorted(value for value in originators if value),
                    "input_types": types,
                    "compaction_trigger_count": types.count("compaction_trigger"),
                })

    if len(matches) != 1 or matches[0]["originators"] != ["codex-tui"]:
        raise ValueError(f"未唯一命中 codex-tui compaction_trigger：{matches}")
    return {
        "schema_version": "codex-capture-record/ep024/v1",
        "run_id": args.run_id,
        "status": "complete",
        "client": client_label(args),
        "connection_integrity": integrity,
        "response_create_frames": response_create_frames,
        "matches": matches,
        "conclusion": "真实 TUI 连接发出了含 compaction_trigger 的上行 response.create",
        "evidence_boundary": (
            "wire 中的 originator=codex-tui 证明触发客户端表面；"
            "斜杠命令解析点仍由 TUI 源码证明"
        ),
    }


def exact_user_message_matches(value: object, expected: str) -> list[dict]:
    """提取固定用户文本的普通 message 形态，不把其他对话正文写入证据。"""
    if not isinstance(value, dict) or value.get("type") != "response.create":
        return []
    items = value.get("input")
    if not isinstance(items, list):
        return []

    matches: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content == expected:
            matches.append({
                "item_type": "message",
                "role": "user",
                "content_types": ["string"],
                "exact_text": expected,
            })
            continue
        if not isinstance(content, list):
            continue
        exact_parts = [
            part
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "input_text"
            and part.get("text") == expected
        ]
        if exact_parts:
            content_types = [
                part.get("type", "<missing>")
                for part in content
                if isinstance(part, dict)
            ]
            matches.append({
                "item_type": "message",
                "role": "user",
                "content_types": content_types,
                "exact_text": expected,
                "exact_input_text_parts": len(exact_parts),
            })
    return matches


def count_type_value(value: object, expected: str) -> int:
    """递归统计 JSON 对象中的固定 ``type`` 枚举。"""
    if isinstance(value, dict):
        own = int(value.get("type") == expected)
        return own + sum(count_type_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return sum(count_type_value(item, expected) for item in value)
    return 0


def extract_ep024_negative(args: argparse.Namespace) -> dict:
    """证明 exec 的 ``/compact`` 是普通消息，而不是客户端压缩控制面。"""
    relay_dir = Path(args.relay_dir)
    integrity = connection_integrity(relay_dir, load_manifest(relay_dir))
    if not integrity["clean_after_declared_intervention"]:
        raise ValueError(f"EP-024 负例样本不完整：{integrity}")

    response_create_frames = 0
    client_ws_json_frames = 0
    server_ws_json_frames = 0
    compaction_trigger_count = 0
    compact_endpoint_requests: list[dict] = []
    ordinary_message_matches: list[dict] = []

    for path in sorted(relay_dir.glob("conn*.client_to_upstream.bin")):
        connection = path.name.split(".", 1)[0]
        client_data = path.read_bytes()
        requests = parse_h1_stream(client_data)
        originators = sorted({
            value
            for request in requests
            if (value := header_value(request, "originator")) is not None
        })
        for request in requests:
            request_line = request.get("request_line", "")
            if "/responses/compact" in request_line:
                compact_endpoint_requests.append({
                    "connection": connection,
                    "request_line": request_line,
                })

        head_end = client_data.find(b"\r\n\r\n")
        websocket = (
            head_end >= 0
            and b"upgrade: websocket" in client_data[:head_end].lower()
        )
        if websocket:
            client_objects = list(ws_json_objects(client_data[head_end + 4 :]))
        else:
            client_objects = list(http_json_objects(client_data))

        if websocket:
            client_ws_json_frames += len(client_objects)
        for obj in client_objects:
            compaction_trigger_count += count_type_value(obj, "compaction_trigger")
            if not isinstance(obj, dict) or obj.get("type") != "response.create":
                continue
            response_create_frames += 1
            for match in exact_user_message_matches(obj, "/compact"):
                ordinary_message_matches.append({
                    "connection": connection,
                    "transport": "websocket" if websocket else "http",
                    "originators": originators,
                    **match,
                })

        if not websocket:
            continue
        server_path = relay_dir / f"{connection}.upstream_to_client.bin"
        server_data = server_path.read_bytes()
        server_head_end = server_data.find(b"\r\n\r\n")
        if server_head_end < 0:
            raise ValueError(f"{connection} 的 WS 服务端响应缺少完整 header")
        server_objects = list(ws_json_objects(server_data[server_head_end + 4 :]))
        server_ws_json_frames += len(server_objects)
        compaction_trigger_count += sum(
            count_type_value(obj, "compaction_trigger") for obj in server_objects
        )

    if response_create_frames == 0:
        raise ValueError("EP-024 负例没有 response.create 业务帧")
    if not ordinary_message_matches:
        raise ValueError("没有找到普通 user message 中的精确 /compact")
    if any(match["originators"] != ["codex_exec"] for match in ordinary_message_matches):
        raise ValueError(f"/compact 不是由 codex_exec 发出：{ordinary_message_matches}")
    if compaction_trigger_count:
        raise ValueError(f"负例中意外出现 compaction_trigger：{compaction_trigger_count}")
    if compact_endpoint_requests:
        raise ValueError(f"负例中意外出现 /responses/compact：{compact_endpoint_requests}")

    return {
        "schema_version": "codex-capture-record/ep024-negative/v1",
        "run_id": args.run_id,
        "status": "complete",
        "client": client_label(args),
        "surface": "codex exec",
        "command": "codex exec '/compact'",
        "connection_integrity": integrity,
        "business_observation": {
            "response_create_frames": response_create_frames,
            "client_ws_json_frames": client_ws_json_frames,
            "server_ws_json_frames": server_ws_json_frames,
            "ordinary_message_match_count": len(ordinary_message_matches),
            "ordinary_message_matches": ordinary_message_matches,
            "compaction_trigger_count": compaction_trigger_count,
            "responses_compact_request_count": len(compact_endpoint_requests),
        },
        "conclusion": "codex exec 将字面量 /compact 作为普通 user message 发出",
        "evidence_boundary": (
            "固定 exec 场景的 wire 负例；斜杠命令解析仅存在于 TUI 的全称结论仍由源码证明"
        ),
    }


def extract_conn_retry(args: argparse.Namespace) -> dict:
    relay_dir = Path(args.relay_dir)
    manifest = load_manifest(relay_dir)
    if manifest is None:
        raise ValueError("CONN retry 证据必须包含 relay.json")
    integrity = connection_integrity(relay_dir, manifest)
    if not integrity["clean_after_declared_intervention"]:
        raise ValueError(f"CONN retry 样本存在非预期缺口：{integrity}")

    events = []
    for line in Path(args.intervention).read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") == "conn_retry_probe":
            events.append(event)
    events.sort(key=lambda item: item.get("attempt", 0))
    if [event.get("attempt") for event in events] != [1, 2]:
        raise ValueError(f"retry attempt 不是精确两次：{events}")
    if any(event.get("mode") != args.mode for event in events):
        raise ValueError("intervention mode 与命令参数不一致")
    if any(event.get("target", "models") != args.target for event in events):
        raise ValueError("intervention target 与命令参数不一致")

    heads_by_connection: dict[int, list[bytes]] = {}
    for event in events:
        connection_id = int(event["connection_id"])
        path = relay_dir / f"conn{connection_id:03d}.client_to_upstream.bin"
        heads_by_connection.setdefault(
            connection_id,
            target_h1_requests(path.read_bytes(), args.target),
        )
    ordered_heads: list[bytes] = []
    offsets: collections.Counter[int] = collections.Counter()
    for event in events:
        connection_id = int(event["connection_id"])
        index = offsets[connection_id]
        candidates = heads_by_connection[connection_id]
        if index >= len(candidates):
            raise ValueError(f"conn{connection_id:03d} 缺少 attempt 对应请求字节")
        ordered_heads.append(candidates[index])
        offsets[connection_id] += 1

    same_connection = events[0]["connection_id"] == events[1]["connection_id"]
    expected_same_connection = args.mode == "keepalive-500"
    if same_connection != expected_same_connection:
        raise ValueError(
            f"{args.mode} 的连接归属不符合预期：same_connection={same_connection}"
        )
    expected_actions = (
        ["synthetic_500_keepalive", "forward_to_production"]
        if expected_same_connection
        else ["disconnect", "forward_to_production"]
    )
    if [event.get("action") for event in events] != expected_actions:
        raise ValueError(f"干预动作序列不正确：{events}")

    attempts = []
    parsed_requests = []
    for event, head in zip(events, ordered_heads, strict=True):
        head_end = head.index(b"\r\n\r\n") + 4
        header_lines = head[: head_end - 4].decode("latin-1").split("\r\n")[1:]
        headers = {}
        for line in header_lines:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.lower()] = value.strip()
        body = head[head_end:]
        parsed_requests.append({"headers": headers, "body": body})
        attempts.append({
            "attempt": event["attempt"],
            "connection_id": event["connection_id"],
            "action": event["action"],
            "request_line": event["request_line"],
            "scrubbed_request_sha256": hashlib.sha256(head).hexdigest(),
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        })
    changed_header_names = sorted(
        name
        for name in set(parsed_requests[0]["headers"]) | set(parsed_requests[1]["headers"])
        if parsed_requests[0]["headers"].get(name)
        != parsed_requests[1]["headers"].get(name)
    )
    bodies_identical = parsed_requests[0]["body"] == parsed_requests[1]["body"]
    if not bodies_identical:
        raise ValueError("retry 的逻辑请求 body 发生变化，不能归为同一次上层调用")
    unexpected_header_changes = sorted(set(changed_header_names) - {"cookie"})
    if unexpected_header_changes:
        raise ValueError(f"retry 出现非 Cookie header 变化：{unexpected_header_changes}")
    retry_delay_ms = int(events[1]["t_unix_ms"]) - int(events[0]["t_unix_ms"])
    target_label = "models GET" if args.target == "models" else "Responses POST"
    return {
        "schema_version": "codex-capture-record/conn-retry/v1",
        "run_id": args.run_id,
        "status": "complete",
        "client": client_label(args),
        "provider": args.provider,
        "mode": args.mode,
        "target": args.target,
        "connection_integrity": integrity,
        "attempts": attempts,
        "retry_delay_ms": retry_delay_ms,
        "same_tcp_connection": same_connection,
        "scrubbed_requests_identical": ordered_heads[0] == ordered_heads[1],
        "request_bodies_identical": bodies_identical,
        "changed_header_names": changed_header_names,
        "conclusion": (
            f"可重试 500 且连接存活时，单次上层 {target_label} 的内部 retry 复用同一 TCP"
            if same_connection
            else f"首连接被受控断开后，同一次上层 {target_label} 的内部 retry 改用新 TCP"
        ),
        "evidence_boundary": (
            "wire 证明 attempt 与 TCP 归属；同一内存 ReqwestTransport/Client 的复用"
            "由 EndpointSession retry 闭包捕获 &self.transport 的源码链证明"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("body007", "ep024", "ep024-negative"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--codex-version", type=codex_version, default="0.145.0")
        sub.add_argument("--run-id", required=True)
        sub.add_argument("--relay-dir", required=True)
        sub.add_argument("--output", required=True)
    retry = subparsers.add_parser("conn-retry")
    retry.add_argument("--codex-version", type=codex_version, default="0.145.0")
    retry.add_argument("--run-id", required=True)
    retry.add_argument("--relay-dir", required=True)
    retry.add_argument("--intervention", required=True)
    retry.add_argument("--mode", choices=("keepalive-500", "disconnect"), required=True)
    retry.add_argument("--target", choices=("models", "responses"), default="models")
    retry.add_argument("--provider", default="未记录")
    retry.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.command == "body007":
        result = extract_body007(args)
    elif args.command == "ep024":
        result = extract_ep024(args)
    elif args.command == "ep024-negative":
        result = extract_ep024_negative(args)
    else:
        result = extract_conn_retry(args)
    write_json(Path(args.output), result)
    print(json.dumps({
        "status": result["status"],
        "run_id": result["run_id"],
        "output": args.output,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
