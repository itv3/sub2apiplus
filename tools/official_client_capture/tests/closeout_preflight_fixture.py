"""收尾两道预检（冻结覆盖、门禁环境注入核对）的测试夹具，供 Codex／Claude 收尾测试共用。

收尾测试用的是只有几个文件的隔离仓库，没有冻结台账和 ``tools/upstream_merge``，真实的冻结覆盖检查在里面只会报“算不出来”。
所以通用夹具默认把冻结覆盖检查替换成“全部通过”；专门测预检的用例再显式换回真实实现（``REAL_FROZEN_COVERAGE``）并造出带台账的仓库。

候选测试包的剔除清单（``tools/official_client_capture/tests/__init__.py`` 里的 ``GATE_EXECUTOR_ENV_NAMES``）也要先放进隔离仓库，
否则环境注入核对读不到清单会失败关闭——这正是预检要的行为。
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from tools import codex_closeout as closeout

# 真实实现的引用（模块导入时取，早于任何 patch）。
REAL_FROZEN_COVERAGE = closeout.frozen_coverage

PASSING_FROZEN_COVERAGE = {"name": "freeze-receipts", "passed": True, "problems": [], "detail": {"registered": 0, "uncovered": []}}

SCRUB_FILE = closeout.GATE_SCRUB_FILE
SCRUB_TEXT = '"""隔离仓库里的测试包。"""\n\nGATE_EXECUTOR_ENV_NAMES = ("UNIT_EXECUTOR_RETRY_LEDGER",)\n'


def write_scrub_file(repo: Path, text: str = SCRUB_TEXT) -> Path:
    """把候选测试包的剔除清单写进隔离仓库（调用方随后自己提交）。"""

    path = Path(repo) / SCRUB_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def patch_frozen_coverage(result: dict | None = None):
    """返回已启动的 patcher：``closeout.frozen_coverage`` 固定返回 ``result``（缺省全部通过）。用例结束时由调用方 ``addCleanup(patcher.stop)``。"""

    patcher = mock.patch.object(closeout, "frozen_coverage", return_value=result or PASSING_FROZEN_COVERAGE)
    patcher.start()
    return patcher
