#!/usr/bin/env python3
"""检查 §3.5 人工合并缝与机器精确台账是否覆盖 Codex/OpenAI 出站定型面。

§5.1 要求每次合并上游后刷新台账。文档只保留少量高风险合并缝；完整路径集合由本脚本
相对冻结 upstream commit 自动复算，并与结构化 JSON 比对，避免继续人工维护几十行路径。

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
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
SCAN_ROOT = ROOT / "backend"
INVENTORY = ROOT / "docs" / "egress" / "consolidation" / "egress-surface-inventory.json"
CHANGESET6_TRANSITION = ROOT / "docs" / "egress" / "validation" / "egress-surface-transition.json"
MAINTENANCE_RETIREMENT = ROOT / "docs" / "egress" / "maintenance" / "official-egress-consolidation-retirement.json"
UPSTREAM_MERGE_LEDGER = ROOT / "docs" / "egress" / "maintenance" / "upstream-v0.1.171-egress-merge-ledger.json"
MAINTENANCE_RETIREMENT_SHA256 = "d60fb470a83f4a98f5de231265d2f695f3963536ec45290b36341c248a56ee36"
CURRENT_UPSTREAM_TAG = "v0.1.171"
CURRENT_UPSTREAM_BASE = "f0e7a9c7a23a7d02fb159b62fa809621eb0475a6"

# 这些文件直接改变 Codex body、namespace、能力、身份、连接或 Guard，但不一定
# 命中 SURFACE_RE。单独冻结必要集合，防止人工表格在保持总数不变时用无关路径替换。
REQUIRED_REVIEW_TOUCHPOINTS = {
    "backend/cmd/server/wire.go",
    "backend/internal/config/config.go",
    "backend/internal/pkg/apicompat/chatcompletions_responses_bridge.go",
    "backend/internal/pkg/apicompat/responses_namespace.go",
    "backend/internal/pkg/httpclient/pool.go",
    "backend/internal/repository/http_upstream.go",
    "backend/internal/service/account.go",
    "backend/internal/service/openai_codex_identity.go",
    "backend/internal/service/openai_codex_transform.go",
    "backend/internal/service/openai_content_session_seed.go",
    "backend/internal/service/openai_cookie_jar.go",
    "backend/internal/service/openai_gateway_request_body.go",
    "backend/internal/service/openai_gateway_service.go",
    "backend/internal/service/openai_model_capabilities.go",
    "backend/internal/service/openai_oauth_service.go",
    "backend/internal/service/openai_responses_lite_tools.go",
    "backend/internal/service/openai_responses_namespace.go",
    "backend/internal/service/openai_ws_forwarder.go",
    "backend/internal/service/openai_ws_protocol_resolver.go",
    "backend/internal/service/official_egress_upstream_identity_bridge.go",
}

# 上游 v0.1.171 新增的身份发现/归一化基础设施不一定直接命中 strict surface 扫描，
# 但它们决定“发现值是否能越权成为 active wire 身份”，必须进入机器合并台账。
IDENTITY_BOUNDARY_TOUCHPOINTS = {
    "backend/internal/handler/admin/setting_handler.go",
    "backend/internal/handler/admin/setting_handler_update.go",
    "backend/internal/handler/dto/settings.go",
    "backend/internal/pkg/openai/request.go",
    "backend/internal/service/domain_constants.go",
    "backend/internal/service/openai_codex_version_sync_service.go",
    "backend/internal/service/setting_gateway_runtime.go",
    "backend/internal/service/setting_parse.go",
    "backend/internal/service/setting_update.go",
    "backend/internal/service/settings_view.go",
    "backend/internal/service/wire.go",
    "frontend/src/api/admin/settings.ts",
    "frontend/src/i18n/locales/en/admin/settings.ts",
    "frontend/src/i18n/locales/zh/admin/settings.ts",
    "frontend/src/views/admin/SettingsView.vue",
}

# 人类文档只需解释这些高风险边界；完整文件闭集由结构化机器台账负责。
HUMAN_REVIEW_SEAMS = {
    "backend/internal/pkg/openai/request.go",
    "backend/internal/service/openai_codex_identity.go",
    "backend/internal/service/openai_codex_version_sync_service.go",
    "backend/internal/service/official_egress_upstream_identity_bridge.go",
    "backend/internal/service/official_egress_identity_authority.go",
    "backend/internal/officialegress/compiler.go",
    "backend/internal/officialegress/executor.go",
    "backend/internal/service/openai_ws_forwarder_payload.go",
    "backend/internal/service/openai_quota_service.go",
    "backend/internal/service/setting_gateway_runtime.go",
    "backend/internal/service/wire.go",
    "frontend/src/views/admin/SettingsView.vue",
}

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


def ledger_subsection(ledger: str, heading: str, next_heading: str) -> str:
    start = ledger.find(heading)
    if start < 0:
        raise RuntimeError(f"未找到台账小节：{heading}")
    end = ledger.find(next_heading, start + len(heading))
    if end < 0:
        raise RuntimeError(f"未找到台账小节结束标记：{next_heading}")
    return ledger[start:end]


def git_path_exists(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def git_path_differs(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "diff", "--quiet", commit, "--", path],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"无法比较 upstream 路径：{path}")
    return result.returncode == 1


def validate_human_ledger(ledger: str) -> None:
    """校验 Markdown 只保留且完整解释高风险合并缝。"""

    review_section = ledger_subsection(ledger, "### 3.5.2", "### 3.5.3")
    missing = sorted(path for path in HUMAN_REVIEW_SEAMS if path not in review_section)
    if missing:
        raise RuntimeError(f"§3.5.2 缺少高风险人工复核缝：{missing}")
    for path in HUMAN_REVIEW_SEAMS:
        if not (ROOT / path).is_file():
            raise RuntimeError(f"§3.5.2 高风险合并缝不是普通文件：{path}")


def current_upstream_merge_entries(surface: list[str]) -> list[dict[str, object]]:
    """生成相对当前 upstream 基线的 Codex 出站 overlay 精确闭集。"""

    strict_surface = set(surface) - set(SCOPE_EXCLUSIONS)
    candidates = strict_surface | REQUIRED_REVIEW_TOUCHPOINTS | IDENTITY_BOUNDARY_TOUCHPOINTS
    entries: list[dict[str, object]] = []
    for path in sorted(candidates):
        if not (ROOT / path).is_file():
            raise RuntimeError(f"机器合并台账候选不是普通文件：{path}")
        if not git_path_differs(CURRENT_UPSTREAM_BASE, path):
            continue
        scopes: list[str] = []
        if path in strict_surface:
            scopes.append("strict_surface")
        if path in REQUIRED_REVIEW_TOUCHPOINTS:
            scopes.append("required_review_touchpoint")
        if path in IDENTITY_BOUNDARY_TOUCHPOINTS:
            scopes.append("identity_boundary")
        entries.append(
            {
                "path": path,
                "source": "upstream" if git_path_exists(CURRENT_UPSTREAM_BASE, path) else "fork",
                "scopes": scopes,
            }
        )
    return entries


def upstream_merge_ledger_payload(entries: list[dict[str, object]]) -> dict[str, object]:
    """构造可精确重放的 upstream overlay 台账。"""

    canonical = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "schema_version": "upstream-egress-merge-ledger/v1",
        "upstream_tag": CURRENT_UPSTREAM_TAG,
        "upstream_commit": CURRENT_UPSTREAM_BASE,
        "generation_rule": "strict_surface ∪ required_review_touchpoint ∪ identity_boundary 中相对 upstream 有差异的普通文件",
        "overlay_count": len(entries),
        "overlay_sha256": sha256(canonical),
        "overlays": entries,
    }


def validate_upstream_merge_ledger(expected: dict[str, object]) -> None:
    """要求结构化台账与当前工作树复算结果逐字段一致。"""

    try:
        actual = json.loads(UPSTREAM_MERGE_LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "无法读取 v0.1.171 机器合并台账；请先运行 "
            "python3 tools/check_ledger_completeness.py --write-upstream-merge-ledger："
            f"{exc}"
        ) from exc
    if actual != expected:
        raise RuntimeError(
            "v0.1.171 机器合并台账与当前源码 overlay 不一致；请重生成后审阅 JSON 差异"
        )


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
    writes = parser.add_mutually_exclusive_group()
    writes.add_argument(
        "--write-inventory",
        action="store_true",
        help="按当前受审扫描结果写入变更集 5 的结构化完整性清单",
    )
    writes.add_argument(
        "--write-upstream-merge-ledger",
        action="store_true",
        help="按当前源码写入相对 v0.1.171 的结构化 Codex 出站 overlay 台账",
    )
    args = parser.parse_args()

    ledger = ledger_text()
    surface = scan_surface_files()

    try:
        validate_human_ledger(ledger)
        upstream_entries = current_upstream_merge_entries(surface)
        upstream_payload = upstream_merge_ledger_payload(upstream_entries)
    except RuntimeError as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1

    if args.write_upstream_merge_ledger:
        UPSTREAM_MERGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        UPSTREAM_MERGE_LEDGER.write_text(
            json.dumps(upstream_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"✅ 已写入 {len(upstream_entries)} 个 v0.1.171 出站 overlay："
            f"{UPSTREAM_MERGE_LEDGER}"
        )
        return 0

    try:
        validate_upstream_merge_ledger(upstream_payload)
    except RuntimeError as exc:
        print(f"🔴 {exc}", file=sys.stderr)
        return 1

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

    stale = [rel for rel in SCOPE_EXCLUSIONS if rel not in surface]

    if stale:
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
        f"；v0.1.171 机器 overlay {len(upstream_entries)} 个文件逐项一致"
        f"；人工只保留 {len(HUMAN_REVIEW_SEAMS)} 个高风险合并缝"
        + (f"，另有 {len(SCOPE_EXCLUSIONS)} 个工具自引用排除" if SCOPE_EXCLUSIONS else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
