#!/usr/bin/env python3
"""【已作废的可选步骤】收尾：指南第二部分的最小改写——只动开头段的“当前 Active／终态收据／新版官方样本”几句。

状态（重要）
    本脚本只服务 closeout.sh 的可选组 ``--with-part2``（步骤 b2-part2-lead 与 b3-regen），缺省关闭，而且在现行流程里已作废：
    原因是 ``tools/evidence_index.py`` 已改为读指南第四部分「## 4.9 当前 Active 终态收据坐标」小节来校验 Active 终态收据的 Campaign，
    收尾不必再改冻结的第二部分（改第二部分要付第二章摘要连锁：20 份清单＋2 个检查器＋冻结承接收据＋基线重封＋追踪，约 40 分钟，
    还会让受管工具身份变化）。4.9 小节由 ``update_active_receipt.py`` 更新。
    仅当 ``evidence_index.py`` 被回退到读第二部分时才需要启用本脚本；脚本逻辑保留，不删除。

做什么（回退场景下）
    让第二部分开头段写明“当前 Active 是新目标版本、新版终态收据链接、新版官方 VC-1 样本的 Campaign／attempt”，并把几处“本轮”
    改成“<基线版本> 轮”（避免读成新版）；不碰 UA、client_version 等规则正文（它们仍是基线版本官方样本的实测描述）。
    每处替换断言命中恰好一次；已改过再跑会因命中 0 次失败（幂等保护）。改完必须走第二章摘要连锁（regen_after_part2.sh）、
    冻结承接收据与重封链。

内置改写表的适用范围（务必读）
    内置的 5 处“旧文 → 新文”是 **上一轮收尾之前** 的指南措辞（旧文里写着“描述当前生产 active <基线版本> 的规则（previous 为 <退休版本>）”之类）。
    它跑过一次之后指南措辞就变了，下一轮再用会“命中 0 次”——这是预期的：按报错换一份改写表，用 ``--edits-file`` 传入。
    改写表是 JSON 数组 ``[{"old": "...", "new": "..."}, ...]``，``old``／``new`` 里可用占位符：
        {target} {baseline} {retire} {last_retired} {target_short} {baseline_short} {prefix} {prev_prefix}
        {campaign} {attempt} {jobs} {prev_campaign} {prev_jobs} {lite_main_model} {lite_track_model} {rules_total}
    只有模板里实际用到的占位符才要求提供对应参数，缺哪个报哪个。
    内置表里的“全部继承”表述只在 VC-2 迁移清单全部为 inherit（补丁清单为空）的版本成立，启用前先核对。

输入
    在仓库根运行：
    python3 -B part2_minimal.py --target-version <目标> --baseline-version <基线> --retire-version <退休> --campaign <Campaign> \\
        --attempt <官方 attempt id> --jobs <官方 Job 数> [--last-retired-version … --prev-campaign … --prev-jobs … \\
        --lite-main-model … --lite-track-model … --rules-total …] [--edits-file 改写表.json] [--guide 路径]
输出
    原位改写指南文件。
何时用
    仅 ``--with-part2``（回退场景）。现行流程不用。
"""
import argparse
import json
import string
import sys
from pathlib import Path

DEFAULT_GUIDE = "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"

# 内置改写表：上一轮收尾之前的指南措辞 -> 本轮收尾后的措辞。占位符见模块说明。
DEFAULT_EDITS = [
    (
        "描述当前生产 active {baseline} 的规则（previous 为\n{retire}）；生产激活、精确回滚、目标恢复与 {last_retired} Runtime Catalog 退休的机器事实见\n"
        "[`{baseline_short} 终态收据`](egress/maintenance/{prev_prefix}_TERMINAL_STATE_RECEIPT.json)。各条实测中的“{baseline}\n官方样本”来自",
        "描述按 {baseline} 官方样本与 {target} 源码复核写成的规则；当前生产 Active 为 {target}（previous 为\n{baseline}），生产激活、精确回滚、目标恢复与 {retire} Runtime Catalog 退休的机器事实见\n"
        "[`{target_short} 终态收据`](egress/maintenance/{prefix}_TERMINAL_STATE_RECEIPT.json)。{target} 的官方 VC-1 样本来自\n"
        "Campaign `{campaign}` 的 attempt `{attempt}`（{jobs} 个官方 Job 全部 complete），\n"
        "与 {baseline} 基线逐场景比对，线上字节除版本字符串外一致、{rules_total} 条目标规则全部继承，其匹配数本轮没有回填到各规则正文。各条实测中的“{baseline}\n官方样本”来自",
    ),
    (
        "（本轮官方证据全部重新采集，{prev_jobs} 个官方 Job 全部 complete）",
        "（{baseline} 轮官方证据全部重新采集，{prev_jobs} 个官方 Job 全部 complete）",
    ),
    (
        "本轮官方采集的主线（`{lite_main_model}`）与 Lite 轨（`{lite_track_model}`）都是 Lite 模型，非 Lite 分支没有 {baseline}",
        "{baseline} 轮官方采集的主线（`{lite_main_model}`）与 Lite 轨（`{lite_track_model}`）都是 Lite 模型，非 Lite 分支没有 {baseline}",
    ),
    (
        "“候选断言”指 `{prev_campaign}` canonical 链中的 `assert-SPEC-*`（评估基线与 evidence_level 以该链的断言结果为准）。",
        "“候选断言”指 `{prev_campaign}` canonical 链中的 `assert-SPEC-*`（评估基线与 evidence_level 以该链的断言结果为准；"
        "{target} 对应 `{campaign}` 的 canonical 链）。",
    ),
    (
        "{target} 目前只做过源码复核，尚无 {target} 官方样本；",
        "{target} 的官方样本见上，规则正文未回填其匹配数，规则正文里的 {target} 结论以源码复核为准；",
    ),
]

ARG_FOR_PLACEHOLDER = {
    "target": "--target-version", "baseline": "--baseline-version", "retire": "--retire-version", "last_retired": "--last-retired-version",
    "campaign": "--campaign", "attempt": "--attempt", "jobs": "--jobs", "prev_campaign": "--prev-campaign", "prev_jobs": "--prev-jobs",
    "lite_main_model": "--lite-main-model", "lite_track_model": "--lite-track-model", "rules_total": "--rules-total",
}


def tag_of(version):
    """0.149.0 -> 0149；0.149.1 -> 01491（与终态收据文件名一致）。"""
    major, minor, patch = (int(x) for x in version.split("."))
    return f"{major}{minor:03d}{patch if patch else ''}"


def short_of(version):
    return version.rsplit(".", 1)[0]


def placeholders_used(templates):
    """模板里用到的占位符集合。"""
    used = set()
    for text in templates:
        for _lit, field, _spec, _conv in string.Formatter().parse(text):
            if field:
                used.add(field)
    return used


def build_values(args):
    """把命令行参数整理成占位符取值；派生项（标记、前缀）在这里算。"""
    values = {key: getattr(args, key) for key in ARG_FOR_PLACEHOLDER}
    values = {key: value for key, value in values.items() if value not in (None, "")}
    if args.target:
        values["target_short"] = short_of(args.target)
    if args.baseline:
        values["baseline_short"] = short_of(args.baseline)
    if args.target and args.baseline:
        values["prefix"] = f"CODEX_CLI_{tag_of(args.baseline)}_TO_{tag_of(args.target)}"
    if args.baseline and args.retire:
        values["prev_prefix"] = f"CODEX_CLI_{tag_of(args.retire)}_TO_{tag_of(args.baseline)}"
    return values


def apply_edits(text, edits, values):
    """依次应用（旧文, 新文）模板；每个旧文必须恰好命中一次，缺参数时一次性列出全部缺失项。"""
    needed = placeholders_used([item for pair in edits for item in pair])
    missing = sorted(key for key in needed if key not in values)
    if missing:
        hints = [ARG_FOR_PLACEHOLDER.get(key, key) for key in missing]
        raise SystemExit(f"缺少参数：{'、'.join(hints)}（改写表里用到了对应占位符）")
    for old_template, new_template in edits:
        old, new = old_template.format(**values), new_template.format(**values)
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"命中 {count} 次（应为 1）：{old[:60]!r}\n（指南措辞与改写表不一致：按本脚本头部说明换一份 --edits-file）")
        text = text.replace(old, new)
    return text


def load_edits(path):
    if not path:
        return DEFAULT_EDITS
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, dict) and set(item) == {"old", "new"} for item in data):
        raise SystemExit("--edits-file 必须是 [{\"old\": …, \"new\": …}, …] 形式的 JSON 数组")
    return [(item["old"], item["new"]) for item in data]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target-version", dest="target", required=True)
    ap.add_argument("--baseline-version", dest="baseline", required=True)
    ap.add_argument("--retire-version", dest="retire", required=True)
    ap.add_argument("--last-retired-version", dest="last_retired", default=None, help="上一轮收尾退休的版本（旧开头段里写着“<它> Runtime Catalog 退休”）")
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--attempt", required=True)
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--prev-campaign", dest="prev_campaign", default=None, help="基线版本那一轮的 Campaign 全名")
    ap.add_argument("--prev-jobs", dest="prev_jobs", default=None, help="基线版本那一轮的官方 Job 数")
    ap.add_argument("--lite-main-model", dest="lite_main_model", default=None)
    ap.add_argument("--lite-track-model", dest="lite_track_model", default=None)
    ap.add_argument("--rules-total", dest="rules_total", default=None)
    ap.add_argument("--edits-file", default="")
    ap.add_argument("--guide", default=DEFAULT_GUIDE)
    args = ap.parse_args(argv)
    guide = Path(args.guide)
    text = apply_edits(guide.read_text(encoding="utf-8"), load_edits(args.edits_file), build_values(args))
    guide.write_text(text, encoding="utf-8")
    print("已改", guide)
    return 0


if __name__ == "__main__":
    sys.exit(main())
