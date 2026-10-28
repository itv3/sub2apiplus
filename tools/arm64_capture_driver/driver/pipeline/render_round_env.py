#!/usr/bin/env python3
"""把 round-env.template 渲染成本轮驱动参数文件（只做文本替换，不联网、不读生产）。

用法：
  render_round_env.py --target <目标版本> --baseline <基线版本> --round <轮次，如 r1> --stamp <STAMP> --out <输出文件>
                      [--set KEY=VALUE ...]
自动推出的名字（全部由版本号算出，模板里没有任何写死的版本）：
  @TAG@            "c" + 目标版本紧凑形（主.次.补丁 → 主 次(三位) 补丁，如目标 0.X.0 就是 c0X0 去掉空格）
  @BASELINE_TAG@   基线版本同样的紧凑标签
  @TARGET_COMPACT@ 目标版本紧凑形（01650）；@BASELINE_SHORT@ 基线紧凑形去掉补丁位
  @TARGET_SHORT@   目标紧凑形去掉补丁位；@PROFILE_SUFFIX@ "r" + 去掉前导 0 的紧凑形；@ZERO64@ 64 个 0 的占位摘要（VC-3 后由流水线写真值）
--set KEY=VALUE 把模板里 KEY=REPLACE_… 的整行值换成 VALUE（只许替换以 REPLACE_ 开头的值，防止悄悄改掉其它键）。
输出里仍含 REPLACE_ 的键会列在标准错误里；这样的文件 parse_env.py 会拒绝，填完才可用。
退出码：0 渲染成功（可能仍有待填键）；2 用法错误。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().with_name("round-env.template")
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def compact(version: str) -> tuple[str, str]:
    match = VERSION.match(version)
    if not match:
        raise SystemExit(f"版本号形状不对（要 主.次.补丁）：{version}")
    major, minor, patch = (int(part) for part in match.groups())
    full = f"{major}{minor:03d}{patch}"
    return full, full[:-1]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--round", required=True, dest="round_name")
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--set", action="append", default=[], dest="overrides")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"r[0-9]+", args.round_name):
        raise SystemExit("--round 形如 r1")
    if not re.fullmatch(r"[0-9]{8}t[0-9]{6}z", args.stamp):
        raise SystemExit("--stamp 形如 20261029t000000z")
    target_full, target_short = compact(args.target)
    base_full, base_short = compact(args.baseline)
    mapping = {
        "ROUND": args.round_name, "STAMP": args.stamp, "TARGET_VERSION": args.target, "BASELINE_VERSION": args.baseline,
        "TAG": "c" + target_full, "BASELINE_TAG": "c" + base_full, "TARGET_COMPACT": target_full,
        "BASELINE_SHORT": base_short, "TARGET_SHORT": target_short, "PROFILE_SUFFIX": "r" + str(int(target_full)), "ZERO64": "0" * 64,
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in mapping.items():
        text = text.replace(f"@{key}@", value)
    leftover = sorted(set(re.findall(r"@[A-Z_]+@", text)))
    if leftover:
        raise SystemExit(f"模板里有未识别的占位：{leftover}")
    for item in args.overrides:
        key, sep, value = item.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not re.fullmatch(r"[A-Za-z0-9_./:@,+=$-]+", value):
            raise SystemExit(f"--set 参数不合法：{item!r}")
        pattern = re.compile(rf"^({key}=)(.*REPLACE_.*)$", re.M)
        hits = pattern.findall(text)
        if len(hits) != 1:
            raise SystemExit(f"{key} 在模板里不是恰好一行待填值（命中 {len(hits)}）；只许替换 REPLACE_ 开头的值")
        text = pattern.sub(lambda m: m.group(1) + value, text)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    args.out.chmod(0o600)
    pending = sorted(set(re.findall(r"^([A-Z][A-Z0-9_]*)=.*REPLACE_", text, flags=re.M)))
    print(f"已渲染 {args.out}；待填键 {len(pending)} 个：{' '.join(pending) if pending else '无'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main(sys.argv[1:]))
