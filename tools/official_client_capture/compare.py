#!/usr/bin/env python3
"""比较两份已脱敏的官方客户端结构证据。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.analysis import compare_normalized
from tools.official_client_capture.capturelib.security import secure_write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="比较两份脱敏抓包结构。")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    baseline = json.loads(arguments.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(arguments.candidate.read_text(encoding="utf-8"))
    result = compare_normalized(baseline, candidate)
    if arguments.output:
        secure_write_json(arguments.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["equal"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
