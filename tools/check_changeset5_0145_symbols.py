#!/usr/bin/env python3
"""冻结并检查变更集 5 的生产 Go 标识符 `0145` 闭集。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN_ROOT = ROOT / "backend"
PRE_INVENTORY = ROOT / "docs" / "changeset5" / "0145-pre-refactor-inventory.json"
ALLOWLIST = ROOT / "docs" / "changeset5" / "0145-symbol-allowlist.json"
IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*0145[A-Za-z0-9_]*\b")


def code_only(source: str) -> str:
    """移除 Go 注释与字符串内容，同时保留换行和字符位置。"""
    result = list(source)
    index = 0
    state = "code"
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                result[index] = result[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                result[index] = result[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if char == '"':
                result[index] = " "
                state = "string"
            elif char == "'":
                result[index] = " "
                state = "rune"
            elif char == "`":
                result[index] = " "
                state = "raw_string"
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                result[index] = " "
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                result[index] = result[index + 1] = " "
                index += 2
                state = "code"
                continue
            if char != "\n":
                result[index] = " "
            index += 1
            continue
        if state in {"string", "rune"}:
            result[index] = "\n" if char == "\n" else " "
            if char == "\\":
                if index + 1 < len(source):
                    index += 1
                    result[index] = "\n" if source[index] == "\n" else " "
            elif (state == "string" and char == '"') or (state == "rune" and char == "'"):
                state = "code"
            index += 1
            continue
        if state == "raw_string":
            result[index] = "\n" if char == "\n" else " "
            if char == "`":
                state = "code"
            index += 1
    return "".join(result)


def scan() -> dict[str, list[dict[str, object]]]:
    found: dict[str, list[dict[str, object]]] = {}
    for path in sorted(SCAN_ROOT.rglob("*.go")):
        if path.name.endswith("_test.go") or path.is_symlink():
            continue
        source = path.read_text(encoding="utf-8")
        stripped = code_only(source)
        relative = path.relative_to(ROOT).as_posix()
        for match in IDENTIFIER_RE.finditer(stripped):
            line = stripped.count("\n", 0, match.start()) + 1
            found.setdefault(match.group(0), []).append({"path": relative, "line": line})
    return found


def inventory_payload(found: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    identifiers = []
    fingerprints: list[str] = []
    for name in sorted(found):
        occurrences = found[name]
        identifiers.append({"name": name, "count": len(occurrences), "occurrences": occurrences})
        fingerprints.extend(
            f"{name}\0{item['path']}\0{item['line']}" for item in occurrences
        )
    fingerprint = hashlib.sha256("\n".join(fingerprints).encode()).hexdigest()
    return {
        "schema_version": "changeset5-0145-pre-refactor-inventory/v1",
        "changeset": "5",
        "scope": "backend production Go identifiers; comments, strings and tests excluded",
        "identifier_count": len(identifiers),
        "occurrence_count": sum(item["count"] for item in identifiers),
        "occurrence_fingerprint_sha256": fingerprint,
        "identifiers": identifiers,
    }


def self_test() -> int:
    source = '''package p
// comment0145
const evidence = "string0145"
var generic0145 = `raw0145`
func call0145() { _ = '\\n' /* block0145 */ }
'''
    names = IDENTIFIER_RE.findall(code_only(source))
    if names != ["generic0145", "call0145"]:
        print(f"🔴 0145 词法判据自测失败：{names}", file=sys.stderr)
        return 1
    print("✅ 0145 生产标识符词法判据自测通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-pre-inventory", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    found = scan()
    if args.write_pre_inventory:
        payload = inventory_payload(found)
        PRE_INVENTORY.parent.mkdir(parents=True, exist_ok=True)
        PRE_INVENTORY.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"✅ 已冻结 {payload['identifier_count']} 个标识符、"
            f"{payload['occurrence_count']} 次出现"
        )
        return 0
    try:
        allowlist = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"🔴 无法读取 0145 标识符允许清单：{exc}", file=sys.stderr)
        return 1
    if allowlist.get("schema_version") != "changeset5-0145-symbol-allowlist/v1":
        print("🔴 0145 标识符允许清单 schema 非法", file=sys.stderr)
        return 1
    entries = allowlist.get("allowed_identifiers")
    if not isinstance(entries, list):
        print("🔴 0145 标识符允许清单缺少 allowed_identifiers", file=sys.stderr)
        return 1
    allowed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("reason"):
            print("🔴 0145 标识符允许项缺少名称或保留理由", file=sys.stderr)
            return 1
        allowed.add(entry["name"])
    unexpected = sorted(set(found) - allowed)
    stale = sorted(allowed - set(found))
    if unexpected or stale:
        for name in unexpected:
            locations = ", ".join(
                f"{item['path']}:{item['line']}" for item in found[name][:4]
            )
            print(f"🔴 版本无关生产标识符仍含 0145：{name}（{locations}）", file=sys.stderr)
        for name in stale:
            print(f"🔴 0145 允许清单出现陈旧标识符：{name}", file=sys.stderr)
        return 1
    print(f"✅ 生产标识符 0145 闭集通过：仅保留 {len(allowed)} 个证据／历史 ID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
