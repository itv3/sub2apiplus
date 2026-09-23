"""真实链执行入口（``make test-capture-real-chains`` 使用）：流式运行登记的真实链模块。

真实链只供开发机、CI 与 ARM64 自查手动执行，不进发布认证；认证由 pre-A3 在 staging 内执行
登记链，并把 skip 记为未认证。本入口保证手动执行时 skip 同样不会被误读为通过：

- 退出前逐条列出全部被跳过的用例及原因；
- 设置 ``CAPTURE_REAL_CHAINS_REQUIRE_EXECUTION=1``（ARM64 自查使用）时，``tests/real_chains/`` 下任一真实链被跳过
  即非零退出；同批运行的普通单测因检出范围等环境原因跳过时只列出、不改变退出码。

本文件位于 tests/，不进受管摘要，也不匹配默认发现的 ``test_*.py``。
"""

from __future__ import annotations

import os
import sys
import unittest

REQUIRE_EXECUTION_ENV = "CAPTURE_REAL_CHAINS_REQUIRE_EXECUTION"


def main(argv: list[str]) -> int:
    modules = argv[1:]
    if not modules:
        print("用法：python3 -m tools.official_client_capture.tests.real_chain_gate <真实链模块>...", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2 if os.environ.get("VERBOSE") == "1" else 1).run(suite)
    if result.skipped:
        print(f"以下 {len(result.skipped)} 条用例在本机被跳过，未经验证：", file=sys.stderr)
        for case, reason in result.skipped:
            print(f"  - {case.id()}：{reason}", file=sys.stderr)
        chains = [case.id() for case, _reason in result.skipped if ".real_chains." in case.id()]
        if chains and os.environ.get(REQUIRE_EXECUTION_ENV) == "1":
            print(f"{REQUIRE_EXECUTION_ENV}=1：{len(chains)} 条真实链被跳过，按失败处理。", file=sys.stderr)
            return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
