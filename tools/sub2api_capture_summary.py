#!/usr/bin/env python3
"""汇总 Sub2API 阶段二采样记录。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


INTERESTING_HEADERS = [
    "user-agent",
    "anthropic-version",
    "anthropic-beta",
    "openai-beta",
    "originator",
    "version",
    "x-stainless-arch",
    "x-stainless-lang",
    "x-stainless-os",
    "x-stainless-package-version",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-sub2api-capture",
]


def header_value(headers: dict[str, Any], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            if isinstance(value, list):
                return ", ".join(str(item) for item in value)
            return str(value)
    return ""


def body_field(body: Any, name: str) -> str:
    if not isinstance(body, dict):
        return ""
    value = body.get(name)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:300]
    return str(value)[:300]


def summarize(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    request = obj.get("request", {})
    response = obj.get("response", {})
    headers = request.get("headers", {})
    body = request.get("body")
    return {
        "file": path.name,
        "capture_id": obj.get("capture_id", ""),
        "time": obj.get("captured_at", ""),
        "method": request.get("method", ""),
        "path": request.get("upstream_path", ""),
        "status": response.get("status", ""),
        "model": body_field(body, "model"),
        "stream": body_field(body, "stream"),
        "messages_or_input": "messages" if isinstance(body, dict) and "messages" in body else ("input" if isinstance(body, dict) and "input" in body else ""),
        "event_types": ",".join(response.get("event_types") or []),
        "headers": {name: header_value(headers, name) for name in INTERESTING_HEADERS if header_value(headers, name)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="汇总 Sub2API 采样 JSON")
    parser.add_argument("log_dir", nargs="?", default="/var/log/sub2api-capture")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="输出 JSON 数组")
    args = parser.parse_args()

    paths = sorted(Path(args.log_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]
    rows = [summarize(path) for path in paths]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for row in rows:
        print(f"{row['capture_id']} {row['status']} {row['method']} {row['path']} model={row['model']} stream={row['stream']}")
        for key, value in row["headers"].items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
