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
)


def probe_models() -> dict[str, bool]:
    payload = json.loads(h1_wire_probe.MODELS_BODY.decode())
    return {item["slug"]: item["use_responses_lite"] for item in payload["models"]}


class MainTrackModelTests(unittest.TestCase):
    def test_track_sets_are_disjoint_and_nonempty(self) -> None:
        self.assertTrue(MAIN_TRACK_MODELS, "主线模型集合不得为空")
        self.assertTrue(LITE_TRACK_MODELS, "Lite 轨模型集合不得为空")
        self.assertFalse(set(MAIN_TRACK_MODELS) & set(LITE_TRACK_MODELS))

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

    def test_candidate_scripts_default_to_track_models(self) -> None:
        """候选脚本的模型默认值必须来自两条轨道的权威定义。

        脚本是 shell、读不到 Python 常量，默认值只能各写一份。写死一个不在轨道里的
        模型不会当场报错——只有当候选机账号恰好没有它时，才会在采集中途以 404 暴露
        （k53 的 frozen-core 即如此）。这条测试把两份默认值钉死在权威定义上。
        """

        here = Path(__file__).resolve().parents[1]
        core = (here / "run_candidate_core_capture.sh").read_text()
        aux = (here / "run_candidate_aux_capture.sh").read_text()

        core_main = re.search(r"^main_model=\$\{MAIN_MODEL:-([^}]+)\}", core, re.M)
        core_lite = re.search(r"^lite_model=\$\{LITE_MODEL:-([^}]+)\}", core, re.M)
        aux_model = re.search(r"^model=\$\{MODEL:-([^}]+)\}", aux, re.M)
        self.assertIsNotNone(core_main, "core 脚本缺 main_model 默认值")
        self.assertIsNotNone(core_lite, "core 脚本缺 lite_model 默认值")
        self.assertIsNotNone(aux_model, "aux 脚本缺 model 默认值")

        self.assertIn(core_main.group(1), MAIN_TRACK_MODELS)
        self.assertIn(core_lite.group(1), LITE_TRACK_MODELS)
        self.assertIn(aux_model.group(1), LITE_TRACK_MODELS)

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
