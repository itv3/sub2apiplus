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



class Expectation0164WireChecksTests(unittest.TestCase):
    """0.164.0 差异报告落进候选期望清单的新判据：手造观测跑正反例。

    - SPEC-EP-002：OAuth 刷新体多一个 refresh_reason（键序 client_id, grant_type, refresh_reason, refresh_token，取值四选一）。
    这四条规则（EP-002、WS-005、BODY-003、EP-022）都含读内部记录类型的判据，整条是 candidate_profile，不在官方侧离线判据门里执行。
    - SPEC-WS-005：incremental_tools 默认开启后，generate=false 的预热帧 input 是空数组，additional_tools 前缀由第一条业务帧带出。
    - SPEC-EP-022：图像头线序加入 x-codex-image-turn-id、x-codex-turn-metadata（紧跟 version）。
    样本的 body 摘要由 relay_extract.summarize_body 从真实 JSON 生成，与官方证据派生的 data.body／data.shape 同构。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = json.loads(
            (TOOL_ROOT / "candidate_rule_expectations_0_164_0.json").read_text(encoding="utf-8")
        )

    def check(self, rule_id: str, check_id: str):
        rule = next(item for item in self.profile["rules"] if item["rule_id"] == rule_id)
        return rule, next(item for item in rule["checks"] if item["id"] == check_id)

    def run_check(self, rule_id: str, check_id: str, observations: list) -> bool:
        rule, check = self.check(rule_id, check_id)
        matched = candidate_rule_assertion._select_observations(
            observations, check["select"], rule["scenario_ids"]
        )
        passed, _ = candidate_rule_assertion._evaluate_assertion(matched, check["assertion"])
        return passed

    @staticmethod
    def refresh_observation(body: dict, *, path: str = "/oauth/token"):
        summary = relay_extract.summarize_body(json.dumps(body).encode(), "", "application/json")
        return candidate_rule_assertion.Observation(
            record_id="A13-refresh",
            scenario_id="A13",
            record_type="http_request",
            artifact_path="relay/conn006.client_to_upstream.bin",
            evidence_paths=("relay/conn006.client_to_upstream.bin",),
            labels={"transport": "http", "provider": "openai_oauth"},
            data={"method": "POST", "path": path, "body": summary},
        )

    @staticmethod
    def frame_observation(frame: dict, *, role: str | None = None):
        summary = relay_extract.summarize_body(json.dumps(frame).encode(), "", "application/json")
        labels = {"transport": "websocket", "provider": "openai_oauth"}
        if role is not None:
            labels["frame_role"] = role
        return candidate_rule_assertion.Observation(
            record_id=f"A06-frame-{role}",
            scenario_id="A06",
            record_type="websocket_frame",
            artifact_path="relay/conn001.client_to_upstream.bin",
            evidence_paths=("relay/conn001.client_to_upstream.bin",),
            labels=labels,
            data={"event_type": "response.create", "top_level_fields_in_order": summary["top_level_fields_in_order"],
                  "shape": summary["shape"]},
        )

    def test_refresh_body_keys_and_reason_closed_set(self) -> None:
        good = {"client_id": "app_xxxxxxxxxxxxxxxxxxxxxxx", "grant_type": "refresh_token",
                "refresh_reason": "forced", "refresh_token": "x" * 211}
        self.assertTrue(self.run_check("SPEC-EP-002", "oauth-refresh-body-keys", [self.refresh_observation(good)]))
        self.assertTrue(self.run_check("SPEC-EP-002", "oauth-refresh-reason-closed-set", [self.refresh_observation(good)]))
        # 0.163.0 的三键形态：缺 refresh_reason，两个判据都失败。
        legacy = {k: v for k, v in good.items() if k != "refresh_reason"}
        self.assertFalse(self.run_check("SPEC-EP-002", "oauth-refresh-body-keys", [self.refresh_observation(legacy)]))
        self.assertFalse(self.run_check("SPEC-EP-002", "oauth-refresh-reason-closed-set", [self.refresh_observation(legacy)]))
        # 键序必须是字典序（BTreeMap），refresh_reason 放在末尾不行。
        reordered = {"client_id": good["client_id"], "grant_type": "refresh_token",
                     "refresh_token": good["refresh_token"], "refresh_reason": "forced"}
        self.assertFalse(self.run_check("SPEC-EP-002", "oauth-refresh-body-keys", [self.refresh_observation(reordered)]))
        # 四个官方取值都放行，闭集之外的值（含子串、大小写变体）失败。
        for reason in ("proactive_expiry", "scheduled", "unauthorized_recovery", "forced"):
            with self.subTest(reason=reason):
                self.assertTrue(self.run_check(
                    "SPEC-EP-002", "oauth-refresh-reason-closed-set",
                    [self.refresh_observation({**good, "refresh_reason": reason})]))
        for reason in ("forced2", "FORCED", "manual", ""):
            with self.subTest(reason=reason):
                self.assertFalse(self.run_check(
                    "SPEC-EP-002", "oauth-refresh-reason-closed-set",
                    [self.refresh_observation({**good, "refresh_reason": reason})]))
        # 选不到样本（别的路径）时失败关闭，而不是空通过。
        self.assertFalse(self.run_check(
            "SPEC-EP-002", "oauth-refresh-body-keys", [self.refresh_observation(good, path="/other")]))

    def test_refresh_checks_do_not_change_rule_modes(self) -> None:
        """EP-002 因 Files 三跳判据读内部记录类型，整条规则本来就是 candidate_profile；新增的两条刷新判据只读线上 http_request，不改变分组。

        官方侧的离线判据门只执行 dual_wire 规则，所以官方证据里 refresh_reason 的真实取值靠 VC-1 的事实核对（facts_summary）确认，
        候选侧在验收（VC-5）时用这两条判据核对。
        """

        modes = acceptance_contract.derive_validation_modes(self.profile)
        self.assertEqual(modes["SPEC-EP-002"], "candidate_profile")
        old = json.loads((TOOL_ROOT / "candidate_rule_expectations_0_163_0.json").read_text(encoding="utf-8"))
        self.assertEqual(modes, acceptance_contract.derive_validation_modes(old))

    def test_warmup_frame_input_is_empty_and_prefix_comes_with_first_business_frame(self) -> None:
        tools = [{"type": "function", "name": "shell"}]
        warmup_empty = {"type": "response.create", "model": "gpt-6.1-sol", "input": [], "generate": False}
        warmup_legacy = {"type": "response.create", "model": "gpt-6.1-sol", "generate": False,
                         "input": [{"type": "additional_tools", "role": "developer", "tools": tools},
                                   {"type": "message", "role": "developer", "content": []}]}
        business_with_prefix = {"type": "response.create", "model": "gpt-6.1-sol", "previous_response_id": "resp_1",
                                "input": [{"type": "additional_tools", "role": "developer", "tools": tools},
                                          {"type": "message", "role": "developer", "content": []},
                                          {"type": "message", "role": "user", "content": []}]}
        business_without_prefix = {"type": "response.create", "model": "gpt-6.1-sol", "previous_response_id": "resp_1",
                                   "input": [{"type": "message", "role": "user", "content": []}]}
        # 预热帧：0.164 形态（空数组）通过；0.163 形态（带前缀）失败；选不到预热帧时失败关闭。
        self.assertTrue(self.run_check("SPEC-WS-005", "warmup-input-empty", [self.frame_observation(warmup_empty, role="warmup")]))
        self.assertFalse(self.run_check("SPEC-WS-005", "warmup-input-empty", [self.frame_observation(warmup_legacy, role="warmup")]))
        self.assertFalse(self.run_check("SPEC-WS-005", "warmup-input-empty", [self.frame_observation(business_with_prefix)]))
        # 预热判定只看 generate=false，不依赖候选侧的 frame_role 标签。
        self.assertTrue(self.run_check("SPEC-WS-005", "warmup-input-empty", [self.frame_observation(warmup_empty)]))
        # 第一条业务帧：前缀排在 input 最前才通过；前缀不在最前（0.164.0 官方形态里它总是最前）或根本没有都失败；
        # 没有标签的帧不会被选中，选不到时失败关闭。
        self.assertTrue(self.run_check(
            "SPEC-WS-005", "first-business-frame-carries-prefix",
            [self.frame_observation(business_with_prefix, role="first_business")]))
        self.assertFalse(self.run_check(
            "SPEC-WS-005", "first-business-frame-carries-prefix",
            [self.frame_observation(business_without_prefix, role="first_business")]))
        business_prefix_not_first = {
            **business_with_prefix,
            "input": [business_with_prefix["input"][2], *business_with_prefix["input"][:2]],
        }
        self.assertFalse(self.run_check(
            "SPEC-WS-005", "first-business-frame-carries-prefix",
            [self.frame_observation(business_prefix_not_first, role="first_business")]))
        self.assertFalse(self.run_check(
            "SPEC-WS-005", "first-business-frame-carries-prefix", [self.frame_observation(business_with_prefix)]))

    def test_first_business_frame_chains_the_warmup_response(self) -> None:
        tools = [{"type": "function", "name": "shell"}]
        prefix = [{"type": "additional_tools", "role": "developer", "tools": tools}]
        chained = {"type": "response.create", "model": "gpt-6.1-sol", "previous_response_id": "resp_informal_0001", "input": prefix}
        unchained = {"type": "response.create", "model": "gpt-6.1-sol", "input": prefix}
        self.assertTrue(self.run_check(
            "SPEC-WS-005", "first-business-frame-chains-warmup", [self.frame_observation(chained, role="first_business")]))
        # 没有 previous_response_id（0.163 之前的整段重放形态）失败；没有标签帧时失败关闭。
        self.assertFalse(self.run_check(
            "SPEC-WS-005", "first-business-frame-chains-warmup", [self.frame_observation(unchained, role="first_business")]))
        self.assertFalse(self.run_check(
            "SPEC-WS-005", "first-business-frame-chains-warmup", [self.frame_observation(chained)]))

    def test_image_header_order_lists_the_two_new_headers_right_after_version(self) -> None:
        _, check = self.check("SPEC-EP-022", "image-header-order")
        order = check["assertion"]["value"]
        self.assertEqual(
            order,
            ["version", "x-codex-image-turn-id", "x-codex-turn-metadata", "authorization", "chatgpt-account-id",
             "content-type", "originator", "user-agent", "accept", "cookie", "host", "content-length"],
        )
        # IMG 路 0.164.0 非正式实采（20 条请求）：两个新头紧跟 version、在 authorization 之前，image-turn-id 在前；
        # 默认路径下官方不写 originator 头（线程 originator 等于进程 originator），由 HTTP 客户端默认头补在 content-type 之后，
        # 所以 originator 不在两个新头之间；user-agent 仍在 accept 之前。
        self.assertEqual(order[:3], ["version", "x-codex-image-turn-id", "x-codex-turn-metadata"])
        self.assertLess(order.index("x-codex-turn-metadata"), order.index("authorization"))
        self.assertLess(order.index("content-type"), order.index("originator"))
        self.assertLess(order.index("user-agent"), order.index("accept"))

    def test_other_rules_are_untouched_by_the_increment(self) -> None:
        """差异报告只落了四条规则；其余规则的检查集合与 0.163.0 逐项相同（版本号换回后比较）。"""

        raw = (TOOL_ROOT / "candidate_rule_expectations_0_164_0.json").read_text(encoding="utf-8")
        reverted = json.loads(raw.replace("0.164.0", "0.163.0").replace(r"0\\.164\\.0", r"0\\.163\\.0"))
        old = json.loads((TOOL_ROOT / "candidate_rule_expectations_0_163_0.json").read_text(encoding="utf-8"))
        changed = sorted(
            new["rule_id"] for new, before in zip(reverted["rules"], old["rules"]) if new != before
        )
        self.assertEqual(changed, ["SPEC-BODY-003", "SPEC-EP-002", "SPEC-EP-022", "SPEC-WS-005"])
        self.assertEqual(len(reverted["rules"]), len(old["rules"]))


class MainTrackLiteExpectation0164Tests(MainTrackLiteExpectationTests):
    """0.164.0 候选期望清单（由 0.163.0 终态复制）同样满足主线 Lite 的 BODY-006 检查与 EP-001 约束。"""

    MANIFEST_SUFFIX = "0_164_0"


if __name__ == "__main__":
    unittest.main()
