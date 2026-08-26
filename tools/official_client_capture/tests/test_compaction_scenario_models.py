"""压缩场景不得把成败绑死在某个特定模型的上游可用性上。

comp-hash-changed 原先直接借生产目录里 gpt-5.6-luna -> gpt-5.4 的自然跨组，结果该模型
间歇性连第一轮 turn 都跑不完，整轮 official 采集（20 分钟）反复作废。改为受控模型目录
后，触发条件由目录里的 comp_hash 决定，与哪个模型当前是否健康无关。
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


class CompactionScenarioModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = Path(__file__).parents[1] / "run_official_relay_scenario.sh"
        cls.source = cls.path.read_text(encoding="utf-8")

    def test_shell_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.path)], text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_首模型跟随_Campaign_且不再硬编码不受支持模型(self) -> None:
        """两条换模链都必须从 MODEL 取得首模型，不能再写死 gpt-5.4。"""

        self.assertIn("compaction_first_model=$model", self.source)
        self.assertEqual(self.source.count("configure_compaction_models"), 3)
        self.assertNotRegex(
            self.source,
            r"compaction_first_model=['\"]gpt-",
        )
        self.assertNotIn("gpt-5.3-codex-spark", self.source)

    def test_第二模型可冻结覆盖且默认使用非_Lite_mini(self) -> None:
        self.assertIn(
            "local secondary=${COMPACTION_SECOND_MODEL:-gpt-5.4-mini}",
            self.source,
        )
        self.assertIn("compaction_second_model=$secondary", self.source)
        self.assertIn(".use_responses_lite != false", self.source)

    def test_两个压缩场景都用受控目录(self) -> None:
        """受控目录让触发条件来自目录本身，而不是生产目录的当期状态。"""

        for catalog in ("comp-hash-catalog.json", "model-downshift-catalog.json"):
            with self.subTest(catalog=catalog):
                self.assertIn(catalog, self.source)
        # 只数场景分支里的赋值，不含顶部的 compaction_catalog="" 初始化。
        scenario_assignments = re.findall(
            r'compaction_catalog="/capture/runs/\$run_id/[^"]+"', self.source
        )
        self.assertEqual(len(scenario_assignments), 2)

    def test_comp_hash_两侧取值必须不同(self) -> None:
        """CompHashChanged 的触发前提就是换模前后 hash 不同；写成同值场景必然落空。"""

        self.assertIn('comp-hash-probe-first', self.source)
        self.assertIn('comp-hash-probe-second', self.source)

    def test_downshift_仍保持同_hash(self) -> None:
        """ModelDownshift 要的是窗口差异，hash 必须相同，免得先触发 CompHashChanged。"""

        self.assertIn('.comp_hash = "downshift-probe"', self.source)
        self.assertIn('.auto_compact_token_limit = 16000', self.source)
        self.assertIn('.auto_compact_token_limit = 8000', self.source)


if __name__ == "__main__":
    unittest.main()
