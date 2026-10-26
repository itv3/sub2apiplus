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
    - SPEC-EP-022：图像头线序加入 x-codex-image-turn-id、x-codex-turn-metadata（紧跟 version）；图像请求的 x-codex-turn-metadata 必须是官方默认路径 14 键同序
      （依赖 relay_extract 新增的请求级 turn_metadata_keys；头值本身仍脱敏）。
    - SPEC-BODY-006：Lite 前缀两项的形态（additional_tools 键序与 id 形态、基础指令消息 id 形态与 passthrough 键序，不核对 UUID 版本位）；
      依赖 relay_extract 给 input 附的头两项形态 ``_head``。
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

    # ---- SPEC-BODY-006 前缀两项形态（依赖 relay_extract.shape_top_level 给 input 附的 _head） ----

    V7_AT = "at_01a17935-5c98-7f51-995d-9105098f672a"
    V7_MSG = "msg_01a17935-5c98-7f51-995d-9110469c51d5"
    V5_AT = "at_2b6266ff-2d0f-5660-9fdb-a206367297ef"
    V5_MSG = "msg_e938c4f7-c15e-5d89-a7a7-7492156931d4"

    def prefix_body(self, *, at_id=V7_AT, msg_id=V7_MSG, passthrough_keys=("turn_id", "create_time", "content_item_kinds", "content_item_metadata"),
                    first_type="additional_tools") -> dict:
        passthrough = {key: [] for key in passthrough_keys}
        return {
            "model": "gpt-6.1-sol",
            "input": [
                {"type": first_type, "id": at_id, "role": "developer", "tools": [{"type": "function", "name": "shell"}]},
                {"type": "message", "id": msg_id, "role": "developer", "content": [{"type": "input_text", "text": "x"}],
                 "internal_chat_message_metadata_passthrough": passthrough},
                {"type": "message", "role": "user", "content": []},
            ],
        }

    def http_observation(self, body: dict):
        summary = relay_extract.summarize_body(json.dumps(body).encode(), "", "application/json")
        return candidate_rule_assertion.Observation(
            record_id="A04-lite", scenario_id="A04", record_type="http_request",
            artifact_path="relay/conn001.client_to_upstream.bin", evidence_paths=("relay/conn001.client_to_upstream.bin",),
            labels={"transport": "http", "mode": "lite", "provider": "openai_oauth", "track": "main"},
            data={"method": "POST", "path": "/backend-api/codex/responses", "body": summary},
        )

    def run_prefix_checks(self, body: dict) -> dict:
        return {
            check_id: self.run_check("SPEC-BODY-006", check_id, [self.http_observation(body)])
            for check_id in ("main-additional-tools-item-form", "main-base-instructions-item-form",
                             "main-base-instructions-passthrough-keys")
        }

    def test_lite_prefix_items_in_the_0164_form_pass(self) -> None:
        self.assertEqual(set(self.run_prefix_checks(self.prefix_body()).values()), {True})
        # 压缩后的新窗口：基础指令消息缺 create_time（官方 0.164.0 实采 c1 第 4 帧），仍是有序子集。
        self.assertEqual(
            set(self.run_prefix_checks(self.prefix_body(passthrough_keys=("turn_id", "content_item_kinds", "content_item_metadata"))).values()),
            {True},
        )
        # 旧窗口续聊（0.163 建的会话由 0.164 续聊，r1 样本）：id 仍是 UUIDv5，passthrough 只有 content_item_kinds、content_item_metadata。
        # 判据不核对 UUID 版本位，所以通过。
        self.assertEqual(
            set(self.run_prefix_checks(self.prefix_body(
                at_id=self.V5_AT, msg_id=self.V5_MSG, passthrough_keys=("content_item_kinds", "content_item_metadata"))).values()),
            {True},
        )

    def test_lite_prefix_items_in_the_0163_form_or_malformed_fail(self) -> None:
        # 0.163 形态：基础指令消息 passthrough 只有 content_item_kinds，缺 content_item_metadata。
        results = self.run_prefix_checks(self.prefix_body(passthrough_keys=("content_item_kinds",)))
        self.assertTrue(results["main-additional-tools-item-form"])
        self.assertFalse(results["main-base-instructions-passthrough-keys"])
        # 键序错（content_item_kinds 在 turn_id 前）失败。
        self.assertFalse(self.run_prefix_checks(
            self.prefix_body(passthrough_keys=("content_item_kinds", "content_item_metadata", "turn_id")))["main-base-instructions-passthrough-keys"])
        # id 不是 <前缀>_规范 UUID：additional_tools 项、基础指令消息各自失败。
        self.assertFalse(self.run_prefix_checks(self.prefix_body(at_id="at_not-a-uuid"))["main-additional-tools-item-form"])
        self.assertFalse(self.run_prefix_checks(self.prefix_body(msg_id="msg_1234"))["main-base-instructions-item-form"])
        # id 前缀错（把基础指令消息的 id 写成 at_）失败。
        self.assertFalse(self.run_prefix_checks(self.prefix_body(msg_id=self.V7_AT))["main-base-instructions-item-form"])
        # 首项不是 additional_tools（前缀被挤走）失败。
        self.assertFalse(self.run_prefix_checks(self.prefix_body(first_type="message"))["main-additional-tools-item-form"])

    def test_input_head_summary_is_structure_only_and_shared_by_all_summarizers(self) -> None:
        body = self.prefix_body()
        summary = relay_extract.summarize_body(json.dumps(body).encode(), "", "application/json")
        head = summary["shape"]["input"]["_head"]
        self.assertEqual(head["0"], {"keys": ["type", "id", "role", "tools"], "type": "additional_tools", "id_form": "at_uuid"})
        self.assertEqual(head["1"]["id_form"], "msg_uuid")
        self.assertEqual(head["1"]["passthrough_keys"], ["turn_id", "create_time", "content_item_kinds", "content_item_metadata"])
        self.assertNotIn("2", head)
        # 摘要里没有 UUID 本身，也没有版本位，更没有对话内容。
        serialized = json.dumps(summary)
        for secret in (self.V7_AT, self.V7_MSG, "01a17935"):
            self.assertNotIn(secret, serialized)
        # 空 input 与没有 input 的请求体不产生 _head。
        self.assertNotIn("_head", json.dumps(relay_extract.summarize_body(b'{"input": []}', "", "application/json")))
        self.assertNotIn("_head", json.dumps(relay_extract.summarize_body(b'{"model": "x"}', "", "application/json")))
        # 三条路径同构：shape_top_level（summarize_body 与 WS 帧）和 mitm 派生用同一个函数。
        self.assertEqual(relay_extract.shape_top_level(body), summary["shape"])
        from tools.official_client_capture import derive_official_observations as derive

        self.assertEqual(derive._mitm_body_summary({"json": body})["shape"], summary["shape"])

    # ---- SPEC-EP-022：图像请求 x-codex-turn-metadata 的 14 键（依赖 relay_extract 新增的 turn_metadata_keys） ----

    # 官方 CLI 默认路径（exec，IMG 路对 0.164.0 的实采原文换成占位值）：键序是 swap_remove 的副作用。
    IMAGE_TURN_METADATA_OFFICIAL = {
        "session_id": "s", "thread_id": "s", "reasoning_effort": "low", "turn_id": "t", "model": "gpt-6.1-sol",
        "thread_source": "user", "turn_trigger": "exec", "sandbox": "seccomp", "sandbox_mode": "read-only",
        "auto_review_enabled": False, "node_repl_auto_review_required": True, "node_repl_disabled": False,
        "turn_started_at_unix_ms": 1, "codex_version": "0.164.0",
    }

    def image_request_bytes(self, metadata: dict | None) -> bytes:
        body = json.dumps({"prompt": "p", "background": "opaque", "model": "gpt-image-2", "quality": "auto", "size": "auto"}).encode()
        headers = ["POST /backend-api/codex/images/generations HTTP/1.1", "version: 0.164.0", "x-codex-image-turn-id: t"]
        if metadata is not None:
            headers.append("x-codex-turn-metadata: " + json.dumps(metadata, separators=(",", ":")))
        headers += ["authorization: Bearer x", "content-type: application/json", "host: chatgpt.com", f"content-length: {len(body)}"]
        return ("\r\n".join(headers) + "\r\n\r\n").encode() + body

    def image_observations(self, metadata: dict | None) -> list:
        from tools.official_client_capture import derive_official_observations as derive

        records = derive._derive_from_h1(
            self.image_request_bytes(metadata),
            {"connection_id": "conn005", "target": "scenarios/A09/relay/conn005.client_to_upstream.bin", "scenario_id": "A09",
             "source": "scenarios/A09/relay/conn005.client_to_upstream.bin"},
        )
        return [
            candidate_rule_assertion.Observation(
                record_id=record["record_id"], scenario_id="A09", record_type=record["record_type"],
                artifact_path="x", evidence_paths=("x",),
                labels={"transport": "http", "endpoint_class": "images", "provider": "openai_oauth"}, data=record["data"],
            )
            for record in records
        ]

    def test_image_turn_metadata_keys_are_visible_while_values_stay_redacted(self) -> None:
        requests = relay_extract.parse_h1_stream(self.image_request_bytes(self.IMAGE_TURN_METADATA_OFFICIAL))
        self.assertEqual(requests[0]["turn_metadata_keys"], list(self.IMAGE_TURN_METADATA_OFFICIAL))
        header = next(h for h in requests[0]["headers"] if h["name"] == "x-codex-turn-metadata")
        self.assertTrue(header["value"].startswith("<redacted len="), header)
        # 没有这个头、头值不是 JSON 对象的请求不产生该字段。
        self.assertNotIn("turn_metadata_keys", relay_extract.parse_h1_stream(self.image_request_bytes(None))[0])
        self.assertNotIn("turn_metadata_keys", relay_extract.parse_h1_stream(
            b"GET /x HTTP/1.1\r\nx-codex-turn-metadata: not-json\r\nhost: h\r\n\r\n")[0])
        # 派生器与断言器直接解析原件的结果同构（模块文档的约定）。
        import tempfile

        derived = self.image_observations(self.IMAGE_TURN_METADATA_OFFICIAL)[0].data["turn_metadata_keys"]
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "conn005.client_to_upstream.bin"
            raw.write_bytes(self.image_request_bytes(self.IMAGE_TURN_METADATA_OFFICIAL))
            direct = candidate_rule_assertion._h1_observations(raw, "conn005.client_to_upstream.bin", ["A09"], {}, {})
        self.assertEqual(derived, direct[0].data["turn_metadata_keys"])

    def test_image_turn_metadata_default_keys_check(self) -> None:
        check = "image-turn-metadata-default-keys"
        self.assertTrue(self.run_check("SPEC-EP-022", check, self.image_observations(self.IMAGE_TURN_METADATA_OFFICIAL)))
        # 补丁漏填 SessionDefaults：代码静默退回 12 键（没有 model、reasoning_effort），判据必须拦住。
        twelve = {k: v for k, v in self.IMAGE_TURN_METADATA_OFFICIAL.items() if k not in {"model", "reasoning_effort"}}
        self.assertFalse(self.run_check("SPEC-EP-022", check, self.image_observations(twelve)))
        # 只缺 reasoning_effort（13 键）、键序不对（model 排在 turn_id 前）也失败。
        self.assertFalse(self.run_check("SPEC-EP-022", check, self.image_observations(
            {k: v for k, v in self.IMAGE_TURN_METADATA_OFFICIAL.items() if k != "reasoning_effort"})))
        reordered = {"session_id": "s", "thread_id": "s", "turn_id": "t", "model": "m", "reasoning_effort": "low",
                     **{k: v for k, v in self.IMAGE_TURN_METADATA_OFFICIAL.items()
                        if k not in {"session_id", "thread_id", "turn_id", "model", "reasoning_effort"}}}
        self.assertFalse(self.run_check("SPEC-EP-022", check, self.image_observations(reordered)))
        # 请求根本没带这个头也失败（而不是空通过）；选不到样本同理。
        self.assertFalse(self.run_check("SPEC-EP-022", check, self.image_observations(None)))
        self.assertFalse(self.run_check("SPEC-EP-022", check, []))

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
        """差异报告只落了五条规则；其余规则的检查集合与 0.163.0 逐项相同（版本号换回后比较）。"""

        raw = (TOOL_ROOT / "candidate_rule_expectations_0_164_0.json").read_text(encoding="utf-8")
        reverted = json.loads(raw.replace("0.164.0", "0.163.0").replace(r"0\\.164\\.0", r"0\\.163\\.0"))
        old = json.loads((TOOL_ROOT / "candidate_rule_expectations_0_163_0.json").read_text(encoding="utf-8"))
        changed = sorted(
            new["rule_id"] for new, before in zip(reverted["rules"], old["rules"]) if new != before
        )
        self.assertEqual(changed, ["SPEC-BODY-003", "SPEC-BODY-006", "SPEC-EP-002", "SPEC-EP-022", "SPEC-WS-005"])
        self.assertEqual(len(reverted["rules"]), len(old["rules"]))


class MainTrackLiteExpectation0164Tests(MainTrackLiteExpectationTests):
    """0.164.0 候选期望清单（由 0.163.0 终态复制）同样满足主线 Lite 的 BODY-006 检查与 EP-001 约束。"""

    MANIFEST_SUFFIX = "0_164_0"


if __name__ == "__main__":
    unittest.main()
