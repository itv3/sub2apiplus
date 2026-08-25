"""锁定双轨模型坐标在三处声明之间逐字一致。

主线模型是判据语义的根：主线判据整体建立在 use_responses_lite=false 上。模型集合
分散声明在三个文件里——权威定义、h1 探针的受控 /models 载荷、compact 证据的模型
白名单——任何一处漏改都不会当场报错，而是让采集条件与标签声明悄悄脱节。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tools.official_client_capture import extract_compaction_reason, h1_wire_probe
from tools.official_client_capture.capturelib.model import (
    LITE_TRACK_MODELS,
    MAIN_TRACK_MODELS,
    track_models_for_version,
)


def probe_models() -> dict[str, bool]:
    payload = json.loads(h1_wire_probe.MODELS_BODY.decode())
    return {item["slug"]: item["use_responses_lite"] for item in payload["models"]}


class MainTrackModelTests(unittest.TestCase):
    def test_track_sets_are_disjoint_and_nonempty(self) -> None:
        self.assertTrue(MAIN_TRACK_MODELS, "主线模型集合不得为空")
        self.assertTrue(LITE_TRACK_MODELS, "Lite 轨模型集合不得为空")
        self.assertFalse(set(MAIN_TRACK_MODELS) & set(LITE_TRACK_MODELS))

    def test_versioned_track_policies_are_frozen(self) -> None:
        self.assertEqual(
            track_models_for_version("0.147.0", "main"),
            ("gpt-5.4", "gpt-5.5"),
        )
        self.assertEqual(
            track_models_for_version("0.147.0", "lite"),
            ("gpt-5.6-luna",),
        )
        self.assertEqual(
            track_models_for_version("0.149.1", "main"),
            ("gpt-5.5", "gpt-5.4-mini"),
        )
        self.assertEqual(
            track_models_for_version("0.149.1", "lite"),
            ("gpt-5.6-terra", "gpt-5.6-luna"),
        )

    def test_probe_models_cover_both_tracks_with_matching_lite_flag(self) -> None:
        """受控 /models 必须覆盖两条轨道，且 lite 标志与轨道归属一致。

        覆盖不全时官方 CLI 查不到模型元数据会落到默认值；标志写反等于用受控上游
        伪造 Lite 条件——两者都会让 h1-wire 采到的样本与标签声明的 mode 对不上。
        """

        probe = probe_models()
        for model in MAIN_TRACK_MODELS:
            self.assertIn(model, probe, f"h1 探针受控 /models 缺主线模型 {model}")
            self.assertIs(
                probe[model],
                False,
                f"{model} 属主线，use_responses_lite 必须为 false",
            )
        for model in LITE_TRACK_MODELS:
            self.assertIn(model, probe, f"h1 探针受控 /models 缺 Lite 轨模型 {model}")
            self.assertIs(
                probe[model],
                True,
                f"{model} 属 Lite 轨，use_responses_lite 必须为 true",
            )

    def test_compaction_allowed_models_cover_both_tracks(self) -> None:
        missing = (set(MAIN_TRACK_MODELS) | set(LITE_TRACK_MODELS)) - (
            extract_compaction_reason.ALLOWED_MODELS
        )
        self.assertFalse(missing, f"compact 证据白名单缺模型：{sorted(missing)}")

    def test_candidate_scripts_require_campaign_models(self) -> None:
        """候选脚本不得自行猜测目标版本的两条模型轨道。"""

        here = Path(__file__).resolve().parents[1]
        core = (here / "run_candidate_core_capture.sh").read_text()
        aux = (here / "run_candidate_aux_capture.sh").read_text()

        self.assertIn("main_model=${MAIN_MODEL:?", core)
        self.assertIn("lite_model=${LITE_MODEL:?", core)
        self.assertIn("model=${MODEL:?", aux)
        self.assertNotRegex(core, re.compile(r"^main_model=\$\{MAIN_MODEL:-", re.M))
        self.assertNotRegex(core, re.compile(r"^lite_model=\$\{LITE_MODEL:-", re.M))
        self.assertNotRegex(aux, re.compile(r"^model=\$\{MODEL:-", re.M))

    def test_candidate_core_script_has_no_hardcoded_model(self) -> None:
        """core 脚本的请求体不得再出现裸模型名，只能走两个变量。"""

        core = (Path(__file__).resolve().parents[1] / "run_candidate_core_capture.sh")
        bodies = re.findall(
            r'write_request_body\s+"[^"]+"\s+(\S+)', core.read_text()
        )
        self.assertTrue(bodies, "未解析到任何 write_request_body 调用")
        hardcoded = sorted({b for b in bodies if not b.startswith('"$')})
        self.assertFalse(hardcoded, f"仍有硬编码模型：{hardcoded}")


if __name__ == "__main__":
    unittest.main()
