#!/usr/bin/env python3
"""检查生产 Go 代码中的 Codex 版本标识符泄漏。

§3.1 要求稳定执行引擎“不包含按版本散落的常量和模型特判”，§4.5.13 要求新 Codex
版本原则上只新增画像、版本清单和测试，不修改 §3.5.2 的共享接入点。两条约束依赖
同一个可机器复算的事实：除版本快照本身以外，生产代码不应出现版本字面量或把版本
焊进符号名的标识符。

仓库当前尚未完成版本解耦，直接要求归零会让门禁恒红，因此本脚本以基线快照方式
运行：记录每个文件既有的命中行数，只允许下降，不允许上升，未登记的文件必须为零。
版本解耦完成后用 --update-baseline 逐步收紧，最终目标是除版本快照外全部归零。

用法：

    python3 tools/check_version_leak.py              # 门禁模式，比对基线
    python3 tools/check_version_leak.py --update-baseline
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "version_leak_baseline.json"
SCAN_ROOT = ROOT / "backend"

# 版本字面量与版本化标识符：命中即表示该文件把某个具体 Codex 版本焊进了代码。
LEAK_RE = re.compile(r"0\.145\.0|0_145_0|Codex0145|codex0145|officialCodexVersion0145")

# 版本快照按定义必须携带版本字面量，这是设计要求而非泄漏。
# 此处只豁免快照文件本身；通用执行层与共享业务层都不在豁免范围内。
EXEMPT = {
    "backend/internal/service/official_egress_codex_0145_profile.go",
}


def scan() -> dict[str, int]:
    """扫描生产 Go 代码，返回相对仓库根路径到命中行数的映射。"""
    hits: dict[str, int] = {}
    for path in sorted(SCAN_ROOT.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        count = sum(1 for line in text.splitlines() if LEAK_RE.search(line))
        if count:
            hits[rel] = count
    return hits


def load_baseline() -> dict[str, int] | None:
    if not BASELINE.exists():
        return None
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, dict):
        return None
    return {str(k): int(v) for k, v in files.items()}


def write_baseline(hits: dict[str, int]) -> None:
    payload = {
        "_comment": (
            "各生产文件当前的 Codex 版本标识符命中行数。门禁只允许下降，不允许上升；"
            "未列入本表的文件必须为零。版本解耦完成后应逐步收紧至空表。"
        ),
        "_exempt": sorted(EXEMPT),
        "files": dict(sorted(hits.items())),
    }
    BASELINE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="以当前扫描结果覆盖基线，用于版本解耦推进后收紧门禁",
    )
    args = parser.parse_args()

    hits = scan()
    total = sum(hits.values())

    if args.update_baseline:
        write_baseline(hits)
        print(f"✅ 已更新版本泄漏基线：{len(hits)} 个文件，合计 {total} 个命中行")
        return 0

    baseline = load_baseline()
    if baseline is None:
        print(
            f"🔴 缺少版本泄漏基线 {BASELINE.relative_to(ROOT)}，"
            "请先执行 --update-baseline",
            file=sys.stderr,
        )
        return 1

    errors: list[str] = []
    for rel, count in sorted(hits.items()):
        allowed = baseline.get(rel)
        if allowed is None:
            errors.append(
                f"{rel} 是基线外的新增版本泄漏（{count} 行）；"
                "共享业务层与通用执行层不得引入版本标识符"
            )
        elif count > allowed:
            errors.append(f"{rel} 版本泄漏增加：基线 {allowed} 行，当前 {count} 行")

    if errors:
        for error in errors:
            print(f"🔴 {error}", file=sys.stderr)
        return 1

    improved = {
        rel: (baseline[rel], hits.get(rel, 0))
        for rel in baseline
        if hits.get(rel, 0) < baseline[rel]
    }
    if improved:
        for rel, (was, now) in sorted(improved.items()):
            print(f"⬇️  {rel}：{was} → {now}")
        print(
            "提示：命中已下降，可执行 --update-baseline 收紧门禁，"
            "防止后续回升到旧水位"
        )

    print(f"✅ 版本泄漏未超基线：{len(hits)} 个文件，合计 {total} 个命中行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
