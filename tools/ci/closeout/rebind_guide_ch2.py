#!/usr/bin/env python3
"""【可选组 --with-part2，现行流程已作废】指南第二章摘要连锁重绑：把受管清单里绑定的“旧第二章摘要”换成“当前指南第二章摘要”，并重绑检查器的冻结画像摘要。

状态：现行流程里收尾不再改第二部分（evidence_index 改读指南 4.9 小节），本脚本只在 ``--with-part2``（第二部分被改过）时由
regen_after_part2.sh 调用；逻辑保留，不删除。

做什么
    指南第二部分（第二章）改过之后，摘要随之变化；受管清单（场景清单的 source_spec.sha256、期望清单的 source_spec_sha256）
    和两个检查器常量 FROZEN_PROFILE_SHA256（candidate_rule_assertion.py、candidate_test_trace.py，绑定各自默认画像文件的整文件摘要）
    都要跟着换。收尾工具 ``tools/codex_closeout.py`` 里有同一套逻辑（source_updates），但它要求“磁盘上是旧指南、传入新指南文本”，
    集成/收尾分支上指南已是新文本，所以这里复刻同一算法：旧摘要取基点提交上的指南第二章摘要，新摘要取工作树当前指南。
做法（与 source_updates 一致）
    1. 遍历 tools/official_client_capture/*.json，source_spec 绑定指南第二章的清单：
         绑定值＝旧摘要 → 只替换这一处摘要（正则唯一命中），其余字节不动；
         绑定值＝新摘要 → 已是最新，跳过；
         其余（历史清单绑历史摘要）→ 必须是指南历史上出现过的第二章摘要且工作树与 HEAD 一致，否则报错（不能批量覆盖漂移）。
    2. 遍历 tools/official_client_capture/*.py，常量 DEFAULT_PROFILE_RELATIVE_PATH 指向被改清单的检查器：
         FROZEN_PROFILE_SHA256 必须等于该清单改前整文件摘要，换成改后整文件摘要。
    3. 载荷审核：每个被改文件与改前比较，断言差异恰为“一行摘要替换”（检查器常量同理），逐项打印；任何文件差异多于一行直接失败。
用法（在要改的工作树根运行；PYTHONDONTWRITEBYTECODE=1）
    python3 -B rebind_guide_ch2.py --base <基点提交>                      # 只报告
    python3 -B rebind_guide_ch2.py --base <基点提交> --apply \\
        --target-version <目标版本> [--note-date YYYY-MM-DD] [--note-tool-name part2_minimal.py]   # 写文件
    --apply 时在每个检查器的常量上方追加一段载荷审核注释，写明第二章摘要新旧值、被改清单份数与整文件摘要新旧值；
    注释里的日期、目标版本、改写工具名来自上面三个选项（--target-version 在 --apply 时必填）。
输出：只报告时打印待改清单与检查器的摘要新旧值；--apply 时原位改写这些文件。
何时用：仅 ``--with-part2``（第二部分被改过之后），在 part2_minimal.py 之后、冻结承接收据与重封链之前。
退出码：0＝已是最新或已写入；1＝报告有待改（未 --apply）；2＝出错。
"""
from __future__ import annotations

import argparse
import ast
import datetime
import difflib
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

GUIDE = "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
FRAGMENTS = {"第二章", "第二部分", "第二部分-规则"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check)


def one_line_diff(before: str, after: str, label: str) -> str:
    """断言 before→after 恰好是一行替换，返回变化摘要。"""
    old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    changes = [d for d in difflib.ndiff(old_lines, new_lines) if d[:2] in ("- ", "+ ")]
    if len(changes) != 2 or changes[0][:2] != "- " or changes[1][:2] != "+ ":
        raise SystemExit(f"载荷审核失败：{label} 的差异不是恰好一行替换（{len(changes)} 行变化）")
    return f"{label}：1 行替换"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True, help="基点提交（旧指南第二章摘要取自它）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--target-version", default="", help="目标版本，写进载荷审核注释（--apply 时必填）")
    parser.add_argument("--note-date", default="", help="载荷审核注释里的日期 YYYY-MM-DD，缺省取本机今天")
    parser.add_argument("--note-tool-name", default="part2_minimal.py", help="载荷审核注释里写的第二部分改写工具名")
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    from tools import codex_closeout as cc

    base = git(root, "rev-parse", args.base).stdout.strip()
    base_sha = cc.part2_sha(git(root, "show", f"{base}:{GUIDE}").stdout)
    new_sha = cc.part2_sha((root / GUIDE).read_text())
    # 清单里实际绑着的第二章摘要：连锁可能已做过一次（例如集成阶段做过，收尾又改了第二部分），这时旧摘要是上一次连锁的值，不是基点的值。
    import collections
    bound_values = collections.Counter()
    for candidate in sorted((root / "tools/official_client_capture").glob("*.json")):
        try:
            document = json.loads(candidate.read_text())
        except ValueError:
            continue
        if not isinstance(document, dict):
            continue
        src = document.get("source_spec")
        if isinstance(src, dict) and src.get("path") == GUIDE and src.get("fragment") in FRAGMENTS:
            bound_values[src.get("sha256")] += 1
        elif isinstance(src, str) and src in {GUIDE + "#" + f for f in FRAGMENTS}:
            bound_values[document.get("source_spec_sha256")] += 1
    others = [(value, count) for value, count in bound_values.most_common() if value != new_sha]
    old_sha = others[0][0] if others else base_sha
    print(f"基点 {base[:12]} 指南第二章摘要：{base_sha}")
    print(f"清单当前绑定的旧摘要（最多数）：{old_sha}（{others[0][1] if others else 0} 份）")
    print(f"当前指南第二章摘要：          {new_sha}")
    if old_sha == new_sha or not others:
        print("第二章没有变化（清单都已绑当前摘要），无需连锁。")
        return 0
    if bound_values.get(new_sha, 0) > others[0][1]:
        # 幂等保护（10-30 收尾踩到）：连锁已做过时，剩下的“最多数旧摘要”只是更早版本（如 0.145.0）故意保留的历史摘要，绝不能再当成要替换的旧摘要
        print(f"连锁已完成：{bound_values[new_sha]} 份清单已绑当前摘要，多于任何其它值（最多 {others[0][1]} 份，历史清单故意保留），不再改动。")
        return 0

    directory = root / "tools/official_client_capture"
    history = None
    manifest_changes: dict[str, tuple[str, str]] = {}  # 相对路径 → (改前文本, 改后文本)
    already = 0
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text())
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        source = value.get("source_spec")
        if isinstance(source, dict) and source.get("path") == GUIDE and source.get("fragment") in FRAGMENTS:
            bound = source.get("sha256")
            pattern = r'("source_spec"\s*:\s*\{[^{}]*?"sha256"\s*:\s*")' + "@@SHA@@" + r'(")'
        elif isinstance(source, str) and source in {GUIDE + "#" + f for f in FRAGMENTS}:
            bound = value.get("source_spec_sha256")
            pattern = r'("source_spec_sha256"\s*:\s*")' + "@@SHA@@" + r'(")'
        else:
            continue
        if bound == new_sha:
            already += 1
            continue
        relative = path.relative_to(root).as_posix()
        if bound != old_sha:
            # 历史清单绑历史摘要：必须是指南历史上出现过的第二章摘要，且工作树与 HEAD 一致
            if history is None:
                history = set()
                for commit in git(root, "log", "--format=%H", "--", GUIDE).stdout.splitlines():
                    shown = git(root, "show", f"{commit}:{GUIDE}", check=False)
                    if shown.returncode == 0 and cc.PART2 in shown.stdout:
                        history.add(cc.part2_sha(shown.stdout))
            committed = git(root, "show", f"HEAD:{relative}", check=False)
            if bound not in history or committed.returncode or committed.stdout != path.read_text():
                print(f"错误：{relative} 绑定了既无历史依据也非基点摘要的值 {bound}，不能批量覆盖", file=sys.stderr)
                return 2
            continue
        before = path.read_text()
        after, count = re.subn(pattern.replace("@@SHA@@", re.escape(old_sha)), lambda m: m[1] + new_sha + m[2], before)
        if count != 1:
            print(f"错误：{relative} 的来源摘要字段不能唯一定位（命中 {count} 处）", file=sys.stderr)
            return 2
        manifest_changes[relative] = (before, after)

    # 检查器常量：画像文件改前/改后的整文件摘要
    checker_changes: dict[str, tuple[str, str, str, str]] = {}  # 相对路径 → (改前文本, 改后文本, 旧画像摘要, 新画像摘要)
    for path in sorted(directory.glob("*.py")):
        text = path.read_text()
        constants = {}
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"DEFAULT_PROFILE_RELATIVE_PATH", "FROZEN_PROFILE_SHA256"}:
                    try:
                        constants[node.targets[0].id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
        profile = constants.get("DEFAULT_PROFILE_RELATIVE_PATH")
        if profile not in manifest_changes or "FROZEN_PROFILE_SHA256" not in constants:
            continue
        before_text, after_text = manifest_changes[profile]
        old_profile, new_profile = sha(before_text.encode()), sha(after_text.encode())
        if constants["FROZEN_PROFILE_SHA256"] != old_profile:
            print(f"错误：{path.name} 的 FROZEN_PROFILE_SHA256 不等于 {profile} 改前摘要", file=sys.stderr)
            return 2
        pattern = r'(\bFROZEN_PROFILE_SHA256\s*=\s*\(?\s*[\'"])' + old_profile + r'([\'"])'
        changed, count = re.subn(pattern, lambda m: m[1] + new_profile + m[2], text)
        if count != 1:
            print(f"错误：{path.name} 的摘要常量不能唯一定位", file=sys.stderr)
            return 2
        checker_changes[path.relative_to(root).as_posix()] = (text, changed, old_profile, new_profile)

    historical = sum(count for value, count in bound_values.items() if value not in (new_sha, old_sha))
    if historical:
        # 更早版本的清单绑着历史摘要是正常的（上面的历史核对已保证那些摘要出现在指南历史里、文件与 HEAD 一致），只提示数量
        print(f"提示：另有 {historical} 份历史清单绑着更早的第二章摘要，保持原样（已核对出现在指南历史里）")
    print(f"已绑新摘要 {already} 份；待改清单 {len(manifest_changes)} 份、检查器 {len(checker_changes)} 个")
    for relative, (before, after) in manifest_changes.items():
        print("  清单", one_line_diff(before, after, relative))
    for relative, (before, after, old_p, new_p) in checker_changes.items():
        print("  检查器", one_line_diff(before, after, relative), f"：{old_p[:12]}… → {new_p[:12]}…")
    if not manifest_changes:
        print("没有待改项。")
        return 0
    if not args.apply:
        print("（未写文件；加 --apply 才会改）")
        return 1
    if not args.target_version:
        print("错误：--apply 需要 --target-version（写进载荷审核注释）", file=sys.stderr)
        return 2
    note_date = args.note_date or datetime.date.today().isoformat()
    for relative, (_before, after) in manifest_changes.items():
        (root / relative).write_text(after)
    for relative, (_before, after, old_p, new_p) in checker_changes.items():
        # 载荷审核注释：在常量上方留一条说明，写明第二章摘要、被绑定清单的差异（脚本已断言恰为一行替换）与整文件摘要新旧值
        marker = "FROZEN_PROFILE_SHA256 = (\n"
        if after.count(marker) != 1:
            raise SystemExit(f"错误：{relative} 的 FROZEN_PROFILE_SHA256 赋值写法不唯一，无法追加审核注释")
        profile_name = next(r for r in manifest_changes if sha(manifest_changes[r][0].encode()) == old_p)
        note = (
            f"# {note_date}（{args.target_version} 收尾）：指南第二部分开头段补写当前 Active 与 {args.target_version} 官方样本 Campaign（{args.note_tool_name}，只改开头段、不动规则正文），第二章摘要由 {old_sha[:8]}… 变为 {new_sha[:8]}…，\n"
            f"# {len(manifest_changes)} 份场景／期望清单的 source_spec 摘要随之重绑（脚本断言每份差异恰为一行替换）；本常量绑定的 {Path(profile_name).name}\n"
            f"# 载荷审核：与改前逐行比较仅 source_spec 摘要一行不同，规则、场景、选择器与判据逐字不变。整文件摘要 {old_p[:8]}… 变为 {new_p[:8]}…。\n"
        )
        (root / relative).write_text(after.replace(marker, note + marker, 1))
    print("已写入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
