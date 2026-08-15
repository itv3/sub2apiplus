#!/usr/bin/env python3
"""校验第二部分 53 个编号项的格式与分类。

第一、第三部分属于独立内容，本脚本既不解析也不改写；每个编号项必须使用统一
六字段模板。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

SPEC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"

HEAD_RE = re.compile(
    r"^### (SPEC-[A-Z0-9]+-\d+)(?:\s*[~/]\s*(\d+))?\s+(.+)$",
    re.M,
)
ANY_HEAD_RE = re.compile(r"^#{1,6} \S", re.M)
LEVEL_RE = re.compile(r"\b(L[1-4])\b(?=[^\[\]\n]*\])")
FIELD_RE = re.compile(
    r"^- \*\*(范围|规则|机制|记录|源码|实测|实现|状态)\*\*："
    r"(.*?)(?=^- \*\*(?:范围|规则|机制|记录|源码|实测|实现|状态)\*\*：|\Z)",
    re.M | re.S,
)

COLS = ["✅ 验过", "🟡 部分", "⛔ 验不了", "⚠ 未验"]

VISIBLE_RULES = {
    "SPEC-TLS-001", "SPEC-TLS-002", "SPEC-TLS-003",
    "SPEC-PROTO-001", "SPEC-PROTO-002", "SPEC-CONN-001",
    "SPEC-H1-001", "SPEC-H1-002", "SPEC-H1-003", "SPEC-H1-004",
    "SPEC-H2-001", "SPEC-H2-002", "SPEC-H2-003", "SPEC-H2-004",
    "SPEC-H2-005", "SPEC-H2-006", "SPEC-H2-007",
    "SPEC-WS-001", "SPEC-WS-002", "SPEC-WS-003", "SPEC-WS-004", "SPEC-WS-005",
    "SPEC-HDR-002", "SPEC-HDR-004", "SPEC-HDR-005", "SPEC-HDR-006",
    "SPEC-HDR-007", "SPEC-HDR-008",
    "SPEC-BODY-001", "SPEC-BODY-002", "SPEC-BODY-003", "SPEC-BODY-005",
    "SPEC-BODY-006",
    "SPEC-EP-001", "SPEC-EP-002", "SPEC-EP-005", "SPEC-EP-006",
    "SPEC-EP-007", "SPEC-EP-008", "SPEC-EP-009", "SPEC-EP-012",
    "SPEC-EP-013", "SPEC-EP-014", "SPEC-EP-015", "SPEC-EP-019",
    "SPEC-EP-020", "SPEC-EP-021", "SPEC-EP-022",
}
MECHANISM_ITEMS = {"SPEC-HDR-001", "SPEC-BODY-004", "SPEC-EP-023"}
EVIDENCE_RECORDS = {"SPEC-BODY-007", "SPEC-EP-024"}

CA_RULES = {
    "SPEC-TLS-002", "SPEC-H2-001", "SPEC-H2-002", "SPEC-H2-003",
    "SPEC-H2-004", "SPEC-H2-005", "SPEC-H2-006", "SPEC-H2-007",
}
CUSTOM_PROVIDER_RULES = {"SPEC-WS-003"}
OAUTH_RULES = VISIBLE_RULES - CA_RULES - CUSTOM_PROVIDER_RULES

HISTORY_RE = re.compile(
    r"历史样本|旧样本|原文|原状态|原结论|迁移结果|曾经|早前|后来|已删除"
)


def second_part(text: str) -> str:
    """只返回“第二部分 Codex CLI 客户端规则画像”，并拒绝缺失或重复的章节边界。"""
    starts = list(
        re.finditer(r"^# 第二部分 Codex CLI 客户端规则画像\s*$", text, re.M)
    )
    if len(starts) != 1:
        raise ValueError(
            "“# 第二部分 Codex CLI 客户端规则画像”应恰好出现 1 次，"
            f"实际 {len(starts)} 次"
        )
    start = starts[0].start()
    end_match = re.search(r"^# 第三部分(?:\s|$)", text[starts[0].end():], re.M)
    if end_match is None:
        raise ValueError("缺少第二部分之后的“# 第三部分”边界")
    end = starts[0].end() + end_match.start()
    return text[start:end]


def rule_kind(sid: str) -> str | None:
    if sid in VISIBLE_RULES:
        return "W"
    if sid in MECHANISM_ITEMS:
        return "M"
    if sid in EVIDENCE_RECORDS:
        return "E"
    return None


def rule_scope(sid: str) -> str | None:
    if sid in OAUTH_RULES:
        return "OAUTH"
    if sid in CA_RULES:
        return "CA"
    if sid in CUSTOM_PROVIDER_RULES:
        return "CUSTOM_PROVIDER"
    return None


def expanded_ids(label: str) -> list[str]:
    if "~" not in label:
        return [label]
    start, end = label.split("~", 1)
    prefix, first = start.rsplit("-", 1)
    return [
        f"{prefix}-{number:0{len(first)}d}"
        for number in range(int(first), int(end) + 1)
    ]


def _rule_sections(text: str):
    rules_text = second_part(text)
    heads = list(HEAD_RE.finditer(rules_text))
    all_heads = [match.start() for match in ANY_HEAD_RE.finditer(rules_text)]
    for match in heads:
        stop = next((pos for pos in all_heads if pos > match.start()), len(rules_text))
        yield match, rules_text[match.start():stop]


def _fields(body: str) -> dict[str, str]:
    return {
        match.group(1): re.sub(r"\s+", " ", match.group(2)).strip()
        for match in FIELD_RE.finditer(body)
    }


def parse(text: str):
    """返回 evidence_index 依赖的兼容元组。

    元组为（编号、条数、证据来源、验证状态、标题）。新格式禁止范围标题，因此
    条数恒为 1。
    """
    out = []
    for match, body in _rule_sections(text):
        sid, end, title = match.group(1), match.group(2), match.group(3).strip()
        label = f"{sid}~{end}" if end else sid
        levels = set(LEVEL_RE.findall(body))
        origin = "源码读出（L1/L2）" if levels & {"L1", "L2"} else "只能实测（L3）"
        status_text = _fields(body).get("状态", "")
        if status_text.startswith("✅"):
            status = COLS[0]
        elif status_text.startswith("🟡"):
            status = COLS[1]
        elif status_text.startswith("⛔"):
            status = COLS[2]
        else:
            status = COLS[3]
        out.append((label, 1, origin, status, title))
    return out


def status_texts(text: str) -> dict[str, str]:
    """返回第二部分各编号项的完整状态文本。"""
    return {
        match.group(1): _fields(body).get("状态", "")
        for match, body in _rule_sections(text)
    }


def render_summary(items) -> str:
    """根据第二部分逐项状态生成分类总览，用于只读漂移校验。"""
    status_counts = {
        scope: {status: 0 for status in COLS}
        for scope in ("OAUTH", "CA", "CUSTOM_PROVIDER")
    }
    for label, _, _, status, _ in items:
        for sid in expanded_ids(label):
            scope = rule_scope(sid)
            if scope:
                status_counts[scope][status] += 1

    oauth = status_counts["OAUTH"]
    ca = status_counts["CA"]
    custom = status_counts["CUSTOM_PROVIDER"]
    total = (
        len(OAUTH_RULES)
        + len(CA_RULES)
        + len(CUSTOM_PROVIDER_RULES)
        + len(MECHANISM_ITEMS)
        + len(EVIDENCE_RECORDS)
    )
    verified_oauth = oauth[COLS[0]]
    required_oauth = len(OAUTH_RULES)
    mechanism_total = len(MECHANISM_ITEMS)
    required_alignment = required_oauth + mechanism_total
    return f"""<!-- SPEC_STATUS_START -->
| 分组 | 条数 | 当前验证状态 | Sub2API 需对齐项 |
|---|---:|---|---:|
| **① 内置 OpenAI OAuth 可见规则** | **{required_oauth}** | ✅ {verified_oauth}；🟡 {oauth[COLS[1]]} | **{required_oauth}** |
| **② 自定义 CA 条件分支** | **{len(CA_RULES)}** | ✅ {ca[COLS[0]]}；🟡 {ca[COLS[1]]} | **0** |
| **③ 自定义 provider 条件分支** | **{len(CUSTOM_PROVIDER_RULES)}** | ✅ {custom[COLS[0]]}；🟡 {custom[COLS[1]]} | **0** |
| **④ 派生／内部机制说明** | **{mechanism_total}** | 源码机制 | **{mechanism_total}** |
| **⑤ 采集与观测记录** | **{len(EVIDENCE_RECORDS)}** | 观测记录 | **0** |
| **合计** | **{total}** | — | **{required_alignment}** |
<!-- SPEC_STATUS_END -->"""


def validate_taxonomy(items) -> None:
    ids = [sid for label, *_ in items for sid in expanded_ids(label)]
    duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
    if duplicates:
        raise ValueError(f"文档存在重复编号：{duplicates}")
    expected = VISIBLE_RULES | MECHANISM_ITEMS | EVIDENCE_RECORDS
    actual = set(ids)
    if actual != expected:
        raise ValueError(
            f"编号集合不闭合：缺少={sorted(expected - actual)}，"
            f"多出={sorted(actual - expected)}"
        )
    scopes = OAUTH_RULES | CA_RULES | CUSTOM_PROVIDER_RULES
    if scopes != VISIBLE_RULES:
        raise ValueError(
            f"可见规则范围不闭合：缺少={sorted(VISIBLE_RULES - scopes)}，"
            f"多出={sorted(scopes - VISIBLE_RULES)}"
        )
    if (OAUTH_RULES & CA_RULES) or (OAUTH_RULES & CUSTOM_PROVIDER_RULES) or (
        CA_RULES & CUSTOM_PROVIDER_RULES
    ):
        raise ValueError("可见规则范围分类重叠")


def validate_format(text: str) -> list[str]:
    errors: list[str] = []
    try:
        items = parse(text)
        sections = list(_rule_sections(text))
        rules_only = second_part(text)
    except ValueError as exc:
        return [str(exc)]
    try:
        validate_taxonomy(items)
    except ValueError as exc:
        errors.append(str(exc))

    summary_matches = list(re.finditer(
        r"<!-- SPEC_STATUS_START -->.*?<!-- SPEC_STATUS_END -->",
        rules_only,
        re.S,
    ))
    if len(summary_matches) != 1:
        errors.append(f"第二部分分类总览应恰好出现 1 次，实际 {len(summary_matches)} 次")
    elif summary_matches[0].group(0) != render_summary(items):
        errors.append("第二部分分类总览与逐项状态不一致")

    for match, body in sections:
        sid, end = match.group(1), match.group(2)
        if end:
            errors.append(f"{sid} 仍使用范围标题；53 个编号必须逐项独立")
        matches = list(FIELD_RE.finditer(body))
        labels = [field.group(1) for field in matches]
        duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
        if duplicate_labels:
            errors.append(f"{sid} 字段重复：{duplicate_labels}")
        expected_core = {"范围", "源码", "实测", "实现", "状态"}
        missing = expected_core - set(labels)
        if missing:
            errors.append(f"{sid} 缺字段：{sorted(missing)}")

        content_labels = {"规则", "机制", "记录"} & set(labels)
        expected_label = {"W": "规则", "M": "机制", "E": "记录"}.get(rule_kind(sid))
        if content_labels != {expected_label}:
            errors.append(
                f"{sid} 内容字段应为 {expected_label}，实际为 {sorted(content_labels)}"
            )

        if rule_kind(sid) == "W":
            status = _fields(body).get("状态", "")
            if not status.startswith(("✅", "🟡")):
                errors.append(f"{sid} 可见规则状态必须显式以 ✅ 或 🟡 开头")

    stale = sorted(set(HISTORY_RE.findall(rules_only)))
    if stale:
        errors.append(f"规则正文仍含历史叙述：{stale}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="兼容参数；脚本始终只读")
    parser.parse_args()

    text = SPEC.read_text(encoding="utf-8")
    errors = validate_format(text)
    if errors:
        for error in errors:
            print(f"❌ {error}", file=sys.stderr)
        return 1

    print("✅ 第二部分：53 个编号、统一模板与分类一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
