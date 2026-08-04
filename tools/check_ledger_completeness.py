#!/usr/bin/env python3
"""检查 §3.5 源码改动台账是否覆盖全部 Codex/OpenAI 出站定型面。

§4.7 第 9 步要求每次合并上游后更新台账，但台账是手工维护的表格，没有任何机制
保证它与代码同步。本脚本用可复算的判据重新计算“应当登记”的文件集合，与文档
实际登记的内容比对，把台账完整性从人工承诺变成机器断言。

判据是「生产 Go 代码中引用了 Codex/OpenAI 出站定型专属符号」。该判据刻意不使用
通用的 OfficialEgress 前缀：那个前缀同时覆盖 Anthropic 画像，会把 §1.1 明确排除
的供应商路径卷进来。当前生产判据可区分两侧；唯一排除项是扫描器分类表对被扫描
符号名的工具自引用，不是生产出站路径。

用法：

    python3 tools/check_ledger_completeness.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "CODEX_CLI_0145_EGRESS_SPEC.md"
SCAN_ROOT = ROOT / "backend"
INVENTORY = ROOT / "docs" / "changeset5" / "egress-surface-inventory.json"
CHANGESET6_TRANSITION = ROOT / "docs" / "changeset6" / "egress-surface-transition.json"
MAINTENANCE_RETIREMENT = ROOT / "docs" / "maintenance" / "official-egress-consolidation-retirement.json"
MAINTENANCE_RETIREMENT_SHA256 = "d60fb470a83f4a98f5de231265d2f695f3963536ec45290b36341c248a56ee36"

# Codex/OpenAI 出站定型专属符号。命中即表示该文件参与官方出站形态的产生，
# 无论它是否携带版本字面量——WS 传输、连接池与握手定型点都不含版本号。
# 版本化的符号名按形状匹配而不写死版本号：判据若写成 Codex0145，升级后新增的
# Codex0146 文件就不会被认定为出站定型面，台账会在最需要复算的时刻悄悄漏登。
SURFACE_RE = re.compile(
    r"officialCodex|OfficialCodex|[Cc]odex\d{3,}"
    r"|OpenAIOfficialEgress|officialEgressWebSocket"
    r"|compiledOfficialEgressHTTPClient|officialEgressHTTPClient"
    r"|officialOpenAIHTTPBodyContract|OfficialEgressTransportWebSocket"
)

# 命中判据但按 §1.1 不属于本版规则范围的文件。每条必须写明依据。
# 当前只有扫描器分类表会因“以字符串引用被扫描符号”而误中；生产 Anthropic 与
# API Key mimic 路径仍由判据自身区分，不靠排除表隐藏。
SCOPE_EXCLUSIONS: dict[str, str] = {
    # 扫描工具自身：分类规则里出现 officialCodexFileUploadCall 等符号是作为
    # **被扫描对象的名字**，不是参与出站定型。把它登记进 §3.5 台账反而会让
    # 「台账 = 出站定型面」这个语义失真。
    "backend/cmd/egressscan/classify.go": "egressscan 分类规则表，符号为被扫描对象名称，不参与出站定型",
    # 画像枚举生成器是离线 main 包，只把 schema 名称写入生成源码；生产 server
    # 不导入 cmd 包。其余 dump 命令当前未命中判据，不制造空豁免。
    "backend/cmd/egressprofiledump/genenums.go": "离线枚举生成器 main 包，不进入生产 server 二进制",
}


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


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def changeset6_additions(inventory_raw: bytes) -> list[dict[str, str]]:
    """读取变更集 6 增量面；变更集 5 的 52 面历史清单保持只读。"""

    try:
        transition = json.loads(CHANGESET6_TRANSITION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取变更集 6 出站面 transition：{exc}") from exc
    if (
        transition.get("schema_version") != "changeset6-egress-surface-transition/v1"
        or transition.get("changeset") != "6"
        or transition.get("base_inventory_path")
        != "docs/changeset5/egress-surface-inventory.json"
        or transition.get("base_inventory_sha256") != sha256(inventory_raw)
        or transition.get("removals") != []
    ):
        raise RuntimeError("变更集 6 出站面 transition schema、基线绑定或 removals 非法")
    additions = transition.get("additions")
    if not isinstance(additions, list) or transition.get("addition_count") != len(additions):
        raise RuntimeError("变更集 6 出站面 transition additions 或计数非法")
    paths = [item.get("path") for item in additions if isinstance(item, dict)]
    if len(paths) != len(additions) or paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("变更集 6 出站面 transition 路径为空、未排序或重复")
    for item in additions:
        if item.get("file_type") != "regular" or not item.get("reason"):
            raise RuntimeError(f"变更集 6 出站面 transition 条目非法：{item!r}")
    if transition.get("resulting_surface_count") != 52 + len(additions):
        raise RuntimeError("变更集 6 出站面 transition 结果计数非法")
    return additions


def maintenance_removals(frozen_paths: set[str]) -> list[str]:
    """读取本次退休收据，只允许删除收据绑定且确已不存在的历史出站面。"""

    raw = MAINTENANCE_RETIREMENT.read_bytes()
    if sha256(raw) != MAINTENANCE_RETIREMENT_SHA256:
        raise RuntimeError("官方出站维护退休收据摘要漂移")
    receipt = json.loads(raw)
    if receipt.get("schema_version") != "official-egress-consolidation-retirement/v1":
        raise RuntimeError("官方出站维护退休收据 schema 非法")
    retired = receipt.get("retired_production_sources")
    if not isinstance(retired, list):
        raise RuntimeError("官方出站维护退休收据缺少生产源码清单")
    removals = sorted(
        item.get("path")
        for item in retired
        if isinstance(item, dict) and item.get("path") in frozen_paths
    )
    expected = ["backend/internal/service/official_egress_legacy_dispatch.go"]
    if removals != expected:
        raise RuntimeError(f"维护出站面退休集合漂移：{removals!r}")
    for path in removals:
        if (ROOT / path).exists():
            raise RuntimeError(f"已退休出站面仍存在：{path}")
    return removals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-inventory",
        action="store_true",
        help="按当前受审扫描结果写入变更集 5 的结构化完整性清单",
    )
    args = parser.parse_args()

    ledger = ledger_text()
    surface = scan_surface_files()

    if args.write_inventory:
        authoritative = [rel for rel in surface if rel not in SCOPE_EXCLUSIONS]
        payload = {
            "schema_version": "changeset5-egress-surface-inventory/v1",
            "changeset": "5",
            "classification_upstream_base": "26d894ef4f50645a4bf1030e378ac892f17d0223",
            "observed_remote_head": "825ca7b1fc9335f904bc077f051de815fb61e47f",
            "surface_count": len(authoritative),
            "surfaces": [
                {"path": rel, "file_type": "regular"} for rel in authoritative
            ],
            "exclusions": [
                {"path": rel, "reason": reason}
                for rel, reason in sorted(SCOPE_EXCLUSIONS.items())
            ],
        }
        INVENTORY.parent.mkdir(parents=True, exist_ok=True)
        INVENTORY.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"✅ 已写入 {len(authoritative)} 面结构化清单：{INVENTORY}")
        return 0

    try:
        inventory_raw = INVENTORY.read_bytes()
        payload = json.loads(inventory_raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"🔴 无法读取结构化出站面清单：{exc}", file=sys.stderr)
        return 1
    if payload.get("schema_version") != "changeset5-egress-surface-inventory/v1":
        print("🔴 结构化出站面清单 schema 非法", file=sys.stderr)
        return 1
    declared = payload.get("surfaces")
    exclusions = payload.get("exclusions")
    if not isinstance(declared, list) or not isinstance(exclusions, list):
        print("🔴 结构化出站面清单缺少 surfaces/exclusions 数组", file=sys.stderr)
        return 1
    declared_paths = [item.get("path") for item in declared if isinstance(item, dict)]
    exclusion_paths = [item.get("path") for item in exclusions if isinstance(item, dict)]
    if (
        len(declared_paths) != len(declared)
        or len(exclusion_paths) != len(exclusions)
        or len(set(declared_paths + exclusion_paths)) != len(declared_paths) + len(exclusion_paths)
    ):
        print("🔴 结构化出站面清单存在空路径或重复路径", file=sys.stderr)
        return 1
    if payload.get("surface_count") != 52 or len(declared_paths) != 52:
        print("🔴 变更集 5 的完整出站面必须严格为 52 项", file=sys.stderr)
        return 1
    try:
        additions = changeset6_additions(inventory_raw)
    except RuntimeError as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1
    addition_paths = [item["path"] for item in additions]
    if set(addition_paths) & (set(declared_paths) | set(exclusion_paths)):
        print("🔴 变更集 6 出站面 transition 与历史清单重复", file=sys.stderr)
        return 1
    declared.extend(additions)
    declared_paths.extend(addition_paths)
    try:
        removal_paths = maintenance_removals(set(declared_paths))
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1
    declared = [item for item in declared if item["path"] not in set(removal_paths)]
    declared_paths = [path for path in declared_paths if path not in set(removal_paths)]
    if set(exclusion_paths) != set(SCOPE_EXCLUSIONS):
        print("🔴 工具自引用排除项与源码门禁不一致", file=sys.stderr)
        return 1
    scanned = set(surface)
    frozen = set(declared_paths) | set(exclusion_paths)
    if scanned != frozen:
        for rel in sorted(scanned - frozen):
            print(f"🔴 出现未登记的新出站定型文件：{rel}", file=sys.stderr)
        for rel in sorted(frozen - scanned):
            print(f"🔴 已冻结出站定型文件不再命中扫描：{rel}", file=sys.stderr)
        return 1
    for item in declared:
        rel = item["path"]
        path = ROOT / rel
        if item.get("file_type") != "regular" or not path.is_file() or path.is_symlink():
            print(f"🔴 出站定型路径不是已登记的普通文件：{rel}", file=sys.stderr)
            return 1

    missing: list[str] = []
    for rel in declared_paths:
        # 只接受完整仓库相对路径；basename 兜底会让同名文件错误借用登记。
        if rel in ledger:
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

    covered = len(declared_paths)
    print(
        f"✅ §3.5 台账完整：变更集 5 冻结 52 面 + 变更集 6 增量 {len(additions)} 面"
        f" - 维护退休 {len(removal_paths)} 面 = {covered} 个出站定型文件全部登记"
        + (f"，另有 {len(SCOPE_EXCLUSIONS)} 个工具自引用排除" if SCOPE_EXCLUSIONS else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
