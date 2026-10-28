"""官方客户端抓包工具测试。"""

import os as _os

# 收尾门禁（tools/codex_closeout.py 的 run_gates）给驱动设了环境变量，驱动再转发给执行器，执行器起的每个测试单元都继承了它们。
# 测试里大量用 ``env={**os.environ, ...}`` 嵌套起临时执行器（test_ci_unit_executor_retry.RetryRunBase.run_gates、test_ci_unit_executor、
# test_ci_entry_gates 等），不剔除的话嵌套执行器会改用外层门禁的持久补跑账本，而不是测试自己的临时记录库：第一个用例用掉
# “最近 5 轮同一单元只许补跑通过 1 次”的唯一额度，后面的用例读到“这次再红直接判红”（0.164 收尾门禁 test_ci_background_validation_retry 等 3 个单元因此红）。
# 所以在测试包导入时统一剔除；需要这些变量的用例（test_ci_retry_ledger_across_closeouts）都经显式 env 字典传入，不依赖外层 os.environ。
# 以后收尾工具给执行器再设环境变量，把名字加进下面的元组；test_ci_retry_ledger_across_closeouts 里的守卫用例会核对
# codex_closeout.run_gates 设的每个变量是否都在这里。
GATE_EXECUTOR_ENV_NAMES = ("UNIT_EXECUTOR_RETRY_LEDGER",)
for _name in GATE_EXECUTOR_ENV_NAMES:
    _os.environ.pop(_name, None)
