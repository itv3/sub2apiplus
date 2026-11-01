#!/usr/bin/env python3
"""收尾 b3a：晋升后形态的测试断言修正。幂等：已是目标版本就什么都不改。

做什么
    VC-6 晋升后 Runtime Catalog 的 Active 变成新目标版本，``test_claude_upgrade_boundary_codex.py`` 里
    ``test_real_codex_runtime_catalog_resolves_active`` 仍断言上一版为 Active，收尾预跑才暴露（历次收尾的先例）。
    本脚本把这条断言（及其上方的一行注释）从“基线版本”改成“目标版本”。该文件不在冻结台账内（grep base-files.sha256 无命中），不需要承接收据。
输入
    在仓库（收尾工作树）根目录运行：
    python3 -B apply_postpromo_fixes.py --target <目标版本> --prev <基线版本> --retired <退休版本>
    锚点：测试文件里两处固定写法
        ``        # <prev> 收尾：VC-6 晋升后 Runtime Catalog 的 Active 为 <prev>（Previous <retired>）。``
        ``        self.assertEqual(active["version"], "<prev>")``
    每个锚点必须恰好命中一次，否则报错退出（文件被改过，按报错手工改同样两行）。
输出
    原位改写测试文件；已经是目标版本时打印“已是目标版本，不改”并退出 0。
何时用
    b3a-postpromo-fixes 步骤（晋升＋退休的模拟树上可提前跑）。
"""
import argparse
import sys
from pathlib import Path

TEST_FILE = "tools/official_client_capture/tests/test_claude_upgrade_boundary_codex.py"


def apply(root, target, prev, retired):
    """在 root 下改测试文件；返回（是否改动, 提示文字）。锚点不是恰好 1 次时抛 SystemExit。"""
    path = Path(root) / TEST_FILE
    text = path.read_text(encoding="utf-8")
    old_comment = f"        # {prev} 收尾：VC-6 晋升后 Runtime Catalog 的 Active 为 {prev}（Previous {retired}）。\n"
    old_assert = f'        self.assertEqual(active["version"], "{prev}")\n'
    new_comment = f"        # {target} 收尾：VC-6 晋升后 Runtime Catalog 的 Active 为 {target}（Previous {prev}）。\n"
    new_assert = f'        self.assertEqual(active["version"], "{target}")\n'
    if new_assert in text:
        return False, "已是目标版本，不改"
    for old in (old_comment, old_assert):
        if text.count(old) != 1:
            raise SystemExit(f"锚点命中 {text.count(old)} 次（应为 1）：{old.strip()[:60]}")
    text = text.replace(old_comment, new_comment).replace(old_assert, new_assert)
    path.write_text(text, encoding="utf-8")
    return True, f"已改 {path}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, help="目标版本（晋升后的 Active）")
    parser.add_argument("--prev", required=True, help="基线版本（晋升后的 Previous，也是测试里现写的 Active）")
    parser.add_argument("--retired", required=True, help="退休版本（测试注释里现写的 Previous）")
    parser.add_argument("--root", default=".", help="仓库根，缺省当前目录")
    args = parser.parse_args(argv)
    _changed, message = apply(args.root, args.target, args.prev, args.retired)
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
