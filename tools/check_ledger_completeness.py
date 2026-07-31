#!/usr/bin/env python3
"""检查 §3.5 源码改动台账是否覆盖全部 Codex/OpenAI 出站定型面。

§4.7 第 9 步要求每次合并上游后更新台账，但台账是手工维护的表格，没有任何机制
保证它与代码同步。本脚本用可复算的判据重新计算“应当登记”的文件集合，与文档
实际登记的内容比对，把台账完整性从人工承诺变成机器断言。

判据是「生产 Go 代码中引用了 Codex/OpenAI 出站定型专属符号」。该判据刻意不使用
通用的 OfficialEgress 前缀：那个前缀同时覆盖 Anthropic 画像，会把 §1.1 明确排除
的供应商路径卷进来。当前判据在本仓库可完整区分两侧，因此 SCOPE_EXCLUSIONS 为空。

用法：

    python3 tools/check_ledger_completeness.py
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "CODEX_CLI_0145_EGRESS_SPEC.md"
SCAN_ROOT = ROOT / "backend"

# Codex/OpenAI 出站定型专属符号。命中即表示该文件参与官方出站形态的产生，
# 无论它是否携带版本字面量——WS 传输、连接池与握手定型点都不含版本号。
SURFACE_RE = re.compile(
    r"officialCodex|OfficialCodex|Codex0145|codex0145"
    r"|OpenAIOfficialEgress|officialEgressWebSocket"
    r"|officialOpenAIHTTPBodyContract|OfficialEgressTransportWebSocket"
)

# 命中判据但按 §1.1 不属于本版规则范围的文件。每条必须写明依据；
# 当前判据已能区分 Anthropic 与 API Key mimic 路径，因此为空。
SCOPE_EXCLUSIONS: dict[str, str] = {}


def scan_surface_files() -> list[str]:
    """返回参与 Codex/OpenAI 出站定型的生产文件，路径相对仓库根。"""
    found: list[str] = []
    for path in sorted(SCAN_ROOT.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if SURFACE_RE.search(text):
            found.append(path.relative_to(ROOT).as_posix())
    return found


def ledger_text() -> str:
    """取 §3.5 全文；台账登记只在该节内有效。"""
    text = SPEC.read_text(encoding="utf-8")
    start = text.find("## 3.5 源码改动台账")
    if start < 0:
        raise SystemExit("🔴 未找到 §3.5 源码改动台账")
    end = text.find("\n## ", start + 1)
    return text[start:end if end > 0 else len(text)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    ledger = ledger_text()
    surface = scan_surface_files()

    missing: list[str] = []
    for rel in surface:
        if rel in SCOPE_EXCLUSIONS:
            continue
        # 台账写完整路径；退回 basename 是为了容忍以文件组形式登记的条目。
        if rel in ledger or pathlib.PurePosixPath(rel).name in ledger:
            continue
        missing.append(rel)

    stale = [rel for rel in SCOPE_EXCLUSIONS if rel not in surface]

    if missing or stale:
        for rel in missing:
            print(
                f"🔴 {rel} 参与 Codex/OpenAI 出站定型，但未登记进 §3.5 台账",
                file=sys.stderr,
            )
        for rel in stale:
            print(
                f"🔴 {rel} 已不在出站定型面上，应从 SCOPE_EXCLUSIONS 删除",
                file=sys.stderr,
            )
        return 1

    covered = len(surface) - len(SCOPE_EXCLUSIONS)
    print(
        f"✅ §3.5 台账完整：{covered} 个出站定型文件全部登记"
        + (f"，另有 {len(SCOPE_EXCLUSIONS)} 个按 §1.1 排除" if SCOPE_EXCLUSIONS else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
