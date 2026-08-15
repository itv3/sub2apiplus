#!/usr/bin/env python3
"""显式更新第二部分源码引用锚点清单。"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from check_spec_refs import DEFAULT_ANCHOR_MANIFEST
from check_spec_refs import DEFAULT_SPEC
from check_spec_refs import build_index
from check_spec_refs import context_digest
from check_spec_refs import extract_rule_source_refs
from check_spec_refs import is_external
from check_spec_refs import resolve_ref


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=pathlib.Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_ANCHOR_MANIFEST)
    parser.add_argument(
        "--write",
        action="store_true",
        help="确认以当前规格表引用重建锚点清单",
    )
    args = parser.parse_args()

    if not args.write:
        print("必须显式传入 --write 才会更新锚点清单。", file=sys.stderr)
        return 2

    spec_text = args.spec.read_text(encoding="utf-8")
    rules = extract_rule_source_refs(spec_text)
    if len(rules) != 53:
        print(f"第二部分应有 53 条规则，实际为 {len(rules)}。", file=sys.stderr)
        return 1

    index = build_index()
    output_rules: dict[str, list[dict[str, object]]] = {}
    for sid, refs in rules.items():
        entries: list[dict[str, object]] = []
        for ref in refs:
            if is_external(ref):
                print(f"{sid} 仍引用未固化外部源码：{ref.target()}", file=sys.stderr)
                return 1
            path, error = resolve_ref(ref, index)
            if error or path is None:
                print(f"{sid} 无法定位 {ref.target()}：{error}", file=sys.stderr)
                return 1
            digest, preview = context_digest(path, ref)
            entries.append(
                {
                    **ref.identity(),
                    "context_sha256": digest,
                    "anchor": preview,
                }
            )
        output_rules[sid] = entries

    manifest = {
        "schema_version": 1,
        "spec": "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
        "source_version": "0.147.0",
        "rules": output_rules,
    }
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    reference_count = sum(len(entries) for entries in output_rules.values())
    print(f"已写入 {len(output_rules)} 条规则、{reference_count} 处源码引用锚点。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
