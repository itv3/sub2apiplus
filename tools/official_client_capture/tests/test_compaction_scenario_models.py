"""压缩场景不得把成败绑死在某个特定模型的上游可用性上。

comp-hash-changed 原先直接借生产目录里 gpt-5.6-luna -> gpt-5.4 的自然跨组，结果该模型
间歇性连第一轮 turn 都跑不完，整轮 official 采集（20 分钟）反复作废。改为受控模型目录
后，触发条件由目录里的 comp_hash 决定，与哪个模型当前是否健康无关。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
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
        # 0.163.0 客户端内置目录（rust-v0.163.0 的 codex-rs/models-manager/models.json）里与本场景相关的三个条目，
        # 只保留测试用到的字段：主线 gpt-6.1-sol（use_responses_lite=true）、Lite 轨 gpt-6-astra（true）、
        # 已下线的旧主线 gpt-5.5（false）。
        cls.catalog_0163 = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "codex_0_163_models_catalog.json"
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

    def test_0163_目录支持_Lite_主线到_Astra_Lite_的两个压缩原因(self) -> None:
        """0.163.0 起主线模型本身是 Lite：首模型按声明核对 use_responses_lite=true，触发字段照旧由受控目录改写。"""

        result = build_compaction_model_catalog.build_catalog(
            self.catalog_0163,
            first_model="gpt-6.1-sol",
            second_model="gpt-6-astra",
            second_track="lite",
            reason="comp_hash_changed",
            first_use_responses_lite=True,
        )
        by_slug = {item["slug"]: item for item in result["models"]}
        self.assertTrue(by_slug["gpt-6.1-sol"]["use_responses_lite"])
        self.assertTrue(by_slug["gpt-6-astra"]["use_responses_lite"])
        # 真实目录里两者 comp_hash 都是 3000，自然切换不会触发 CompHashChanged；受控目录必须把它们改成不同值。
        self.assertEqual(self.catalog_0163["models"][0]["comp_hash"], self.catalog_0163["models"][1]["comp_hash"])
        self.assertEqual(by_slug["gpt-6.1-sol"]["comp_hash"], "comp-hash-probe-first")
        self.assertEqual(by_slug["gpt-6-astra"]["comp_hash"], "comp-hash-probe-second")

        downshift = build_compaction_model_catalog.build_catalog(
            self.catalog_0163,
            first_model="gpt-6.1-sol",
            second_model="gpt-6-astra",
            second_track="lite",
            reason="model_downshift",
            first_use_responses_lite=True,
        )
        first, second = downshift["models"]
        # 真实目录里两者窗口都是 272000，自然切换不会触发 ModelDownshift；受控目录把窗口冻结为 272000 -> 128000。
        self.assertEqual(first["comp_hash"], "downshift-probe")
        self.assertEqual(second["comp_hash"], "downshift-probe")
        self.assertEqual((first["context_window"], second["context_window"]), (272000, 128000))
        self.assertEqual((first["auto_compact_token_limit"], second["auto_compact_token_limit"]), (16000, 8000))

    def test_首模型_Lite_标志必须与主线声明一致(self) -> None:
        """不放宽成两种都接受：声明 true 就拒绝目录里 false 的首模型，缺省 false 就拒绝 Lite 首模型。"""

        cases = (
            # 声明主线是 Lite（0.163.0），目录里的旧主线 gpt-5.5 是非 Lite → 拒绝。
            ("gpt-5.5", True, "首模型必须是 use_responses_lite=true"),
            # 缺省（0.160.0 及更早的语义）下，Lite 的 gpt-6.1-sol 不能充当主线首模型。
            ("gpt-6.1-sol", False, "首模型必须是 use_responses_lite=false"),
        )
        for first, expected_lite, message in cases:
            with self.subTest(first=first, expected_lite=expected_lite), self.assertRaisesRegex(
                build_compaction_model_catalog.CatalogError,
                message,
            ):
                build_compaction_model_catalog.build_catalog(
                    self.catalog_0163,
                    first_model=first,
                    second_model="gpt-6-astra",
                    second_track="lite",
                    reason="comp_hash_changed",
                    first_use_responses_lite=expected_lite,
                )
        # 不传该参数就是 0.160.0 及更早的历史语义（首模型非 Lite），0.154 目录的既有用法不变。
        build_compaction_model_catalog.build_catalog(
            self.catalog,
            first_model="gpt-5.5",
            second_model="gpt-6-astra",
            second_track="lite",
            reason="comp_hash_changed",
        )

    def test_命令行参数默认保持历史语义并接受_true(self) -> None:
        """脚本传的是命令行参数：缺省按 false 核对（历史 Job 不带该参数），显式 true 才接受 Lite 首模型。"""

        tool = self.tool_root / "build_compaction_model_catalog.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cache = root / "models_cache.json"
            cache.write_text(json.dumps(self.catalog_0163), encoding="utf-8")
            base = [
                sys.executable,
                "-B",
                str(tool),
                "--models-cache",
                str(cache),
                "--first-model",
                "gpt-6.1-sol",
                "--second-model",
                "gpt-6-astra",
                "--second-track",
                "lite",
                "--reason",
                "comp_hash_changed",
            ]
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            rejected = subprocess.run(
                [*base, "--output", str(root / "rejected.json")],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(rejected.returncode, 2, rejected.stderr)
            self.assertIn("首模型必须是 use_responses_lite=false", rejected.stderr)
            self.assertFalse((root / "rejected.json").exists())
            accepted = subprocess.run(
                [*base, "--first-use-responses-lite", "true", "--output", str(root / "accepted.json")],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            written = json.loads((root / "accepted.json").read_text(encoding="utf-8"))
            self.assertEqual([item["slug"] for item in written["models"]], ["gpt-6.1-sol", "gpt-6-astra"])

    def test_中继脚本把首模型_Lite_声明交给目录构造器(self) -> None:
        """两条压缩场景都必须把 Campaign 冻结的主线 Lite 声明传给构造器，缺省 false 保持历史 Job 语义。"""

        self.assertIn("compaction_first_lite=$first_lite", self.source)
        self.assertIn("local first_lite=${COMPACTION_FIRST_USE_RESPONSES_LITE:-false}", self.source)
        self.assertEqual(
            self.source.count('--first-use-responses-lite "$compaction_first_lite"'),
            2,
        )
        self.assertIn("COMPACTION_FIRST_USE_RESPONSES_LITE 必须是 true 或 false", self.source)


if __name__ == "__main__":
    unittest.main()
