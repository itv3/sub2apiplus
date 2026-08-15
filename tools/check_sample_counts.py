#!/usr/bin/env python3
"""核对规格表里标注的样本连接数与归档实况是否一致。

为什么需要这个脚本
------------------
2026-07-29 外部审核发现三处连接数错位：`clean-image` 标 28（实为 3）、
`clean-legacy` 标 97（实为 6）、`clean-search` 标 31（实为 4）。三个错误数字
恰好是被它们取代的 `recap-*` 那批的连接数——**换了运行号，洁净度数字沿用旧的**。

这类错误的特点是"看起来很具体所以没人复查"。`check_spec_refs.py` 查不到它
（那只验源码引用），`spec_status.py` 也查不到（那只按关键词分类）。手工核对
不可持续，故单独立一个脚本。
"""
from __future__ import annotations
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
RAW  = ROOT / "local-analysis/captures/raw-scrubbed"

# 匹配「`运行号`（… N 连接 …）」这类标注。运行号与数字之间允许有任意修饰文字
# （"**洁净样本**："、"⚠ **不洁净**：" 等），但不跨行——跨行会把下一条的数字吃进来。
PAT = re.compile(r"`([A-Za-z][\w.-]{6,})`[^\n`]{0,40}?\*{0,2}(\d+)\*{0,2}\s*连接")


def actual(run: str) -> int | None:
    """归档里的实际连接数。目录名可能带工具前缀，故先精确后前缀匹配。"""
    d = RAW / run / "relay"
    if not d.is_dir():
        # ⚠ 文档里同一次采集有两个名字：原始目录名 `relay-h2-<ts>` 与带工具前缀的
        # `official-relay-h2-relay-h2-<ts>`。只按前缀匹配会漏掉后者，
        # 于是那两处标注被静默"跳过"——看着是 0 错误，其实没检查。
        # 用 ISO 时间戳归一，与 evidence_index.canon_run 同口径。
        ts = re.search(r"(20260\d{3}T\d{6}Z)", run)
        for cand in sorted(RAW.iterdir()) if RAW.is_dir() else []:
            if cand.name == run or cand.name.startswith(run + "-"):
                d = cand / "relay"
                break
            if ts and ts.group(1) in cand.name:
                d = cand / "relay"
                break
    if not d.is_dir():
        return None
    return len(list(d.glob("*.client_to_upstream.bin")))


def main() -> int:
    text = SPEC.read_text(encoding="utf-8")
    bad, ok, skip = [], 0, 0
    for m in PAT.finditer(text):
        run, claimed = m.group(1), int(m.group(2))
        line = text[:m.start()].count("\n") + 1
        got = actual(run)
        if got is None:
            skip += 1
            continue
        if got != claimed:
            bad.append((line, run, claimed, got))
        else:
            ok += 1
    print(f"连接数标注 {ok + len(bad) + skip} 处："
          f"✅ 一致 {ok}   ⏭ 未归档跳过 {skip}   ❌ 不符 {len(bad)}")
    for line, run, claimed, got in bad:
        print(f"  L{line} `{run}`：文档写 {claimed}，归档实为 {got}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
