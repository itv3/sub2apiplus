#!/usr/bin/env python3
"""收尾 b8a：生成并更新指南第四部分「## 4.9 当前 Active 终态收据坐标」小节的五行值表。

做什么
    ``tools/evidence_index.py`` 重建 ``docs/EVIDENCE_INDEX.md`` 时，要求指南第四部分 4.9 小节同时写明：Active 版本号、终态收据文件名、
    官方证据来源 Campaign 与末级（canonical）Campaign，缺一项失败关闭（以前读的是冻结的第二部分，每轮收尾为此改冻结章，
    要付第二章摘要连锁；改到 4.9 以后收尾不碰第二部分）。本脚本按新终态事实（或终态收据）把 4.9 小节的值表换成新 Active 的值：

        | 项 | 当前值（<目标版本>） |
        |---|---|
        | Active／Previous | `codex-cli <目标>`／`codex-cli <基线>` |
        | 终态收据 | [`<收据文件名>`](egress/maintenance/<收据文件名>) |
        | 审计索引 | [`<审计索引文件名>`](egress/maintenance/<审计索引文件名>)（私有归档坐标取自它） |
        | 官方证据来源 Campaign | `<官方来源 Campaign>`（attempt `<官方 attempt>`） |
        | 末级（canonical）Campaign | `<末级 Campaign>` |

    表以外的小节文字一个字不动。找不到小节、小节标题不唯一、表头或行标签与约定不符（有人手工改过表结构）都直接报错，不去猜。
输入
    update_active_receipt.py --guide <指南文件> --facts <终态事实 json 或终态收据 json> --official-attempt <官方 attempt id> \\
        (--in-place | --output <新文件>) [--receipt-name <收据文件名>] [--verify-repo <仓库根>]
    * ``--facts`` 里取 ``target.version``／``target.previous_version``、``campaign_chain``（role=official_evidence_source 的那一项，以及最后一项
      作为末级）、``audit_index.path``（取文件名）；终态事实（gen_terminal_facts.py 的输出）与终态收据都带这些字段；
    * 收据文件名缺省由审计索引文件名推出（把 ``_AUDIT_INDEX.json`` 换成 ``_TERMINAL_STATE_RECEIPT.json``，两者同前缀）；
    * ``--verify-repo``：更新后调用该仓库 ``tools/evidence_index.py`` 的 ``active_campaign_evidence``（与重建索引同一校验）核对更新结果；
      该仓库里必须已有 Active 的终态收据（收尾时由 b8a 临时放入候选收据）。校验函数不存在（evidence_index 还是旧版）时报“跳过校验”并退出码 3。
输出
    更新后的指南（原位或写到 --output，已存在则拒绝覆盖）；打印“已更新”或“4.9 小节值表已是最新”。
何时用
    b8a-active-receipt 步骤（b7 之后、b8 之前）：先在 ``${FACTS}`` 下产生带新 4.9 表的指南副本，b8 临时借用它重建证据索引，b9 以它为输入渲染最终指南。
    4.9 是第四部分的内容，不被任何摘要钉住；指南本身是冻结路径，最终由收尾 prepare 的冻结承接收据覆盖，不需要另做第二章摘要连锁。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

HEADING = "## 4.9 当前 Active 终态收据坐标"
TABLE_HEADER = re.compile(r"^\| 项 \| 当前值（[^）]*） \|$")
TABLE_SEPARATOR = "|---|---|"
# 值表约定的五个行标签，顺序固定（evidence_index 只检查值是否出现，标签顺序由本脚本守住，防止手工改表后被悄悄覆盖）。
ROW_LABELS = ("Active／Previous", "终态收据", "审计索引", "官方证据来源 Campaign", "末级（canonical）Campaign")
EXIT_VERIFY_SKIPPED = 3


class SectionError(Exception):
    """4.9 小节找不到或结构与约定不符。"""


def section_bounds(text: str) -> tuple[int, int]:
    """4.9 小节的（起点, 终点）字符位置：标题行恰好出现一次，止于下一个一、二级标题或文末。与 evidence_index.active_receipt_section 同一判据。"""

    starts = list(re.finditer(r"^" + re.escape(HEADING) + r"[^\n]*$", text, re.M))
    if len(starts) != 1:
        raise SectionError(f"“{HEADING}”应恰好出现 1 次，实际 {len(starts)} 次（指南里没有 4.9 小节？先让 L3 的指南改动并进来）")
    end_match = re.search(r"^#{1,2} ", text[starts[0].end():], re.M)
    end = starts[0].end() + end_match.start() if end_match else len(text)
    return starts[0].start(), end


def coordinates_from_facts(facts: dict, official_attempt: str, receipt_name: str = "") -> dict[str, str]:
    """从终态事实／终态收据取 4.9 值表需要的全部坐标；缺哪一项直接报错。"""

    try:
        version = facts["target"]["version"]
        previous = facts["target"]["previous_version"]
        chain = facts["campaign_chain"]
        audit_name = Path(facts["audit_index"]["path"]).name
    except (KeyError, TypeError) as error:
        raise SectionError(f"终态事实缺少字段 {error}：需要 target.version、target.previous_version、campaign_chain、audit_index.path") from error
    official = [link["campaign_id"] for link in chain if link.get("role") == "official_evidence_source"]
    if len(official) != 1:
        raise SectionError(f"campaign_chain 里 role=official_evidence_source 的项应恰好 1 个，实际 {len(official)} 个（4.9 值表只有一行官方来源，多个请手工处理）")
    if not chain:
        raise SectionError("campaign_chain 为空")
    if not official_attempt:
        raise SectionError("缺少官方 attempt id（--official-attempt）")
    if not audit_name.endswith("_AUDIT_INDEX.json"):
        raise SectionError(f"审计索引文件名不是 *_AUDIT_INDEX.json：{audit_name}")
    receipt = receipt_name or audit_name[: -len("_AUDIT_INDEX.json")] + "_TERMINAL_STATE_RECEIPT.json"
    return {"version": version, "previous": previous, "receipt": receipt, "audit": audit_name, "official": official[0],
            "attempt": official_attempt, "canonical": chain[-1]["campaign_id"]}


def render_table(c: dict[str, str]) -> list[str]:
    """按约定格式生成值表各行（含表头与分隔行）。"""

    return [
        f"| 项 | 当前值（{c['version']}） |",
        TABLE_SEPARATOR,
        f"| {ROW_LABELS[0]} | `codex-cli {c['version']}`／`codex-cli {c['previous']}` |",
        f"| {ROW_LABELS[1]} | [`{c['receipt']}`](egress/maintenance/{c['receipt']}) |",
        f"| {ROW_LABELS[2]} | [`{c['audit']}`](egress/maintenance/{c['audit']})（私有归档坐标取自它） |",
        f"| {ROW_LABELS[3]} | `{c['official']}`（attempt `{c['attempt']}`） |",
        f"| {ROW_LABELS[4]} | `{c['canonical']}` |",
    ]


def update_text(text: str, coords: dict[str, str]) -> tuple[str, bool]:
    """返回（新指南文本, 是否有改动）。只替换 4.9 小节里那张值表。"""

    start, end = section_bounds(text)
    section = text[start:end]
    lines = section.split("\n")
    headers = [i for i, line in enumerate(lines) if TABLE_HEADER.match(line)]
    if len(headers) != 1:
        raise SectionError(f"4.9 小节里值表表头（“| 项 | 当前值（…） |”）应恰好 1 个，实际 {len(headers)} 个")
    first = headers[0]
    if first + 1 >= len(lines) or lines[first + 1] != TABLE_SEPARATOR:
        raise SectionError("值表表头后面不是分隔行 |---|---|")
    last = first + 2
    while last < len(lines) and lines[last].startswith("|"):
        last += 1
    labels = tuple(line.split("|")[1].strip() for line in lines[first + 2:last])
    if labels != ROW_LABELS:
        raise SectionError(f"值表的行标签与约定不符：现有 {list(labels)}，约定 {list(ROW_LABELS)}（有人手工改过表结构？先核对再改）")
    new_lines = lines[:first] + render_table(coords) + lines[last:]
    new_section = "\n".join(new_lines)
    new_text = text[:start] + new_section + text[end:]
    # 复核 evidence_index 要求出现的四类值都在小节里（与其判据一致）
    section_after = new_text[start:start + len(new_section)]
    missing = [item for item in (coords["version"], coords["receipt"], coords["official"], coords["canonical"]) if item not in section_after]
    if missing:
        raise SectionError(f"更新后 4.9 小节仍缺 {missing}")
    return new_text, new_text != text


def verify_with_evidence_index(repo: Path, guide_text: str) -> None:
    """调用仓库里 tools/evidence_index.py 的 active_campaign_evidence 校验（与重建证据索引同一判据）；失败抛 ValueError。"""

    module_path = repo / "tools" / "evidence_index.py"
    spec = importlib.util.spec_from_file_location("evidence_index_for_closeout", module_path)
    if spec is None or spec.loader is None:
        raise SectionError(f"加载不了 {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # evidence_index.py 顶部有 ``import spec_status``（按脚本方式运行时 tools/ 在 sys.path 里），这里补上，用完还原
    tools_dir = str(repo / "tools")
    sys.path.insert(0, tools_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(tools_dir)
    if not hasattr(module, "active_receipt_section"):
        raise FileNotFoundError("该仓库的 evidence_index.py 还是旧版（没有 active_receipt_section），校验位置不在 4.9 小节，跳过")
    module.active_campaign_evidence(guide_text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--guide", required=True)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--official-attempt", required=True)
    parser.add_argument("--receipt-name", default="")
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--verify-repo", default="")
    args = parser.parse_args(argv)
    if bool(args.in_place) == bool(args.output):
        print("--in-place 与 --output 必须二选一", file=sys.stderr)
        return 2
    try:
        facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
        coords = coordinates_from_facts(facts, args.official_attempt, args.receipt_name)
        guide = Path(args.guide)
        new_text, changed = update_text(guide.read_text(encoding="utf-8"), coords)
    except SectionError as error:
        print(f"4.9 小节更新失败：{error}", file=sys.stderr)
        return 1
    if args.in_place:
        if changed:
            guide.write_text(new_text, encoding="utf-8")
    else:
        out = Path(args.output)
        if out.exists():
            print(f"输出已存在，拒绝覆盖：{out}", file=sys.stderr)
            return 1
        out.write_text(new_text, encoding="utf-8")
    print(f"4.9 小节值表已更新为 Active {coords['version']}（收据 {coords['receipt']}）" if changed else f"4.9 小节值表已是最新（Active {coords['version']}）")
    if args.verify_repo:
        try:
            verify_with_evidence_index(Path(args.verify_repo), new_text)
        except FileNotFoundError as error:
            print(f"跳过 evidence_index 校验：{error}", file=sys.stderr)
            return EXIT_VERIFY_SKIPPED
        except (ValueError, SectionError) as error:
            print(f"evidence_index 校验未通过：{error}", file=sys.stderr)
            return 1
        print("evidence_index.active_campaign_evidence 校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
