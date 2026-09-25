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

from tools.official_client_capture import (
    extract_compaction_reason,
    h1_wire_probe,
    upstream_byte_relay,
)
from tools.official_client_capture.capturelib.model import (
    LITE_TRACK_MODELS,
    MAIN_TRACK_MODELS,
    track_models_for_version,
)

# 候选 relay 合成 /models 清单接管后每条模型必须齐备的字段：缺任何一个都会让网关
# 按 serde 默认值定型（effort 为空、summary=auto），与真实上游 authoritative 清单
# 的形态脱节。
SYNTHETIC_MODEL_REQUIRED_FIELDS = (
    "visibility",
    "use_responses_lite",
    "supports_parallel_tool_calls",
    "default_reasoning_level",
    "default_reasoning_summary",
    "supports_reasoning_summary_parameter",
)
# 真实上游 0.154.0 清单（官方 lite-http-response conn001）的默认 effort。
SYNTHETIC_MODEL_EXPECTED_EFFORT = {
    "gpt-6-astra": "medium",
    "gpt-5.6-sol": "low",
    "gpt-5.6-luna": "medium",
    "gpt-5.5": "medium",
}


def probe_models() -> dict[str, bool]:
    payload = json.loads(h1_wire_probe.MODELS_BODY.decode())
    return {item["slug"]: item["use_responses_lite"] for item in payload["models"]}


def synthetic_models(body: bytes) -> dict[str, dict]:
    payload = json.loads(body.decode())
    return {item["slug"]: item for item in payload["models"]}


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
        self.assertEqual(
            track_models_for_version("0.151.0", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.151.0", "lite"),
            ("gpt-5.6-terra",),
        )
        self.assertEqual(
            track_models_for_version("0.154.0", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.154.0", "lite"),
            ("gpt-6-astra",),
        )
        self.assertEqual(
            track_models_for_version("0.156.1", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.156.1", "lite"),
            ("gpt-6-astra",),
        )
        self.assertEqual(
            track_models_for_version("0.157.0", "main"),
            ("gpt-5.5",),
        )
        self.assertEqual(
            track_models_for_version("0.157.0", "lite"),
            ("gpt-6-astra",),
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

    def test_relay_synthetic_models_are_authoritative_and_complete(self) -> None:
        """候选 relay 的 core／aux 合成 /models 必须与官方采集条件等价。

        官方采集走真实上游，清单含 visibility=list 而被整体接管；候选合成清单若缺
        visibility 或缺 Lite 轨模型，网关就回落 bundled 快照并把新模型当未知模型
        判成非 Lite（0.154 gpt-6-astra 的 VC-5 失败根因）。这里锁定：两份清单都
        覆盖目标版本 Lite 轨，core 还覆盖主轨；每条模型五个能力位齐全且
        visibility=list；lite 标志与轨道一致，并与 h1 探针受控清单同 slug 一致；
        默认 effort 与真实上游一致。
        """

        probe = probe_models()
        core = synthetic_models(upstream_byte_relay.SYNTHETIC_CORE_MODELS_BODY)
        aux = synthetic_models(upstream_byte_relay.SYNTHETIC_AUX_MODELS_BODY)
        lite_0154 = track_models_for_version("0.154.0", "lite")
        main_0154 = track_models_for_version("0.154.0", "main")
        for name, manifest, required in (
            ("core", core, lite_0154 + main_0154),
            ("aux", aux, lite_0154),
        ):
            for model in required:
                self.assertIn(model, manifest, f"relay {name} 合成 /models 缺 {model}")
            self.assertTrue(
                any(item["visibility"] == "list" for item in manifest.values()),
                f"relay {name} 合成 /models 无 visibility=list，清单不会被接管",
            )
            for slug, item in manifest.items():
                for field in SYNTHETIC_MODEL_REQUIRED_FIELDS:
                    self.assertIn(field, item, f"relay {name} 合成 /models 的 {slug} 缺 {field}")
                self.assertEqual(item["visibility"], "list", slug)
                self.assertIs(item["supports_parallel_tool_calls"], True, slug)
                self.assertIs(item["supports_reasoning_summary_parameter"], True, slug)
                self.assertEqual(item["default_reasoning_summary"], "none", slug)
                self.assertEqual(
                    item["default_reasoning_level"],
                    SYNTHETIC_MODEL_EXPECTED_EFFORT[slug],
                    f"{slug} 默认 effort 与真实上游 0.154.0 清单不一致",
                )
                if slug in probe:
                    self.assertIs(item["use_responses_lite"], probe[slug], slug)
                if slug in LITE_TRACK_MODELS:
                    self.assertIs(item["use_responses_lite"], True, slug)
                if slug in MAIN_TRACK_MODELS:
                    self.assertIs(item["use_responses_lite"], False, slug)

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

    def test_h1_probe_path_filter_ignores_startup_models(self) -> None:
        """images 证据槽不能被服务启动期 models 请求抢占。"""

        prefix = "/backend-api/codex/images/"
        self.assertFalse(
            h1_wire_probe.record_matches_path_prefix(
                {
                    "request_line": (
                        "GET /backend-api/codex/models?client_version=0.149.1 "
                        "HTTP/1.1"
                    )
                },
                prefix,
            )
        )
        self.assertTrue(
            h1_wire_probe.record_matches_path_prefix(
                {
                    "request_line": (
                        "POST /backend-api/codex/images/generations HTTP/1.1"
                    )
                },
                prefix,
            )
        )
        self.assertTrue(
            h1_wire_probe.record_matches_path_prefix(
                {"request_line": "GET /anything HTTP/1.1"}, ""
            )
        )

    def test_images_script_enables_image_path_filter(self) -> None:
        """images runner 必须显式启用端点过滤。"""

        script = (
            Path(__file__).resolve().parents[1] / "run_images_wire_probe.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "--record-path-prefix /backend-api/codex/images/",
            script,
        )


if __name__ == "__main__":
    unittest.main()
