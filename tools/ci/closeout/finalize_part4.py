#!/usr/bin/env python3
"""收尾 b9 之后：把第四部分“本轮经验”与时间线／实战回填并入 render_guide.py 渲染出的 final-guide.md（原位修改，只动第四部分）。

做什么
    三件事都是“有输入才做、找不到唯一锚点就明确失败”，因为指南里不一定还留着“收尾时回填”的占位符；每件事都断言目标文本恰好出现一次，
    任何一处不符直接失败，不写文件。
      1. --lessons <条目.md>：把“失败矩阵补充（…实测，复核时逐条对照）：”标题的版本串末尾补上目标版本，并把条目并入“#### 暂停种类与放行”之前；
      2. --timeline-line <整句>：
           a) 指南里有占位句“<主.次> 轮实测（按上述口径，收尾时回填）：…”（整行）→ 整行替换；
           b) 没有占位句 → 紧跟在最近一条“X.YYY 轮实测（按上述口径）：…”段落之后另起一段插入（整句由人写、事实逐条核过）；
      3. --partial-manifest-sentence <一句>：把“该流程在真实执行器上…实战未验”里的那一句替换成给定句（找不到唯一锚点就失败）。
    并自检：第二部分字节不变（取第二部分整段比对渲染前后的摘要）。
输入
    finalize_part4.py --guide <final-guide.md> --target <目标版本> [--lessons <条目.md>] [--timeline-line <整句>] [--partial-manifest-sentence <一句>]
    经验条目与时间线整句由调度者按事实撰写（需要判断，不机械生成），预先备好放文件里。
输出
    原位改写 --guide 指向的文件，打印行数变化；仍有“〔收尾回填〕”之类占位时提示。
何时用
    b9-render-guide 渲染出 final-guide.md 之后、b10-sync 之前（b10-sync 优先上传 final-guide-p4.md，即本脚本处理后的副本）。
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--guide", required=True)
ap.add_argument("--lessons", default="")
ap.add_argument("--timeline-line", default="")
ap.add_argument("--partial-manifest-sentence", default="")
ap.add_argument("--target", required=True, help="目标版本，如 0.167.0（用来拼“<主.次> 轮实测”与失败矩阵版本串）")
a = ap.parse_args()

path = Path(a.guide)
text = path.read_text(encoding="utf-8")
before_lines = text.split("\n")
short = a.target.rsplit(".", 1)[0]  # 主.次，如 0.167


def part2(t: str) -> str:
    start = t.index("# 第二部分")
    end = t.index("# 第三部分", start)
    return t[start:end]


digest_before = hashlib.sha256(part2(text).encode()).hexdigest()

# 1. 失败矩阵补充
if a.lessons:
    heads = re.findall(r"(?m)^失败矩阵补充（([0-9./／]+) 实测，复核时逐条对照）：$", text)
    if len(heads) != 1:
        sys.exit("找不到唯一的“失败矩阵补充”标题")
    old_head = f"失败矩阵补充（{heads[0]} 实测，复核时逐条对照）："
    new_list = heads[0] if a.target in heads[0] else f"{heads[0]}／{a.target}"
    text = text.replace(old_head, f"失败矩阵补充（{new_list} 实测，复核时逐条对照）：", 1)
    anchor = "\n\n#### 暂停种类与放行"
    if text.count(anchor) != 1:
        sys.exit("找不到唯一的“暂停种类与放行”标题锚点")
    lessons = Path(a.lessons).read_text(encoding="utf-8").rstrip("\n")
    text = text.replace(anchor, "\n" + lessons + anchor, 1)

# 2. 时间线整句
if a.timeline_line:
    line = a.timeline_line.strip()
    placeholders = re.findall(rf"(?m)^{re.escape(short)} 轮实测（按上述口径，收尾时回填）：.*$", text)
    if len(placeholders) == 1:
        text = text.replace(placeholders[0], line, 1)
    elif len(placeholders) == 0:
        existing = re.findall(rf"(?m)^{re.escape(short)} 轮实测（按上述口径）：.*$", text)
        if existing:
            sys.exit(f"指南里已经有“{short} 轮实测”句，不重复插入（要改请手工）")
        prior = list(re.finditer(r"(?m)^\d+\.\d+ 轮实测（按上述口径）：.*$", text))
        if not prior:
            sys.exit("找不到任何“X.YYY 轮实测（按上述口径）”句作为插入锚点")
        last = prior[-1]
        text = text[:last.end()] + "\n\n" + line + text[last.end():]
    else:
        sys.exit("占位句出现多次")

# 3. 部分完成清单实战结果
if a.partial_manifest_sentence:
    pat = re.compile(r"该流程在真实执行器上未做实战（[^）]*）")
    found = pat.findall(text)
    ph = "该流程在真实执行器上的实战结果〔收尾回填〕；"
    if len(found) == 1:
        text = text.replace(found[0], a.partial_manifest_sentence.strip().rstrip("；;。"), 1)
    elif text.count(ph) == 1:
        text = text.replace(ph, a.partial_manifest_sentence.strip().rstrip("；;。") + "；", 1)
    else:
        sys.exit("找不到唯一的“实战未验”或“〔收尾回填〕”锚点")

# 自检
if hashlib.sha256(part2(text).encode()).hexdigest() != digest_before:
    sys.exit("自检失败：第二部分被改动")
left = [l for l in text.split("\n") if "〔　〕" in l or "〔收尾回填〕" in l]
if left:
    print("提示：仍有占位：", *[l[:80] for l in left], sep="\n  ")
path.write_text(text, encoding="utf-8")
after_lines = text.split("\n")
print(f"已写入 {path}：行数 {len(before_lines)} -> {len(after_lines)}")
