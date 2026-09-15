"""压缩场景不得把成败绑死在某个特定模型的上游可用性上。

comp-hash-changed 原先直接借生产目录里 gpt-5.6-luna -> gpt-5.4 的自然跨组，结果该模型
间歇性连第一轮 turn 都跑不完，整轮 official 采集（20 分钟）反复作废。改为受控模型目录
后，触发条件由目录里的 comp_hash 决定，与哪个模型当前是否健康无关。
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

from tools.official_client_capture import build_compaction_model_catalog


class CompactionScenarioModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool_root = Path(__file__).parents[1]
        cls.path = cls.tool_root / "run_official_relay_scenario.sh"
        cls.source = cls.path.read_text(encoding="utf-8")
        cls.catalog = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "codex_0_154_models_catalog.json"
            ).read_text(encoding="utf-8")
        )

    def test_shell_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.path)], text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_两个模型均由_Campaign_显式绑定(self) -> None:
        """0.154 两条换模链必须使用 Campaign 的 Main 和 Lite 模型。"""

        self.assertIn("compaction_first_model=$model", self.source)
        self.assertIn("local secondary=${COMPACTION_SECOND_MODEL:-}", self.source)
        self.assertEqual(self.source.count("configure_compaction_models"), 3)
        self.assertNotRegex(
            self.source,
            r"compaction_first_model=['\"]gpt-",
        )
        self.assertNotIn("gpt-5.3-codex-spark", self.source)
        self.assertIn("codex_minor >= 154", self.source)
        self.assertIn("secondary=gpt-6-astra", self.source)
        self.assertIn("secondary=gpt-5.4-mini", self.source)
        self.assertIn("compaction_second_model=$secondary", self.source)
        self.assertIn("compaction_second_track=$secondary_track", self.source)

        scenario = json.loads(
            (self.tool_root / "codex_upgrade_scenarios_0_154_0.json").read_text(
                encoding="utf-8"
            )
        )
        jobs = {item["id"]: item for item in scenario["capture_jobs"]}
        for job_id in (
            "official-relay-comp-hash-changed",
            "official-relay-model-downshift",
        ):
            self.assertEqual(
                jobs[job_id]["steps"][0]["environment"][
                    "COMPACTION_SECOND_MODEL"
                ],
                "{lite_model}",
            )

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
        self.assertEqual(
            self.source.count("build_compaction_model_catalog.py"),
            2,
        )

    def test_真实_0154_目录支持_Main_到_Astra_Lite_的_hash_变化(self) -> None:
        """CompHashChanged 的触发前提就是换模前后 hash 不同；写成同值场景必然落空。"""

        result = build_compaction_model_catalog.build_catalog(
            self.catalog,
            first_model="gpt-5.5",
            second_model="gpt-6-astra",
            second_track="lite",
            reason="comp_hash_changed",
        )
        by_slug = {item["slug"]: item for item in result["models"]}
        self.assertFalse(by_slug["gpt-5.5"]["use_responses_lite"])
        self.assertTrue(by_slug["gpt-6-astra"]["use_responses_lite"])
        self.assertEqual(by_slug["gpt-5.5"]["comp_hash"], "comp-hash-probe-first")
        self.assertEqual(
            by_slug["gpt-6-astra"]["comp_hash"], "comp-hash-probe-second"
        )

    def test_真实_0154_目录支持_Main_到_Astra_Lite_的_downshift(self) -> None:
        """ModelDownshift 要的是窗口差异，hash 必须相同，免得先触发 CompHashChanged。"""

        result = build_compaction_model_catalog.build_catalog(
            self.catalog,
            first_model="gpt-5.5",
            second_model="gpt-6-astra",
            second_track="lite",
            reason="model_downshift",
        )
        first, second = result["models"]
        self.assertEqual(first["comp_hash"], "downshift-probe")
        self.assertEqual(second["comp_hash"], "downshift-probe")
        self.assertEqual(first["context_window"], 272000)
        self.assertEqual(second["context_window"], 128000)
        self.assertEqual(first["auto_compact_token_limit"], 16000)
        self.assertEqual(second["auto_compact_token_limit"], 8000)

    def test_缺失_重复和_Lite_标记漂移均失败关闭(self) -> None:
        cases = []
        missing = json.loads(json.dumps(self.catalog))
        missing["models"] = [
            item for item in missing["models"] if item["slug"] != "gpt-6-astra"
        ]
        cases.append((missing, "gpt-5.5", "gpt-6-astra", "缺少唯一模型"))
        cases.append((self.catalog, "gpt-5.5", "gpt-5.5", "两个不同"))
        main_drift = json.loads(json.dumps(self.catalog))
        next(item for item in main_drift["models"] if item["slug"] == "gpt-5.5")[
            "use_responses_lite"
        ] = True
        cases.append((main_drift, "gpt-5.5", "gpt-6-astra", "首模型必须"))
        lite_drift = json.loads(json.dumps(self.catalog))
        next(item for item in lite_drift["models"] if item["slug"] == "gpt-6-astra")[
            "use_responses_lite"
        ] = False
        cases.append((lite_drift, "gpt-5.5", "gpt-6-astra", "冻结轨道不一致"))

        for payload, first, second, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                build_compaction_model_catalog.CatalogError,
                message,
            ):
                build_compaction_model_catalog.build_catalog(
                    payload,
                    first_model=first,
                    second_model=second,
                    second_track="lite",
                    reason="comp_hash_changed",
                )


if __name__ == "__main__":
    unittest.main()
