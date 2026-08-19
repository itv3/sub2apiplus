#!/usr/bin/env python3
"""为 FW-F v4 返回固定 additionalContext 的只读 PreToolUse hook。"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        value = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 2
    if not isinstance(value, dict) or value.get("hook_event_name") != "PreToolUse":
        return 3
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "FW_F_V4_HOOK_CONTEXT",
        }
    }
    json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
