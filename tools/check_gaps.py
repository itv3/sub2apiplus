#!/usr/bin/env python3
"""校验第二部分的抓包证据边界与逐项人工审计分类一致。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
sys.path.insert(0, str(ROOT / "tools"))

import evidence_index as evidence  # noqa: E402
import spec_status  # noqa: E402


def main() -> int:
    text = SPEC.read_text(encoding="utf-8")
    try:
        statuses = spec_status.status_texts(text)
    except ValueError as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1

    actual_limited = {sid for sid, status in statuses.items() if "抓包有限" in status}
    actual_na = {sid for sid, status in statuses.items() if "抓包不适用" in status}
    errors = []
    if actual_limited != evidence.CAPTURE_AUDIT_LIMITED:
        errors.append(
            "抓包有限分类漂移："
            f"缺少={sorted(evidence.CAPTURE_AUDIT_LIMITED - actual_limited)}，"
            f"多出={sorted(actual_limited - evidence.CAPTURE_AUDIT_LIMITED)}"
        )
    if actual_na != evidence.CAPTURE_AUDIT_NA:
        errors.append(
            "抓包不适用分类漂移："
            f"缺少={sorted(evidence.CAPTURE_AUDIT_NA - actual_na)}，"
            f"多出={sorted(actual_na - evidence.CAPTURE_AUDIT_NA)}"
        )

    for sid in evidence.CAPTURE_AUDIT_LIMITED:
        if not statuses.get(sid, "").startswith("🟡"):
            errors.append(f"{sid} 抓包有限时，状态必须以 🟡 开头")
    for sid in evidence.CAPTURE_AUDIT_NA:
        if not statuses.get(sid, "").startswith("—"):
            errors.append(f"{sid} 抓包不适用时，状态必须以 — 开头")

    if errors:
        for error in errors:
            print(f"🔴 {error}", file=sys.stderr)
        return 1

    if set(statuses) != (
        spec_status.VISIBLE_RULES
        | spec_status.MECHANISM_ITEMS
        | spec_status.EVIDENCE_RECORDS
    ):
        print(
            "🔴 第二部分编号集合与分类常量不一致",
            file=sys.stderr,
        )
        return 1

    print(
        f"✅ 第二部分证据边界准确：抓包有限 {len(evidence.CAPTURE_AUDIT_LIMITED)}，"
        f"不适用 {len(evidence.CAPTURE_AUDIT_NA)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
