"""0.163.0 起主线是 Lite 模型（0.164.0 沿用）：BODY-006 里原来依赖非 Lite 样本的两个检查已改写成主线 Lite 形态。

这里用手造的 Lite／非 Lite 请求体样本跑改写后检查的正反例，并锁定 EP-001 的 nonlite-image-tool 保持原样的理由
（它读候选侧 Go 追踪测试事实，不依赖官方主线样本）。声明本身与各处载荷的一致性见 test_main_track_models.py。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.official_client_capture import (
    acceptance_contract,
    candidate_rule_assertion,
    relay_extract,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]


class MainTrackLiteExpectationTests(unittest.TestCase):
    """BODY-006 的两个主线 Lite 检查与 EP-001 的 nonlite-image-tool。

    用手造的 Lite／非 Lite 请求体样本跑这两个检查的正反例；样本的 body 摘要由 relay_extract.summarize_body
    从真实 JSON 请求体生成，与官方证据派生的 data.body 同构。引擎用 candidate_rule_assertion 里
    评估规则时同样的选样与断言函数，选不到样本时断言失败关闭（all_* 要求至少一条命中）。

    清单后缀写成类属性，0.164.0 由下面的子类复用同一组断言（0.164.0 清单由 0.163.0 终态复制，判据随差异报告改写时两份都要继续满足）。
    """

    MANIFEST_SUFFIX = "0_163_0"

    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = json.loads(
            (TOOL_ROOT / f"candidate_rule_expectations_{cls.MANIFEST_SUFFIX}.json").read_text(encoding="utf-8")
        )
        cls.rule = next(
            rule for rule in cls.profile["rules"] if rule["rule_id"] == "SPEC-BODY-006"
        )
        cls.checks = {check["id"]: check for check in cls.rule["checks"]}

    @staticmethod
    def lite_body() -> dict:
        """Lite 形态：顶层没有 instructions／tools，基础指令是 input 里的 developer 消息，工具目录是 additional_tools 项。"""

        return {
            "model": "gpt-6.1-sol",
            "stream": True,
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": [{"type": "function", "name": "shell"}]},
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "base"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": "low", "context": "all_turns"},
            "store": False,
            "include": [],
            "text": {"verbosity": "low"},
        }

    @staticmethod
    def non_lite_body() -> dict:
        """0.163.0 的非 Lite 形态：基础指令也移进 developer 消息，但工具目录仍是顶层 tools。"""

        return {
            "model": "gpt-5.5",
            "stream": True,
            "input": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "base"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
            "tools": [{"type": "function", "name": "shell"}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "medium", "context": "none"},
            "store": False,
            "include": [],
        }

    @staticmethod
    def observation(body: dict, *, scenario: str = "A04", mode: str = "lite", track: str | None = "main",
                    method: str = "POST", path: str = "/backend-api/codex/responses"):
        summary = relay_extract.summarize_body(json.dumps(body).encode(), "", "application/json")
        labels = {"transport": "http", "mode": mode, "provider": "openai_oauth"}
        if track is not None:
            labels["track"] = track
        return candidate_rule_assertion.Observation(
            record_id=f"{scenario}-{mode}-{method}",
            scenario_id=scenario,
            record_type="http_request",
            artifact_path="relay/conn001.client_to_upstream.bin",
            evidence_paths=("relay/conn001.client_to_upstream.bin",),
            labels=labels,
            data={"method": method, "path": path, "body": summary},
        )

    def run_check(self, check_id: str, observations: list) -> tuple[bool, dict]:
        check = self.checks[check_id]
        matched = candidate_rule_assertion._select_observations(
            observations, check["select"], self.rule["scenario_ids"]
        )
        return candidate_rule_assertion._evaluate_assertion(matched, check["assertion"])

    def test_old_non_lite_check_ids_are_gone_and_selectors_follow_the_main_track(self) -> None:
        self.assertNotIn("nonlite-instructions-absent", self.checks)
        self.assertNotIn("nonlite-tools-present", self.checks)
        for check_id in ("main-instructions-absent", "main-additional-tools-present"):
            where = {
                (item["path"], item["operator"]): item.get("value")
                for item in self.checks[check_id]["select"]["where"]
            }
            # 与旧检查一样只选 A04 的 Responses POST（启动期 models GET 没有 body）；mode 改为 lite。
            self.assertEqual(self.checks[check_id]["select"]["scenario_ids"], ["A04"])
            self.assertEqual(where[("labels.mode", "equal")], "lite", check_id)
            self.assertEqual(where[("data.method", "equal")], "POST", check_id)
            self.assertEqual(where[("data.path", "equal")], "/backend-api/codex/responses", check_id)

    def test_main_instructions_absent_positive_and_negative(self) -> None:
        # 正例：Lite 主线样本顶层没有 instructions。
        passed, actual = self.run_check("main-instructions-absent", [self.observation(self.lite_body())])
        self.assertTrue(passed, actual)
        # 反例一：顶层仍带 instructions（0.160.0 及更早的非 Lite 形态）→ 失败。
        legacy = {**self.lite_body(), "instructions": "legacy top-level instructions"}
        passed, actual = self.run_check("main-instructions-absent", [self.observation(legacy)])
        self.assertFalse(passed, actual)
        # 反例二：一条合格、一条违规，整体失败（all_not_contains 对每条样本都要成立）。
        passed, _ = self.run_check(
            "main-instructions-absent", [self.observation(self.lite_body()), self.observation(legacy)]
        )
        self.assertFalse(passed)
        # 失败关闭：没有任何主线 Lite 样本被选中（旧标签 mode=non_lite、GET 请求、其它场景）时不得空过。
        for unselected in (
            self.observation(self.lite_body(), mode="non_lite"),
            self.observation(self.lite_body(), method="GET"),
            self.observation(self.lite_body(), scenario="A03"),
            self.observation(self.lite_body(), path="/backend-api/codex/responses/compact"),
        ):
            passed, _ = self.run_check("main-instructions-absent", [unselected])
            self.assertFalse(passed)

    def test_main_additional_tools_present_positive_and_negative(self) -> None:
        # 正例：Lite 主线样本把工具目录放在 input 的 additional_tools 项里。
        passed, actual = self.run_check("main-additional-tools-present", [self.observation(self.lite_body())])
        self.assertTrue(passed, actual)
        # 反例一：非 Lite 形态（工具目录在顶层 tools，input 里没有 additional_tools）即使被标成 lite 也失败。
        passed, actual = self.run_check("main-additional-tools-present", [self.observation(self.non_lite_body())])
        self.assertFalse(passed, actual)
        # 反例二：additional_tools 项被丢掉（input 只剩消息）→ 失败。
        stripped = self.lite_body()
        stripped["input"] = [item for item in stripped["input"] if item["type"] != "additional_tools"]
        passed, _ = self.run_check("main-additional-tools-present", [self.observation(stripped)])
        self.assertFalse(passed)
        # 反例三：多条样本里只要有一条缺 additional_tools，整体失败。
        passed, _ = self.run_check(
            "main-additional-tools-present",
            [self.observation(self.lite_body()), self.observation(stripped)],
        )
        self.assertFalse(passed)
        # 失败关闭：选不到样本时不得空过（旧标签 mode=non_lite 不再被选中）。
        passed, _ = self.run_check(
            "main-additional-tools-present", [self.observation(self.lite_body(), mode="non_lite")]
        )
        self.assertFalse(passed)

    def test_lite_track_checks_still_select_only_the_lite_track(self) -> None:
        """既有 Lite 轨检查仍只选 mode=lite 且 track=lite 的 A03 样本：主线 Lite 样本（track=main）不会混进去。"""

        for check_id in ("lite-top-level-omission", "lite-tools-omission", "lite-model-is-lite-track-model"):
            main_sample = self.observation(self.lite_body(), scenario="A03", track="main")
            matched = candidate_rule_assertion._select_observations(
                [main_sample], self.checks[check_id]["select"], self.rule["scenario_ids"]
            )
            self.assertEqual(matched, [], check_id)
            lite_sample = self.observation(self.lite_body(), scenario="A03", track="lite")
            matched = candidate_rule_assertion._select_observations(
                [lite_sample], self.checks[check_id]["select"], self.rule["scenario_ids"]
            )
            self.assertEqual(len(matched), 1, check_id)

    def test_image_tool_check_is_kept_because_it_does_not_depend_on_official_main_samples(self) -> None:
        """EP-001 的 nonlite-image-tool 保持原样：它读候选侧 Go 追踪测试事实，不读官方主线样本。"""

        rule = next(item for item in self.profile["rules"] if item["rule_id"] == "SPEC-EP-001")
        checks = {check["id"]: check for check in rule["checks"]}
        image_check = checks["nonlite-image-tool"]
        self.assertEqual(image_check["select"]["record_type"], "image_tool_flow")
        self.assertEqual(image_check["select"]["where"], [{"operator": "equal", "path": "data.mode", "value": "non_lite"}])
        # image_tool_flow 是内部记录类型：整条规则按 candidate_profile 只在候选侧执行，官方侧不要求该记录。
        modes = acceptance_contract.derive_validation_modes(self.profile)
        self.assertEqual(modes["SPEC-EP-001"], "candidate_profile")
        # 事实来源：候选追踪测试的 a09.image-tool-nonlite 事实仍在映射里，与官方采集的主线模型无关。
        mapping = json.loads(
            (TOOL_ROOT / f"candidate_test_fact_map_{self.MANIFEST_SUFFIX}.json").read_text(encoding="utf-8")
        )
        fact_ids = {
            fact["fact_id"] for test in mapping["tests"] for fact in test["facts"]
        }
        self.assertIn("a09.image-tool-nonlite", fact_ids)
        self.assertIn("a09.image-tool-lite", fact_ids)

    def test_contract_modes_and_counts_are_unchanged_by_the_rewrite(self) -> None:
        """两个检查改写后 BODY-006 仍是 dual_wire，规则分组数量与旧画像相同；只有 check ID 集合有两处改名。"""

        payload = acceptance_contract.build_contract_payload(self.profile)
        self.assertEqual(payload["rule_counts"], {"dual_wire": 25, "candidate_profile": 18})
        self.assertEqual(payload["validation_modes"]["SPEC-BODY-006"], "dual_wire")
        ids = payload["expected_check_ids"]["SPEC-BODY-006"]
        self.assertIn("main-instructions-absent", ids)
        self.assertIn("main-additional-tools-present", ids)



class MainTrackLiteExpectation0164Tests(MainTrackLiteExpectationTests):
    """0.164.0 候选期望清单（由 0.163.0 终态复制）同样满足主线 Lite 的 BODY-006 检查与 EP-001 约束。"""

    MANIFEST_SUFFIX = "0_164_0"


if __name__ == "__main__":
    unittest.main()
