"""验证 direct 矩阵只操作本轮主体实际需要的验收账号。"""

from __future__ import annotations

import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]


class DirectMatrixAccountSelectionTest(unittest.TestCase):
    def test_codex_only_账号选择与恢复均使用动态闭集(self) -> None:
        source = (TOOL_ROOT / "run_sub2api_direct_matrix.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("selected_account_set", source)
        self.assertIn('for account_id in "${selected_account_ids[@]}"', source)
        self.assertIn("where id in ($account_ids_csv)", source)
        self.assertIn('[[ $current_proxy_state == "$original_proxy_state" ]]', source)
        self.assertNotIn(
            "where id in ($claude_account_id,$codex_account_id)", source
        )


if __name__ == "__main__":
    unittest.main()
