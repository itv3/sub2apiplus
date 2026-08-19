#!/usr/bin/env python3
"""供 Claude FW-F v4 取证使用的无网络 stdio MCP 固定服务。"""

from __future__ import annotations

import json
import sys
from typing import Any


def _tools() -> list[dict[str, Any]]:
    result = [
        {
            "name": "probe_echo",
            "description": "返回固定的 FW_F_V4_MCP_OK 标记。",
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }
    ]
    for index in range(1, 33):
        result.append(
            {
                "name": f"deferred_probe_{index:02d}",
                "description": f"FW-F v4 延迟工具目录占位 {index:02d}。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        )
    return result


def _reply(request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if request_id is None:
        return None
    if method == "initialize":
        result: Any = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "claude-fw-f-v4", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {"tools": _tools()}
    elif method == "tools/call":
        result = {
            "content": [{"type": "text", "text": "FW_F_V4_MCP_OK"}],
            "isError": False,
        }
    elif method in {"resources/list", "prompts/list"}:
        result = {"resources": []} if method == "resources/list" else {"prompts": []}
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        response = _reply(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
