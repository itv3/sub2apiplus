"""0.163.0 起主线是 Lite 模型（其后各版沿用）：BODY-006 里原来依赖非 Lite 样本的两个检查已改写成主线 Lite 形态。

这里用手造的 Lite／非 Lite 请求体样本跑改写后检查的正反例，并锁定 EP-001 的 nonlite-image-tool 保持原样的理由
（它读候选侧 Go 追踪测试事实，不依赖官方主线样本）。声明本身与各处载荷的一致性见 test_main_track_models.py。
"""

from __future__ import annotations

import dataclasses
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

    清单后缀写成类属性，其后各版由下面的子类复用同一组断言（新清单由上一版终态复制，判据随差异报告改写时各份都要继续满足）。
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
    """0.164.0 差异报告落进候选期望清单的新判据：手造观测跑正反例（后缀写成类属性，0.165.0 由子类复用）。

    - SPEC-EP-002：OAuth 刷新体多一个 refresh_reason（键序 client_id, grant_type, refresh_reason, refresh_token，取值四选一）。
    这四条规则（EP-002、WS-005、BODY-003、EP-022）都含读内部记录类型的判据，整条是 candidate_profile，不在官方侧离线判据门里执行。
    - SPEC-WS-005：incremental_tools 默认开启后，generate=false 的预热帧 input 是空数组，additional_tools 前缀由第一条业务帧带出。
    - SPEC-EP-022：图像头线序加入 x-codex-image-turn-id、x-codex-turn-metadata（紧跟 version）；图像请求的 x-codex-turn-metadata 必须是官方默认路径 14 键同序
      （依赖 relay_extract 新增的请求级 turn_metadata_keys；头值本身仍脱敏）。
    - SPEC-BODY-003：Lite 前缀两项的形态（additional_tools 键序与 id 形态、基础指令消息 id 形态与 passthrough 键序，不核对 UUID 版本位）；
      依赖 relay_extract 给 input 附的头两项形态 ``_head``。这几条判据**落在 BODY-003（整条 candidate_profile，官方侧不执行）而不是 BODY-006**：
      BODY-006 是 dual_wire，官方证据上也会执行，而官方非 Lite 请求本来就不带 content_item_metadata，放在那里正式采集时可能误红（调度者 10-27 决定）。
    样本的 body 摘要由 relay_extract.summarize_body 从真实 JSON 生成，与官方证据派生的 data.body／data.shape 同构。
    """

    MANIFEST_SUFFIX = "0_164_0"

    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = json.loads(
            (TOOL_ROOT / f"candidate_rule_expectations_{cls.MANIFEST_SUFFIX}.json").read_text(encoding="utf-8")
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

    # ---- SPEC-BODY-003 前缀两项形态（依赖 relay_extract.shape_top_level 给 input 附的 _head；候选侧执行，官方侧不执行） ----

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
            check_id: self.run_check("SPEC-BODY-003", check_id, [self.http_observation(body)])
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

    # ---- W1b 逐项 passthrough 保真（依赖 relay_extract 的 input._passthrough；BODY-003 的 HTTP 判据与 WS-005 首个业务帧判据，候选侧执行） ----

    PASSTHROUGH_HTTP_IDS = (
        "main-passthrough-key-orders", "main-passthrough-assistant-key-order", "main-passthrough-carrier-types",
        "main-messages-keep-passthrough", "main-passthrough-turn-id-form", "main-current-turn-item-turn-id",
        "main-client-turn-id-matches-turn-metadata",
    )
    PASSTHROUGH_WS_IDS = (
        "first-business-frame-passthrough-key-orders", "first-business-frame-messages-keep-passthrough",
        "first-business-frame-passthrough-turn-id-form", "first-business-frame-current-turn-item-turn-id",
        "first-business-frame-client-turn-id-matches-turn-metadata",
    )
    TURN_NOW = "01a17935-c8dc-7cb2-8b5b-c9b98611bb03"
    TURN_OLD = "01a17935-0000-7cb2-8b5b-c9b98611bb99"
    FOUR_KEYS = ("turn_id", "create_time", "content_item_kinds", "content_item_metadata")

    def passthrough_item(self, kind, role, keys, turn_id=None) -> dict:
        passthrough = {key: (turn_id if key == "turn_id" else []) for key in keys}
        item: dict = {"type": kind, "content": []}
        if role:
            item["role"] = role
        item["internal_chat_message_metadata_passthrough"] = passthrough
        return item

    def official_input(self, *, turn=None) -> list:
        """官方 Lite 新窗口加一轮历史的请求：前缀两项、历史助手消息、当前回合的用户消息与工具调用。"""

        turn = turn or self.TURN_NOW
        return [
            {"type": "additional_tools", "id": self.V7_AT, "role": "developer", "tools": []},
            {**self.passthrough_item("message", "developer", self.FOUR_KEYS, turn_id=self.TURN_OLD), "id": self.V7_MSG},
            self.passthrough_item("message", "user", self.FOUR_KEYS, turn_id=self.TURN_OLD),
            self.passthrough_item("message", "assistant", ("turn_id", "content_item_kinds"), turn_id=self.TURN_OLD),
            self.passthrough_item("message", "user", self.FOUR_KEYS, turn_id=turn),
            self.passthrough_item("function_call", None, self.FOUR_KEYS + ("cell_id",), turn_id=turn),
        ]

    def passthrough_body(self, items: list, *, client_turn=None, metadata_turn=None) -> dict:
        client_turn = client_turn or self.TURN_NOW
        return {
            "model": "gpt-6.1-sol", "input": items,
            "client_metadata": {"turn_id": client_turn, "x-codex-turn-metadata": json.dumps({"turn_id": metadata_turn or client_turn})},
        }

    def run_passthrough_http(self, body: dict) -> dict:
        return {
            check_id: self.run_check("SPEC-BODY-003", check_id, [self.http_observation(body)])
            for check_id in self.PASSTHROUGH_HTTP_IDS
        }

    def run_passthrough_ws(self, frame: dict) -> dict:
        frame = {"type": "response.create", **frame}
        return {
            check_id: self.run_check("SPEC-WS-005", check_id, [self.frame_observation(frame, role="first_business")])
            for check_id in self.PASSTHROUGH_WS_IDS
        }

    # SIM-06：HTTP 的 main-messages-keep-passthrough 期望“恰好 1 条 message 无 passthrough”（A04 入站是第三方形态，网关只给自己合成的基础指令消息带
    # passthrough，不为第三方用户消息合成，与 ARM64 批准画像 b2 一致）。官方形态入站（每个 message 都带 passthrough）在这条判据上因此是 0，不通过：
    # 这条判据只服务 A04 的第三方入站，官方形态入站原样保留逐项 passthrough 由 Go 单测覆盖。WS 同名判据不变，仍要求 0。
    SIM06_HTTP_ID = "main-messages-keep-passthrough"

    def test_official_form_passthrough_passes_http_and_ws(self) -> None:
        body = self.passthrough_body(self.official_input())
        http = self.run_passthrough_http(body)
        self.assertEqual({key for key, value in http.items() if not value}, {self.SIM06_HTTP_ID})
        self.assertEqual(set(self.run_passthrough_ws(body).values()), {True})
        # 旧窗口续聊（r1 样本形态）：基础指令消息只有 content_item_kinds,content_item_metadata，旧历史项三键；除 SIM-06 判据外仍通过。
        legacy = self.official_input()
        legacy[1] = {**self.passthrough_item("message", "developer", ("content_item_kinds", "content_item_metadata")), "id": self.V5_MSG}
        legacy[2] = self.passthrough_item("message", "user", ("turn_id", "create_time", "content_item_kinds"), turn_id=self.TURN_OLD)
        legacy_http = self.run_passthrough_http(self.passthrough_body(legacy))
        self.assertEqual({key for key, value in legacy_http.items() if not value}, {self.SIM06_HTTP_ID})

    def test_sim06_third_party_inbound_has_exactly_one_message_without_passthrough(self) -> None:
        """SIM-06：A04 第三方入站，网关只给基础指令消息带 passthrough，用户消息恰好 1 条没有——通过；
        基础指令消息的 passthrough 也被剥离（变 2）或开始为第三方用户消息合成（变 0）都必须失败并重新审核。"""

        def third_party(*, base_has_passthrough: bool, user_has_passthrough: bool) -> dict:
            base = {"type": "message", "role": "developer", "id": self.V7_MSG, "content": []}
            if base_has_passthrough:
                base["internal_chat_message_metadata_passthrough"] = {key: ([] if key != "turn_id" else self.TURN_NOW) for key in self.FOUR_KEYS}
            user = {"type": "message", "role": "user", "content": []}
            if user_has_passthrough:
                user["internal_chat_message_metadata_passthrough"] = {"turn_id": self.TURN_NOW}
            items = [{"type": "additional_tools", "id": self.V7_AT, "role": "developer", "tools": []}, base, user]
            return self.passthrough_body(items)

        def verdict(body: dict) -> bool:
            return self.run_check("SPEC-BODY-003", self.SIM06_HTTP_ID, [self.http_observation(body)])

        self.assertTrue(verdict(third_party(base_has_passthrough=True, user_has_passthrough=False)))
        self.assertFalse(verdict(third_party(base_has_passthrough=False, user_has_passthrough=False)))
        self.assertFalse(verdict(third_party(base_has_passthrough=True, user_has_passthrough=True)))
        # 判据值本身就是 1（与批准画像 b2 逐字一致，不是 0）。
        _, check = self.check("SPEC-BODY-003", self.SIM06_HTTP_ID)
        self.assertEqual(check["assertion"]["value"], 1)

    def test_stripped_passthrough_fails(self) -> None:
        """网关把逐项 passthrough 整体剥离（保真失败）：message 不带 passthrough，保真判据与键序、turn_id 判据都红。"""

        stripped = [
            {key: value for key, value in item.items() if key != "internal_chat_message_metadata_passthrough"}
            for item in self.official_input()
        ]
        results = self.run_passthrough_http(self.passthrough_body(stripped))
        self.assertFalse(results["main-messages-keep-passthrough"])
        self.assertFalse(results["main-passthrough-key-orders"])
        self.assertFalse(results["main-current-turn-item-turn-id"])
        self.assertFalse(self.run_passthrough_ws(self.passthrough_body(stripped))["first-business-frame-messages-keep-passthrough"])

    def test_turn_id_not_rewritten_or_split_fails(self) -> None:
        # 当前回合的项保留了客户端旧 turn_id（没有改写成本轮值）：尾项与 client_metadata.turn_id 不等。
        stale = self.official_input(turn=self.TURN_OLD)
        results = self.run_passthrough_http(self.passthrough_body(stale))
        self.assertFalse(results["main-current-turn-item-turn-id"])
        self.assertTrue(results["main-client-turn-id-matches-turn-metadata"])
        self.assertFalse(self.run_passthrough_ws(self.passthrough_body(stale))["first-business-frame-current-turn-item-turn-id"])
        # client_metadata.turn_id 与 turn metadata 里的 turn_id 不一致（W1b 10.3 的既有缺陷）：一致性判据红，项级判据不受影响。
        split = self.passthrough_body(self.official_input(), metadata_turn=self.TURN_OLD)
        self.assertFalse(self.run_passthrough_http(split)["main-client-turn-id-matches-turn-metadata"])
        self.assertTrue(self.run_passthrough_http(split)["main-current-turn-item-turn-id"])
        self.assertFalse(self.run_passthrough_ws(split)["first-business-frame-client-turn-id-matches-turn-metadata"])
        # 项级 turn_id 不是规范 UUID。
        bad_form = self.official_input()
        bad_form[4] = self.passthrough_item("message", "user", self.FOUR_KEYS, turn_id="not-a-uuid")
        self.assertFalse(self.run_passthrough_http(self.passthrough_body(bad_form))["main-passthrough-turn-id-form"])

    def test_malformed_passthrough_keys_and_carriers_fail(self) -> None:
        # 乱序（content_item_kinds 在 create_time 前）、多出官方没有的键。
        for keys in (("turn_id", "content_item_kinds", "create_time"), ("turn_id", "create_time", "content_item_kinds", "foreign_key")):
            items = self.official_input()
            items[4] = self.passthrough_item("message", "user", keys, turn_id=self.TURN_NOW)
            self.assertFalse(self.run_passthrough_http(self.passthrough_body(items))["main-passthrough-key-orders"], keys)
        # 助手消息多了 create_time（官方助手消息只有 turn_id,content_item_kinds）。
        items = self.official_input()
        items[3] = self.passthrough_item("message", "assistant", ("turn_id", "create_time", "content_item_kinds"), turn_id=self.TURN_OLD)
        self.assertFalse(self.run_passthrough_http(self.passthrough_body(items))["main-passthrough-assistant-key-order"])
        # additional_tools 项被网关合成了 passthrough（官方里它没有该字段）。
        items = self.official_input()
        items[0] = {**items[0], "internal_chat_message_metadata_passthrough": {"turn_id": self.TURN_NOW, "content_item_kinds": []}}
        self.assertFalse(self.run_passthrough_http(self.passthrough_body(items))["main-passthrough-carrier-types"])

    def test_http_compaction_requests_are_not_selected_for_passthrough_checks(self) -> None:
        """HTTP 压缩请求不适用保真（W1b：仍按旧行为剥离），带 compaction_trigger 的请求不进这些判据的样本。"""

        items = self.official_input() + [{"type": "compaction_trigger"}]
        rule, check = self.check("SPEC-BODY-003", "main-messages-keep-passthrough")
        matched = candidate_rule_assertion._select_observations(
            [self.http_observation(self.passthrough_body(items))], check["select"], rule["scenario_ids"]
        )
        self.assertEqual(matched, [])
        matched = candidate_rule_assertion._select_observations(
            [self.http_observation(self.passthrough_body(self.official_input()))], check["select"], rule["scenario_ids"]
        )
        self.assertEqual(len(matched), 1)

    def test_passthrough_checks_are_candidate_only_and_do_not_touch_dual_wire_rules(self) -> None:
        by_rule = {rule["rule_id"]: rule for rule in self.profile["rules"]}
        for rule_id, ids in (("SPEC-BODY-003", self.PASSTHROUGH_HTTP_IDS), ("SPEC-WS-005", self.PASSTHROUGH_WS_IDS)):
            present = {check["id"] for check in by_rule[rule_id]["checks"]}
            self.assertTrue(set(ids) <= present, rule_id)
        # 官方侧会执行的 dual_wire 规则里没有任何读 _passthrough 的判据。
        payload = acceptance_contract.build_contract_payload(self.profile)
        for rule in self.profile["rules"]:
            if payload["validation_modes"][rule["rule_id"]] != "dual_wire":
                continue
            self.assertNotIn("input._passthrough", json.dumps(rule), rule["rule_id"])
            self.assertNotIn("input._head", json.dumps(rule), rule["rule_id"])
        self.assertEqual(payload["validation_modes"]["SPEC-BODY-003"], "candidate_profile")
        self.assertEqual(payload["validation_modes"]["SPEC-WS-005"], "candidate_profile")
        self.assertEqual(payload["rule_counts"], {"dual_wire": 25, "candidate_profile": 18})

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

    def test_prefix_form_checks_live_in_candidate_only_body_003_not_in_dual_wire_body_006(self) -> None:
        """三条前缀形态判据只在候选侧执行：落 BODY-003（candidate_profile），BODY-006（dual_wire）一条都没有。"""

        prefix_ids = ("main-additional-tools-item-form", "main-base-instructions-item-form", "main-base-instructions-passthrough-keys")
        by_rule = {rule["rule_id"]: rule for rule in self.profile["rules"]}
        body003_ids = [check["id"] for check in by_rule["SPEC-BODY-003"]["checks"]]
        body006_ids = [check["id"] for check in by_rule["SPEC-BODY-006"]["checks"]]
        for check_id in prefix_ids:
            self.assertIn(check_id, body003_ids)
            self.assertNotIn(check_id, body006_ids)
        # 判据选的是 A04，BODY-003 的 scenario_ids 必须覆盖（引擎要求判据场景是规则场景的子集）。
        self.assertEqual(by_rule["SPEC-BODY-003"]["scenario_ids"], ["A03", "A04", "A05"])
        for rule in self.profile["rules"]:
            for check in rule["checks"]:
                self.assertLessEqual(set(check["select"].get("scenario_ids", [])), set(rule["scenario_ids"]), (rule["rule_id"], check["id"]))
        payload = acceptance_contract.build_contract_payload(self.profile)
        self.assertEqual(payload["validation_modes"]["SPEC-BODY-003"], "candidate_profile")
        self.assertEqual(payload["validation_modes"]["SPEC-BODY-006"], "dual_wire")
        self.assertEqual(payload["rule_counts"], {"dual_wire": 25, "candidate_profile": 18})
        # 官方侧覆盖矩阵不因挪动改变：A04 仍由 BODY-006 提供，候选侧 A04 也早已在列。
        self.assertIn("A04", payload["side_coverage"]["official"])
        self.assertIn("A04", payload["side_coverage"]["candidate"])
        # BODY-006 的检查集合回到 0.163.0 的集合（逐 ID 相同）。
        old = json.loads((TOOL_ROOT / "candidate_rule_expectations_0_163_0.json").read_text(encoding="utf-8"))
        old_ids = [check["id"] for rule in old["rules"] if rule["rule_id"] == "SPEC-BODY-006" for check in rule["checks"]]
        # 其后各版在 BODY-006 的末尾追加的判据（只有子类登记）；官方侧会执行，所以每一条都要能被审计。
        self.assertEqual(body006_ids, old_ids + getattr(self, "ADDED_CHECKS_0165", {}).get("SPEC-BODY-006", []))

    def test_other_rules_are_untouched_by_the_increment(self) -> None:
        """差异报告只落了五条规则；其余规则的检查集合与 0.163.0 逐项相同（版本号换回后比较）。"""

        raw = (TOOL_ROOT / "candidate_rule_expectations_0_164_0.json").read_text(encoding="utf-8")
        reverted = json.loads(raw.replace("0.164.0", "0.163.0").replace(r"0\\.164\\.0", r"0\\.163\\.0"))
        # 0.164.0 目标断言画像里不得出现 baseline（0.163.0）版本字面量（codex_upgrade 的目标断言画像校验会拒绝），所以
        # SPEC-BODY-006 两条检查描述里的“0.163.0 起为 Lite 模型”在 0.164.0 文件里写成了“Lite 模型”；这里对 0.163.0 文件
        # 套同一组替换后再比，其余字逐字相同的要求不变。
        old_raw = (TOOL_ROOT / "candidate_rule_expectations_0_163_0.json").read_text(encoding="utf-8")
        for before, after in (("0.163.0 起为 Lite", "Lite"), ("0.163.0 起主线", "主线")):
            old_raw = old_raw.replace(before, after)
        old = json.loads(old_raw)
        changed = sorted(
            new["rule_id"] for new, before in zip(reverted["rules"], old["rules"]) if new != before
        )
        # BODY-006 是 dual_wire（官方侧也执行），不新增任何判据，必须与 0.163.0 逐字相同。
        self.assertEqual(changed, ["SPEC-BODY-003", "SPEC-EP-002", "SPEC-EP-022", "SPEC-WS-005"])
        self.assertEqual(len(reverted["rules"]), len(old["rules"]))


class InputPassthroughSummaryTests(unittest.TestCase):
    """relay_extract 给 input 附的逐项 passthrough 整体形态 ``_passthrough``（0.164.0 LR2）：只记结构与相等结论，不记任何取值。

    三条摘要路径（summarize_body、WS 帧、mitm 派生）共用 shape_top_level，所以这里直接测它，再测 summarize_body 与派生器同构。
    """

    TURN_A = "01a17935-c8dc-7cb2-8b5b-c9b98611bb03"
    TURN_B = "01a17935-0000-7cb2-8b5b-c9b98611bb99"
    FOUR = ["turn_id", "create_time", "content_item_kinds", "content_item_metadata"]

    @staticmethod
    def item(kind: str, role: str | None, keys, *, turn_id: str | None = None) -> dict:
        passthrough = {key: ([] if key != "turn_id" else turn_id) for key in keys}
        entry: dict = {"type": kind, "content": []}
        if role:
            entry["role"] = role
        if passthrough:
            entry["internal_chat_message_metadata_passthrough"] = passthrough
        return entry

    def body(self, items: list, *, client_turn=None, metadata_turn=None, with_metadata=True) -> dict:
        client_metadata: dict = {}
        if client_turn is not None:
            client_metadata["turn_id"] = client_turn
        if with_metadata:
            client_metadata["x-codex-turn-metadata"] = json.dumps({"turn_id": metadata_turn if metadata_turn is not None else client_turn})
        return {"model": "gpt-6.1-sol", "input": items, "client_metadata": client_metadata}

    def summary(self, body: dict) -> dict:
        return relay_extract.shape_top_level(body)["input"]["_passthrough"]

    def test_official_lite_request_summary(self) -> None:
        items = [
            {"type": "additional_tools", "id": "at_x", "role": "developer", "tools": []},
            self.item("message", "developer", self.FOUR, turn_id=self.TURN_A),
            self.item("message", "user", self.FOUR, turn_id=self.TURN_A),
            self.item("message", "assistant", ["turn_id", "content_item_kinds"], turn_id=self.TURN_B),
            self.item("function_call", None, self.FOUR, turn_id=self.TURN_A),
        ]
        summary = self.summary(self.body(items, client_turn=self.TURN_A))
        self.assertEqual(summary, {
            "key_orders": "|".join(sorted([",".join(self.FOUR), "turn_id,content_item_kinds"])),
            "assistant_key_orders": "turn_id,content_item_kinds",
            "carrier_types": ["function_call", "message"],
            "messages_without": 0,
            "turn_id_forms": ["uuid"],
            "tail_turn_id_matches_client": True,
            "client_turn_id_matches_turn_metadata": True,
            # 并入的 turn_id_wire_summary：测试用的 TURN_A 是官方位布局 UUIDv7；夹具的 turn metadata 里没有开始时间、client_metadata 里没有会话标识，两个布尔取不到即 False。
            "client_turn_id_form": "uuidv7",
            "client_turn_started_gap_ok": False,
            "client_session_ids_ok": False,
        })

    def test_stripped_or_inconsistent_requests_are_visible(self) -> None:
        # passthrough 被整体剥离：message 项没有 passthrough，计数非 0，键序为空。
        stripped = self.summary(self.body([self.item("message", "user", []), self.item("message", "user", [])], client_turn=self.TURN_A))
        self.assertEqual(stripped["messages_without"], 2)
        self.assertEqual(stripped["key_orders"], "")
        self.assertFalse(stripped["tail_turn_id_matches_client"])
        # 尾项 turn_id 不是当前回合（保留了客户端旧值没改写）：布尔为 False。
        stale = self.summary(self.body([self.item("message", "user", self.FOUR, turn_id=self.TURN_B)], client_turn=self.TURN_A))
        self.assertFalse(stale["tail_turn_id_matches_client"])
        # client_metadata.turn_id 与 turn metadata 里的 turn_id 不一致（既有缺陷 cbdf22345 修的就是它）。
        split = self.summary(self.body([self.item("message", "user", self.FOUR, turn_id=self.TURN_A)], client_turn=self.TURN_A, metadata_turn=self.TURN_B))
        self.assertTrue(split["tail_turn_id_matches_client"])
        self.assertFalse(split["client_turn_id_matches_turn_metadata"])
        # 没有 client_metadata 或 turn metadata 不是 JSON：失败关闭为 False。
        self.assertFalse(self.summary({"input": [self.item("message", "user", self.FOUR, turn_id=self.TURN_A)]})["tail_turn_id_matches_client"])
        broken = self.body([self.item("message", "user", self.FOUR, turn_id=self.TURN_A)], client_turn=self.TURN_A)
        broken["client_metadata"]["x-codex-turn-metadata"] = "{not json"
        self.assertFalse(self.summary(broken)["client_turn_id_matches_turn_metadata"])
        # turn_id 不是规范 UUID 或不是字符串：形态为 other。
        odd = self.summary(self.body([self.item("message", "user", self.FOUR, turn_id="not-a-uuid"), {
            "type": "message", "role": "user", "internal_chat_message_metadata_passthrough": {"turn_id": 7}}], client_turn=self.TURN_A))
        self.assertEqual(odd["turn_id_forms"], ["other"])

    def test_summary_carries_no_values_and_is_shared_by_all_summarizers(self) -> None:
        body = self.body([self.item("message", "user", self.FOUR, turn_id=self.TURN_A)], client_turn=self.TURN_A)
        summary = relay_extract.summarize_body(json.dumps(body).encode(), "", "application/json")
        serialized = json.dumps(summary["shape"]["input"]["_passthrough"])
        for secret in (self.TURN_A, "01a17935"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(relay_extract.shape_top_level(body), summary["shape"])
        from tools.official_client_capture import derive_official_observations as derive

        self.assertEqual(derive._mitm_body_summary({"json": body})["shape"], summary["shape"])
        # 空 input、没有对象项的 input、没有 input 都不产生 _passthrough。
        for empty in ({"input": []}, {"input": ["x"]}, {"model": "m"}):
            self.assertNotIn("_passthrough", json.dumps(relay_extract.shape_top_level(empty)))


class TurnIdWireSummaryTests(unittest.TestCase):
    """relay_extract.turn_id_wire_summary（0.165.0 登记新增，W2）：回合 id、开始时间间隔与会话标识的官方位布局摘要。"""

    # 官方位布局 UUIDv7：版本 7、变体 10、rand_a（第三组去版本位的 3 位十六进制）第 2、3 位为 0；毫秒时间戳取第一组与第二组。
    OFFICIAL = "01a17935-c8dc-7cb2-8b5b-c9b98611bb03"
    EMBEDDED_MS = int("01a17935c8dc", 16)
    # google/uuid 风格的 v7：rand_a 是亚毫秒时间，第 2、3 位非零（这里 0x7ff：低两位 11）。
    GOOGLE_STYLE = "01a17935-c8dc-7fff-8b5b-c9b98611bb03"
    HASHED = "0123456789ab-cdef".replace("-", "")[:8] + "-1111-2222-3333-444455556666"
    ROOT = "11111111-1111-7111-8111-111111111111"

    def summary(self, turn_id, *, gap=2, request_kind="turn", session=None, thread=None, window=None, drop_started=False) -> dict:
        thread = thread or session or self.ROOT
        metadata = {"turn_id": turn_id, "request_kind": request_kind}
        if not drop_started and gap is not None and isinstance(turn_id, str) and turn_id[:8].isalnum() and len(turn_id) == 36:
            metadata["turn_started_at_unix_ms"] = int(turn_id.replace("-", "")[:12], 16) + gap
        client_metadata = {
            "turn_id": turn_id,
            "session_id": session or self.ROOT,
            "thread_id": thread,
            "x-codex-window-id": window if window is not None else f"{thread}:0",
            "x-codex-turn-metadata": json.dumps(metadata),
        }
        return relay_extract.turn_id_wire_summary(client_metadata)

    def test_official_layout_turn_id_with_a_short_gap_passes(self) -> None:
        for gap in (0, 1, 4, 100):
            with self.subTest(gap=gap):
                result = self.summary(self.OFFICIAL, gap=gap)
                self.assertEqual(result["client_turn_id_form"], "uuidv7")
                self.assertTrue(result["client_turn_started_gap_ok"])
        self.assertTrue(self.summary(self.OFFICIAL)["client_session_ids_ok"])

    def test_gap_outside_zero_to_one_hundred_milliseconds_fails(self) -> None:
        for gap in (-1, 101, 5000):
            with self.subTest(gap=gap):
                self.assertFalse(self.summary(self.OFFICIAL, gap=gap)["client_turn_started_gap_ok"])
        # 取不到开始时间：失败关闭。
        self.assertFalse(self.summary(self.OFFICIAL, drop_started=True)["client_turn_started_gap_ok"])

    def test_wrong_layouts_are_distinguished(self) -> None:
        self.assertEqual(self.summary(self.GOOGLE_STYLE)["client_turn_id_form"], "uuidv7_layout")
        self.assertEqual(self.summary(self.HASHED)["client_turn_id_form"], "uuid_other")
        self.assertEqual(self.summary("not-a-uuid")["client_turn_id_form"], "other")
        # 回合 id 不是官方位布局时，开始时间间隔也只对 v7 回合 id 有意义；哈希形态取不到。
        self.assertFalse(self.summary(self.HASHED)["client_turn_started_gap_ok"])

    def test_prewarm_frames_have_an_empty_turn_id_and_pass(self) -> None:
        result = self.summary("", request_kind="prewarm", drop_started=True)
        self.assertEqual((result["client_turn_id_form"], result["client_turn_started_gap_ok"]), ("prewarm", True))
        # 空 turn_id 但不是预热：形态 other，间隔不通过。
        other = self.summary("", request_kind="turn", drop_started=True)
        self.assertEqual((other["client_turn_id_form"], other["client_turn_started_gap_ok"]), ("other", False))

    def test_session_ids_are_root_thread_in_window_zero(self) -> None:
        self.assertTrue(self.summary(self.OFFICIAL)["client_session_ids_ok"])
        # 子代理：thread_id 与 session_id 不同；压缩后的新窗口 :1；会话标识不是官方位布局；窗口号不是 thread_id:0。
        self.assertFalse(self.summary(self.OFFICIAL, thread="22222222-2222-7222-8222-222222222222")["client_session_ids_ok"])
        self.assertFalse(self.summary(self.OFFICIAL, window=f"{self.ROOT}:1")["client_session_ids_ok"])
        self.assertFalse(self.summary(self.OFFICIAL, session=self.GOOGLE_STYLE, thread=self.GOOGLE_STYLE)["client_session_ids_ok"])
        self.assertFalse(self.summary(self.OFFICIAL, window="other:0")["client_session_ids_ok"])
        self.assertEqual(relay_extract.turn_id_wire_summary(None), {
            "client_turn_id_form": "other", "client_turn_started_gap_ok": False, "client_session_ids_ok": False})

    def test_summary_is_part_of_the_input_passthrough_summary_and_carries_no_identifiers(self) -> None:
        body = {
            "type": "response.create",
            "input": [{"type": "message", "role": "user", "content": []}],
            "client_metadata": {
                "turn_id": self.OFFICIAL, "session_id": self.ROOT, "thread_id": self.ROOT, "x-codex-window-id": f"{self.ROOT}:0",
                "x-codex-turn-metadata": json.dumps({"turn_id": self.OFFICIAL, "turn_started_at_unix_ms": self.EMBEDDED_MS + 3}),
            },
        }
        shape = relay_extract.shape_top_level(body)
        passthrough = shape["input"]["_passthrough"]
        self.assertEqual(
            (passthrough["client_turn_id_form"], passthrough["client_turn_started_gap_ok"], passthrough["client_session_ids_ok"]),
            ("uuidv7", True, True),
        )
        serialized = json.dumps(shape)
        for identifier in (self.OFFICIAL, self.ROOT):
            self.assertNotIn(identifier, serialized)


class WsMessageFormSummaryTests(unittest.TestCase):
    """WS 消息原文的两个形态结论（0.165.0 登记新增，W2）：不以换行结尾、没有 HTML 转义（官方 serde_json::to_string 的写法）。"""

    @staticmethod
    def frames(payload: bytes) -> list[dict]:
        import struct

        mask = b"\x11\x22\x33\x44"
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        head = bytes([0x81])
        if len(payload) <= 125:
            frame = head + bytes([0x80 | len(payload)]) + mask + masked
        else:
            frame = head + bytes([0x80 | 126]) + struct.pack(">H", len(payload)) + mask + masked
        return relay_extract.parse_ws_frames(frame)

    def message(self, text: str, *, newline: bool = False) -> dict:
        payload = json.dumps({"type": "response.create", "input": [{"type": "message", "content": text}]}, ensure_ascii=False).encode("utf-8")
        return self.frames(payload + (b"\n" if newline else b""))[0]

    def test_official_form_has_neither_trailing_newline_nor_escape(self) -> None:
        frame = self.message("提示 <tag> & 1 > 0")
        self.assertIs(frame["message_ends_with_newline"], False)
        self.assertIs(frame["message_has_html_escape"], False)

    def test_trailing_newline_is_detected(self) -> None:
        self.assertIs(self.message("x", newline=True)["message_ends_with_newline"], True)

    def test_html_escapes_are_detected_in_any_case(self) -> None:
        for escape in ("\\u003c", "\\u003e", "\\u0026", "\\u003C", "\\u2028", "\\u2029"):
            with self.subTest(escape=escape):
                payload = ('{"type":"response.create","input":"a' + escape + 'b"}').encode("utf-8")
                self.assertIs(self.frames(payload)[0]["message_has_html_escape"], True)


class TurnMetadataSummaryTests(unittest.TestCase):
    """relay_extract 给带 turn metadata 的请求体附的结构摘要 ``_turn_metadata``（0.165.0 登记新增）：
    线程来源、请求类型、顶层键名、窗口号、compaction 子键名；只记枚举与键名，不记任何标识与对话内容。"""

    @staticmethod
    def body(source, **extra) -> dict:
        metadata = {"session_id": "11111111-1111-7111-8111-111111111111", "turn_id": "22222222-2222-7222-8222-222222222222"}
        if source is not None:
            metadata["thread_source"] = source
        metadata.update(extra)
        return {"type": "response.create", "client_metadata": {"x-codex-turn-metadata": json.dumps(metadata)}}

    def test_user_title_and_other_sources(self) -> None:
        for source, expected in (("user", "user"), ("thread_title", "thread_title"), ("subagent", "other"), ("", "other")):
            with self.subTest(source=source):
                shape = relay_extract.shape_top_level(self.body(source))
                summary = shape["_turn_metadata"]
                self.assertEqual(summary["thread_source"], expected)
                # 键名按出现顺序给出；没有 request_kind／window_number／compaction 时这些字段不出现。
                self.assertEqual(summary["keys"], ["session_id", "turn_id", "thread_source"])
                for absent in ("request_kind", "window_number", "compaction_keys"):
                    self.assertNotIn(absent, summary)

    def test_request_kind_enum(self) -> None:
        for kind, expected in (("prewarm", "prewarm"), ("turn", "turn"), ("compaction", "compaction"), ("review", "other"), ("", "other")):
            with self.subTest(kind=kind):
                summary = relay_extract.shape_top_level(self.body("user", request_kind=kind))["_turn_metadata"]
                self.assertEqual(summary["request_kind"], expected)
        # 非字符串的 request_kind 不进摘要。
        self.assertNotIn("request_kind", relay_extract.shape_top_level(self.body("user", request_kind=3))["_turn_metadata"])

    def test_window_number_bounds(self) -> None:
        for window, present in ((0, True), (1, True), (1000, True), (1001, False), (-1, False), (True, False), ("1", False), (1.5, False)):
            with self.subTest(window=window):
                summary = relay_extract.shape_top_level(self.body("user", window_number=window))["_turn_metadata"]
                if present:
                    self.assertEqual(summary["window_number"], window)
                else:
                    self.assertNotIn("window_number", summary)

    def test_compaction_object_keys_only(self) -> None:
        compaction = {"trigger": "manual", "reason": "user_requested", "implementation": "responses_compaction_v2", "phase": "standalone_turn"}
        shape = relay_extract.shape_top_level(self.body("user", request_kind="compaction", compaction=compaction))
        summary = shape["_turn_metadata"]
        self.assertEqual(summary["compaction_keys"], ["trigger", "reason", "implementation", "phase"])
        self.assertIn("compaction", summary["keys"])
        # compaction 里的取值（原因、实现名）不进摘要，只留键名。
        self.assertNotIn("responses_compaction_v2", json.dumps(shape))
        # compaction 不是对象时不给 compaction_keys。
        self.assertNotIn("compaction_keys", relay_extract.shape_top_level(self.body("user", compaction="x"))["_turn_metadata"])

    def test_keys_are_bounded_and_truncated(self) -> None:
        many = {f"k{index:02d}": index for index in range(60)}
        summary = relay_extract.shape_top_level(self.body("user", **many))["_turn_metadata"]
        self.assertEqual(len(summary["keys"]), relay_extract.TURN_METADATA_KEYS_LIMIT)
        long_key = "x" * 100
        summary = relay_extract.shape_top_level(self.body("user", **{long_key: 1}))["_turn_metadata"]
        self.assertTrue(all(len(key) <= 40 for key in summary["keys"]))

    def test_summary_carries_no_identifiers_or_raw_values(self) -> None:
        serialized = json.dumps(relay_extract.shape_top_level(self.body("a-very-long-private-source-name")))
        self.assertIn('"thread_source": "other"', serialized)
        for secret in ("11111111-1111", "22222222-2222", "private-source"):
            self.assertNotIn(secret, serialized)

    def test_absent_without_a_parseable_turn_metadata_source(self) -> None:
        self.assertNotIn("_turn_metadata", relay_extract.shape_top_level({"type": "response.create"}))
        self.assertNotIn("_turn_metadata", relay_extract.shape_top_level({"client_metadata": {"x-codex-turn-metadata": "not json"}}))
        # 没有 thread_source 但有可解析的对象：仍给出 keys（压缩帧、预热帧靠键名判），只是没有 thread_source。
        summary = relay_extract.shape_top_level(self.body(None))["_turn_metadata"]
        self.assertNotIn("thread_source", summary)
        self.assertEqual(summary["keys"], ["session_id", "turn_id"])
        # 三条摘要路径（summarize_body、WS 帧、mitm 派生）共用 shape_top_level，同一份请求体给出同构摘要。
        raw = json.dumps(self.body("user")).encode("utf-8")
        self.assertEqual(
            relay_extract.summarize_body(raw, "", "application/json")["shape"]["_turn_metadata"],
            relay_extract.shape_top_level(self.body("user"))["_turn_metadata"],
        )


class MainTrackLiteExpectation0164Tests(MainTrackLiteExpectationTests):
    """0.164.0 候选期望清单（由 0.163.0 终态复制）同样满足主线 Lite 的 BODY-006 检查与 EP-001 约束。"""

    MANIFEST_SUFFIX = "0_164_0"


class MainTrackLiteExpectation0165Tests(MainTrackLiteExpectationTests):
    """0.165.0 候选期望清单（由 0.164.0 终态复制）同样满足主线 Lite 的 BODY-006 检查与 EP-001 约束。"""

    MANIFEST_SUFFIX = "0_165_0"


class Expectation0165WireChecksTests(Expectation0164WireChecksTests):
    """0.165.0 候选期望清单复用 0.164.0 差异报告落下的全部判据正反例（含 SIM-06 对齐批准画像 b2 之后的 BODY-003 口径）。

    清单由 0.164.0 终态复制：与上一版相比，把版本号换回后只有四条检查的描述文字不同（“从该版起”的行为起点表述改成不带版本号的说法，
    目标断言画像校验拒绝任何含 baseline 版本字面量的字符串），判据本身、选择器、检查 ID 一律不变。
    """

    MANIFEST_SUFFIX = "0_165_0"

    # 0.165.0 清单相对上一版的全部差异登记在这里；调研结论每落一类，就在这里加一类，审计测试逐项核对，多出或缺少都红。
    DESCRIPTION_ONLY_0165 = [
        ("SPEC-EP-002", "oauth-refresh-body-keys"),
        ("SPEC-EP-022", "image-header-order"),
        ("SPEC-WS-005", "first-business-frame-carries-prefix"),
        ("SPEC-WS-005", "warmup-input-empty"),
    ]
    # 规则新增的检查（追加在原检查之后，按此顺序）。
    ADDED_CHECKS_0165 = {
        "SPEC-BODY-003": ["main-client-turn-id-form", "main-turn-started-gap", "main-session-ids-official-form"],
        "SPEC-WS-004": ["text-message-single-frame", "text-frame-fin", "no-continuation-frames", "all-text-frames-compressed"],
        "SPEC-BODY-006": [
            "numeric-effort-http-no-configuration-update",
            "numeric-effort-ws-no-configuration-update",
            "configuration-update-http-first-turn",
            "configuration-update-ws-absent-without-effort-change",
        ],
        "SPEC-WS-005": [
            "replay-frame-no-previous-response-id",
            "replay-frame-no-generate",
            "replay-frame-carries-full-history",
            "replay-no-second-warmup",
            "replay-same-connection",
            "first-business-frame-client-turn-id-form",
            "first-business-frame-turn-started-gap",
            "first-business-frame-session-ids-official-form",
            "message-no-trailing-newline",
            "message-no-html-escape",
            "window-stays-zero-without-compaction",
            "compaction-frame-in-old-window",
            "window-advances-after-compaction",
            "window-stays-advanced-after-compaction",
        ],
        "SPEC-BODY-004": ["turn-state-new-turn-reset"],
        "SPEC-BODY-008": [
            "ws-turn-frame-root-turn-id",
            "ws-prewarm-frame-no-root-turn-id",
            "ws-frame-request-start-ms",
            "ws-frame-responses-lite-flag",
            "ws-compaction-frame-no-access-programs",
            "http-turn-request-root-turn-id",  # HTTP 回合请求 client_metadata 带 root_turn_id（W3 第三轮）
        ],
        "SPEC-EP-021": [
            "ws-compaction-frame-request-kind",
            "ws-compaction-frame-no-model-key",
            "ws-compaction-frame-no-reasoning-effort-key",
            "ws-compaction-frame-no-turn-trigger-key",
            "ws-compaction-frame-compaction-object",
            # HTTP 压缩请求的 turn metadata 与请求体（W3 第五轮，画像 TurnMetadata.HTTPCompactionRequests）
            "http-compaction-request-kind",
            "http-compaction-request-no-model-key",
            "http-compaction-request-no-reasoning-effort-key",
            "http-compaction-request-no-turn-trigger-key",
            "http-compaction-request-compaction-object",
            "http-compaction-request-no-access-programs",
        ],
        "SPEC-BODY-009": [
            "ws-turn-frame-root-turn-id-key",
            "ws-turn-frame-agent-name-key",
            "ws-prewarm-frame-no-root-turn-id-key",
            # HTTP 回合请求 turn metadata 的根线程键与窗口键（W3 第四轮）
            "http-turn-request-agent-name-key",
            "http-turn-request-root-turn-id-key",
            "http-turn-request-window-number-key",
            "http-turn-request-context-window-id-key",
        ],
    }
    # 规则的 scenario_ids 变化（旧值必须是新值的前缀）。
    SCENARIO_IDS_0165 = {
        "SPEC-BODY-006": ["A03", "A04", "A05", "A06"],  # W1 的 WS 判据选 A05、A06，判据场景必须是规则场景的子集
        "SPEC-WS-004": ["A05", "A06"],  # W2 的一消息一帧判据选 A05、A06
        "SPEC-WS-005": ["A05", "A06", "A20", "A21", "A22"],
        "SPEC-BODY-008": ["A03", "A04", "A05", "A06", "A20", "A21", "A22"],  # W3 第二批：A20～A22 的回合帧动态键
        "SPEC-EP-021": ["A03", "A05", "A06", "A10", "A21"],  # 压缩请求帧的 turn metadata 判据只有 A21 有压缩
        # 根线程键判据选 A05、A06 与重放三场景的 WS 帧；HTTP turn metadata 四键判据选 A03、A04，判据场景必须是规则场景的子集，所以追加 A03。
        "SPEC-BODY-009": ["A04", "A05", "A06", "A20", "A21", "A22", "A03"],
    }
    ADDED_SCENARIOS_0165 = ["A20", "A21", "A22"]

    def test_other_rules_are_untouched_by_the_increment(self) -> None:
        raw = (TOOL_ROOT / "candidate_rule_expectations_0_165_0.json").read_text(encoding="utf-8")
        # 目标断言画像里不得出现 baseline 版本字面量。
        self.assertNotIn("0.164.0", raw)
        self.assertNotIn(r"0\\.164\\.0", raw)
        reverted = json.loads(raw.replace("0.165.0", "0.164.0").replace(r"0\\.165\\.0", r"0\\.164\\.0"))
        old = json.loads((TOOL_ROOT / "candidate_rule_expectations_0_164_0.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {k: v for k, v in reverted.items() if k not in {"rules", "scenarios"}},
            {k: v for k, v in old.items() if k not in {"rules", "scenarios"}},
        )
        # 场景列表：上一版的场景逐字不变，另加登记的新场景。
        self.assertEqual([s for s in reverted["scenarios"] if s["scenario_id"] in {o["scenario_id"] for o in old["scenarios"]}], old["scenarios"])
        self.assertEqual(
            [s["scenario_id"] for s in reverted["scenarios"] if s["scenario_id"] not in {o["scenario_id"] for o in old["scenarios"]}],
            self.ADDED_SCENARIOS_0165,
        )
        self.assertEqual([r["rule_id"] for r in reverted["rules"]], [r["rule_id"] for r in old["rules"]])
        description_only = []
        for new_rule, old_rule in zip(reverted["rules"], old["rules"]):
            rule_id = new_rule["rule_id"]
            changed_keys = {k for k in set(new_rule) | set(old_rule) if k != "checks" and new_rule.get(k) != old_rule.get(k)}
            if rule_id in self.SCENARIO_IDS_0165:
                self.assertEqual(changed_keys, {"scenario_ids"}, rule_id)
                self.assertEqual(new_rule["scenario_ids"], self.SCENARIO_IDS_0165[rule_id])
                self.assertEqual(new_rule["scenario_ids"][: len(old_rule["scenario_ids"])], old_rule["scenario_ids"])
            else:
                self.assertEqual(changed_keys, set(), rule_id)
            old_ids = [c["id"] for c in old_rule["checks"]]
            added = self.ADDED_CHECKS_0165.get(rule_id, [])
            self.assertEqual([c["id"] for c in new_rule["checks"]], old_ids + added, rule_id)
            for new_check, old_check in zip(new_rule["checks"], old_rule["checks"]):
                if new_check != old_check:
                    keys = sorted(k for k in set(new_check) | set(old_check) if new_check.get(k) != old_check.get(k))
                    self.assertEqual(keys, ["description"], (rule_id, new_check["id"]))
                    description_only.append((rule_id, new_check["id"]))
        self.assertEqual(sorted(description_only), sorted(self.DESCRIPTION_ONLY_0165))
        # SIM-06 在两份清单里都已对齐批准画像 b2：BODY-003 main-messages-keep-passthrough 期望 1。
        for document in (old, reverted):
            body003 = next(rule for rule in document["rules"] if rule["rule_id"] == "SPEC-BODY-003")
            check = next(item for item in body003["checks"] if item["id"] == "main-messages-keep-passthrough")
            self.assertEqual(check["assertion"]["value"], 1)

    # ---- W1：configuration_update（SPEC-BODY-006 的 4 条判据；provider 条件用标签 ws_prewarm 表达） ----

    @staticmethod
    def http_observation_with(types, labels, *, method="POST", path="/backend-api/codex/responses", scenario="A04"):
        return candidate_rule_assertion.Observation(
            record_id=f"{scenario}-http",
            scenario_id=scenario,
            record_type="http_request",
            artifact_path="relay/conn001.client_to_upstream.bin",
            evidence_paths=("relay/conn001.client_to_upstream.bin",),
            labels=labels,
            data={"method": method, "path": path, "wire_protocol": "http",
                  "body": {"shape": {"input": {"_types": types}}}},
        )

    @staticmethod
    def ws_observation_with(types, labels, *, scenario="A06", event="response.create"):
        return candidate_rule_assertion.Observation(
            record_id=f"{scenario}-frame",
            scenario_id=scenario,
            record_type="websocket_frame",
            artifact_path="relay/conn001.client_to_upstream.bin",
            evidence_paths=("relay/conn001.client_to_upstream.bin",),
            labels=labels,
            data={"event_type": event, "shape": {"input": {"_types": types}}},
        )

    def run_body006(self, check_id, observations) -> bool:
        rule, check = self.check("SPEC-BODY-006", check_id)
        matched = candidate_rule_assertion._select_observations(observations, check["select"], rule["scenario_ids"])
        return candidate_rule_assertion._evaluate_assertion(matched, check["assertion"])[0]

    HTTP_PROBE_LABELS = {"transport": "http", "mode": "lite", "track": "main", "ws_prewarm": "none"}

    def test_http_first_turn_requires_configuration_update_only_for_the_no_ws_prewarm_provider(self) -> None:
        check = "configuration-update-http-first-turn"
        with_update = ["additional_tools", "message", "message", "configuration_update"]
        without_update = ["additional_tools", "message", "message"]
        self.assertTrue(self.run_body006(check, [self.http_observation_with(with_update, self.HTTP_PROBE_LABELS)]))
        # 官方无预热 provider 首轮没带（或网关没合成）：失败。
        self.assertFalse(self.run_body006(check, [self.http_observation_with(without_update, self.HTTP_PROBE_LABELS)]))
        # 有 WS 预热的 HTTP 样本（内置 provider 先试 WS，首轮不带；residency／runtime-metrics 一类）没标 ws_prewarm，不被选中，也不能让空选择放行。
        default_provider = {"transport": "http", "mode": "lite", "variant": "default"}
        self.assertFalse(self.run_body006(check, [self.http_observation_with(without_update, default_provider)]))
        self.assertTrue(self.run_body006(check, [
            self.http_observation_with(with_update, self.HTTP_PROBE_LABELS),
            self.http_observation_with(without_update, default_provider),
        ]))
        # 数字档位样本（reasoning_effort_config=numeric）不产生 update，不进这条判据。
        numeric = {**self.HTTP_PROBE_LABELS, "reasoning_effort_config": "numeric"}
        self.assertTrue(self.run_body006(check, [
            self.http_observation_with(with_update, self.HTTP_PROBE_LABELS),
            self.http_observation_with(without_update, numeric),
        ]))

    def test_numeric_effort_never_produces_configuration_update(self) -> None:
        numeric = {"transport": "http", "reasoning_effort_config": "numeric"}
        types = ["additional_tools", "message", "message"]
        self.assertTrue(self.run_body006("numeric-effort-http-no-configuration-update", [self.http_observation_with(types, numeric)]))
        self.assertFalse(self.run_body006(
            "numeric-effort-http-no-configuration-update",
            [self.http_observation_with([*types, "configuration_update"], numeric)]))
        ws_numeric = {"transport": "websocket", "reasoning_effort_config": "numeric"}
        self.assertTrue(self.run_body006("numeric-effort-ws-no-configuration-update", [self.ws_observation_with(types, ws_numeric, scenario="A05")]))
        self.assertFalse(self.run_body006(
            "numeric-effort-ws-no-configuration-update",
            [self.ws_observation_with([*types, "configuration_update"], ws_numeric, scenario="A05")]))
        # 失败关闭：没有数字档位样本时不得空过。
        self.assertFalse(self.run_body006("numeric-effort-http-no-configuration-update", [self.http_observation_with(types, {"transport": "http"})]))

    def test_ws_frames_without_an_effort_change_never_carry_configuration_update(self) -> None:
        check = "configuration-update-ws-absent-without-effort-change"
        clean = [self.ws_observation_with(["additional_tools", "message"], {}), self.ws_observation_with([], {}, scenario="A05")]
        self.assertTrue(self.run_body006(check, clean))
        self.assertFalse(self.run_body006(check, [*clean, self.ws_observation_with(["message", "configuration_update"], {})]))
        self.assertFalse(self.run_body006(check, []))

    def test_ws_prewarm_label_marks_exactly_the_no_prewarm_http_samples(self) -> None:
        """ws_prewarm=none 只标官方经 openai-http-probe provider 的 A04 HTTP 作业与候选 A04 的非数字档位 HTTP 入站。"""

        document = json.loads((TOOL_ROOT / "codex_upgrade_evidence_labels_0_165_0.json").read_text(encoding="utf-8"))
        marked = sorted(
            (entry["side"], entry["job_id"], tuple(rule["scenario_ids"]))
            for entry in document["entries"]
            for rule in entry["rules"]
            if rule["labels"].get("ws_prewarm") == "none"
        )
        self.assertEqual(
            sorted({(side, job) for side, job, _ in marked}),
            [("candidate", "candidate-frozen-core"), ("official", "official-relay-http-response"), ("official", "official-relay-http-response-plain")],
        )
        self.assertEqual({scenarios for _, _, scenarios in marked}, {("A04",)})
        # 候选 A04 里只有真正经网关 HTTP 出站合成 update 的 POST 才标：baseline（conn002）、residency-us（conn004）、parent-thread（conn007）；
        # 启动期 models GET（conn001／3／5）与记忆合并 memgen（conn006，官方与网关都不为它启用 effort 覆盖）不标。
        candidate_globs = sorted(
            rule["glob"].rsplit("/", 1)[-1]
            for entry in document["entries"] if entry["job_id"] == "candidate-frozen-core"
            for rule in entry["rules"] if rule["labels"].get("ws_prewarm") == "none"
        )
        self.assertEqual(candidate_globs, ["conn002.client_to_upstream.bin", "conn004.client_to_upstream.bin", "conn007.client_to_upstream.bin"])
        for entry in document["entries"]:
            for rule in entry["rules"]:
                if rule["labels"].get("ws_prewarm") == "none":
                    self.assertEqual(rule["labels"].get("transport"), "http")
                    self.assertNotIn("reasoning_effort_config", rule["labels"])

    # ---- W2：一消息一帧、回合 id／会话标识的官方位布局、消息形态（SPEC-WS-004／BODY-003／WS-005） ----

    @staticmethod
    def text_frame(scenario="A06", **data):
        return candidate_rule_assertion.Observation(
            record_id=f"{scenario}-text",
            scenario_id=scenario,
            record_type="websocket_frame",
            artifact_path="relay/conn001.client_to_upstream.bin",
            evidence_paths=("relay/conn001.client_to_upstream.bin",),
            labels={},
            data={"opcode": "TEXT", "event_type": "response.create", "fin": True, "rsv1_deflate": True, "message_fragment_count": 1, **data},
        )

    def test_one_message_one_frame_checks_accept_official_frames_and_reject_fragmentation(self) -> None:
        ids = self.ADDED_CHECKS_0165["SPEC-WS-004"]
        official = [self.text_frame("A05"), self.text_frame("A06")]
        for check_id in ids[:2] + ids[3:]:
            self.assertTrue(self.run_check("SPEC-WS-004", check_id, official), check_id)
        # 没有续帧：count_equal 0，官方形态下选择为空也成立（续帧本来就不存在）。
        self.assertTrue(self.run_check("SPEC-WS-004", "no-continuation-frames", official))
        # 候选旧写法：一条消息拆成 7 个分片帧（首帧 fin=false、消息分片数 7），其余是 CONT 续帧；小消息还没压缩。
        fragmented = [self.text_frame(fin=False, message_fragment_count=7)] + [
            self.text_frame(opcode="CONT", event_type=None, fin=index == 5, rsv1_deflate=False) for index in range(6)
        ]
        self.assertFalse(self.run_check("SPEC-WS-004", "text-message-single-frame", fragmented))
        self.assertFalse(self.run_check("SPEC-WS-004", "text-frame-fin", fragmented))
        self.assertFalse(self.run_check("SPEC-WS-004", "no-continuation-frames", fragmented))
        self.assertFalse(self.run_check("SPEC-WS-004", "all-text-frames-compressed", [self.text_frame(rsv1_deflate=False)]))
        # 失败关闭：选不到任何 TEXT 帧时不得空过。
        self.assertFalse(self.run_check("SPEC-WS-004", "text-message-single-frame", []))

    def test_turn_id_session_and_gap_checks_read_the_summary_fields(self) -> None:
        fields = {
            "main-client-turn-id-form": ("client_turn_id_form", "uuidv7", "uuidv7_layout"),
            "main-turn-started-gap": ("client_turn_started_gap_ok", True, False),
            "main-session-ids-official-form": ("client_session_ids_ok", True, False),
        }
        for check_id, (field, good, bad) in fields.items():
            with self.subTest(check_id=check_id):
                def observation(value):
                    return candidate_rule_assertion.Observation(
                        record_id="A04-http", scenario_id="A04", record_type="http_request",
                        artifact_path="relay/conn002.client_to_upstream.bin", evidence_paths=("relay/conn002.client_to_upstream.bin",),
                        labels={"mode": "lite"},
                        data={"method": "POST", "path": "/backend-api/codex/responses",
                              "body": {"shape": {"input": {"_passthrough": {field: value}}}}},
                    )
                self.assertTrue(self.run_check("SPEC-BODY-003", check_id, [observation(good)]))
                self.assertFalse(self.run_check("SPEC-BODY-003", check_id, [observation(bad)]))
                self.assertFalse(self.run_check("SPEC-BODY-003", check_id, []))
        for check_id, (field, good, bad) in {
            "first-business-frame-client-turn-id-form": ("client_turn_id_form", "uuidv7", "uuid_other"),
            "first-business-frame-turn-started-gap": ("client_turn_started_gap_ok", True, False),
            "first-business-frame-session-ids-official-form": ("client_session_ids_ok", True, False),
        }.items():
            with self.subTest(check_id=check_id):
                def frame(value):
                    item = self.text_frame(shape={"input": {"_passthrough": {field: value}}})
                    return dataclasses.replace(item, labels={"frame_role": "first_business"})
                self.assertTrue(self.run_check("SPEC-WS-005", check_id, [frame(good)]))
                self.assertFalse(self.run_check("SPEC-WS-005", check_id, [frame(bad)]))

    def test_message_form_checks_reject_trailing_newline_and_escapes(self) -> None:
        clean = [self.text_frame("A05", message_ends_with_newline=False, message_has_html_escape=False),
                 self.text_frame("A06", message_ends_with_newline=False, message_has_html_escape=False)]
        self.assertTrue(self.run_check("SPEC-WS-005", "message-no-trailing-newline", clean))
        self.assertTrue(self.run_check("SPEC-WS-005", "message-no-html-escape", clean))
        newline = [*clean, self.text_frame(message_ends_with_newline=True, message_has_html_escape=False)]
        escaped = [*clean, self.text_frame(message_ends_with_newline=False, message_has_html_escape=True)]
        self.assertFalse(self.run_check("SPEC-WS-005", "message-no-trailing-newline", newline))
        self.assertFalse(self.run_check("SPEC-WS-005", "message-no-html-escape", escaped))
        # 字段缺失（旧摘要器产出的观测）不得放行。
        self.assertFalse(self.run_check("SPEC-WS-005", "message-no-trailing-newline", [self.text_frame()]))

    # ---- W3：WS 整段重放不重连（SPEC-WS-005，A20～A22；线程角色与重放帧标签由引擎派生，这里手造带标签的观测跑正反例） ----

    @staticmethod
    def replay_observation(scenario, role, shape, *, connection=1, index=0, thread="main"):
        return candidate_rule_assertion.Observation(
            record_id=f"{scenario}-{role}-{index}",
            scenario_id=scenario,
            record_type="websocket_frame",
            artifact_path=f"relay/conn00{connection}.client_to_upstream.bin",
            evidence_paths=(f"relay/conn00{connection}.client_to_upstream.bin",),
            labels={"frame_role": role, "thread_role": thread},
            data={"event_type": "response.create", "connection_id": connection, "frame_index": index, "shape": shape},
        )

    @staticmethod
    def replay_handshake(scenario, index=0, thread="main"):
        return candidate_rule_assertion.Observation(
            record_id=f"{scenario}-hs-{index}",
            scenario_id=scenario,
            record_type="http_request",
            artifact_path=f"relay/conn00{index + 1}.client_to_upstream.bin",
            evidence_paths=(f"relay/conn00{index + 1}.client_to_upstream.bin",),
            labels={"thread_role": thread},
            data={"wire_protocol": "websocket", "method": "GET"},
        )

    GOOD_REPLAY = {"type": "str:response.create", "input": {"_first": {"type": "str:additional_tools"}}}
    WARMUP = {"type": "str:response.create", "generate": "bool:false", "input": "array:<len=0>"}
    REPLAY_SCENARIOS = ("A20", "A21", "A22")

    def good_replay_observations(self):
        observations = []
        for scenario in self.REPLAY_SCENARIOS:
            observations += [
                self.replay_observation(scenario, "warmup", self.WARMUP, index=0),
                self.replay_observation(scenario, "replay", self.GOOD_REPLAY, index=3),
                self.replay_handshake(scenario),
                # 标题线程：独立连接、独立预热、预热后的业务帧；主线程判据不得把它算进去。
                self.replay_observation(scenario, "warmup", self.WARMUP, connection=2, index=0, thread="title"),
                self.replay_observation(
                    scenario, "replay", {"type": "str:response.create", "previous_response_id": "str:x"},
                    connection=2, index=1, thread="title"),
                self.replay_handshake(scenario, 1, thread="title"),
            ]
        return observations

    def test_replay_checks_are_registered_for_the_three_replay_scenarios(self) -> None:
        rule = next(item for item in self.profile["rules"] if item["rule_id"] == "SPEC-WS-005")
        self.assertEqual(rule["scenario_ids"], ["A05", "A06", "A20", "A21", "A22"])
        ids = [check["id"] for check in rule["checks"]]
        added = self.ADDED_CHECKS_0165["SPEC-WS-005"]
        self.assertEqual(ids[len(ids) - len(added):], added)
        self.assertEqual(added[:5], ["replay-frame-no-previous-response-id", "replay-frame-no-generate", "replay-frame-carries-full-history",
                                     "replay-no-second-warmup", "replay-same-connection"])
        profile_scenarios = {item["scenario_id"]: item for item in self.profile["scenarios"]}
        for scenario_id in self.REPLAY_SCENARIOS:
            self.assertEqual(profile_scenarios[scenario_id]["required_artifact_kinds"], ["relay_binary", "websocket_trace"])
        payload = acceptance_contract.build_contract_payload(self.profile)
        self.assertEqual(payload["validation_modes"]["SPEC-WS-005"], "candidate_profile")
        self.assertEqual(payload["rule_counts"], {"dual_wire": 25, "candidate_profile": 18})

    def test_replay_checks_pass_on_official_shaped_replay_and_ignore_the_title_thread(self) -> None:
        observations = self.good_replay_observations()
        for check_id in self.ADDED_CHECKS_0165["SPEC-WS-005"][:5]:
            with self.subTest(check_id=check_id):
                rule, check = self.check("SPEC-WS-005", check_id)
                matched = candidate_rule_assertion._select_observations(observations, check["select"], rule["scenario_ids"])
                passed, _ = candidate_rule_assertion._evaluate_assertion(matched, check["assertion"])
                self.assertTrue(passed)

    def run_replay_check(self, check_id, observations) -> bool:
        rule, check = self.check("SPEC-WS-005", check_id)
        matched = candidate_rule_assertion._select_observations(observations, check["select"], rule["scenario_ids"])
        return candidate_rule_assertion._evaluate_assertion(matched, check["assertion"])[0]

    def test_replay_checks_reject_the_old_gateway_behaviour_and_fail_closed(self) -> None:
        scenarios = self.REPLAY_SCENARIOS
        # 旧做法：重放帧被网关链了预热响应（带 previous_response_id）。
        chained = {**self.GOOD_REPLAY, "previous_response_id": "str:<present>"}
        bad = [self.replay_observation(s, "replay", chained, index=3) for s in scenarios]
        self.assertFalse(self.run_replay_check("replay-frame-no-previous-response-id", bad))
        # 重放帧带 generate。
        bad = [self.replay_observation(s, "replay", {**self.GOOD_REPLAY, "generate": "bool:false"}, index=3) for s in scenarios]
        self.assertFalse(self.run_replay_check("replay-frame-no-generate", bad))
        # 重放帧缺 Lite 前缀。
        bad = [self.replay_observation(s, "replay", {"type": "str:response.create", "input": {"_first": {"type": "str:message"}}}, index=3) for s in scenarios]
        self.assertFalse(self.run_replay_check("replay-frame-carries-full-history", bad))
        # 网关补了第二个预热（每个场景两个 generate=false 主线程帧）。
        bad = []
        for s in scenarios:
            bad += [self.replay_observation(s, "warmup", self.WARMUP, index=0), self.replay_observation(s, "warmup", self.WARMUP, index=4)]
        self.assertFalse(self.run_replay_check("replay-no-second-warmup", bad))
        # 重放新开了连接（每个场景两次主线程握手）。
        bad = []
        for s in scenarios:
            bad += [self.replay_handshake(s, 0), self.replay_handshake(s, 1)]
        self.assertFalse(self.run_replay_check("replay-same-connection", bad))
        # 失败关闭：没有任何重放帧被选中（标签缺失、别的场景、只有标题线程）时不得空过。
        for observations in (
            [self.replay_observation("A18", "first_business", self.GOOD_REPLAY)],
            [self.replay_observation("A06", "replay", self.GOOD_REPLAY)],
            [self.replay_observation("A20", "replay", self.GOOD_REPLAY, thread="title")],
        ):
            self.assertFalse(self.run_replay_check("replay-frame-no-previous-response-id", observations))


    # ---- W3 第二批：窗口推进、压缩请求帧的 turn metadata、根线程键、client_metadata 动态键、turn-state 新回合重置 ----
    # 这一组用“官方形态的会话”端到端跑：请求体 → relay_extract.shape_top_level（摘要）→ 引擎按连接派生线程角色与重放帧标签
    # → 清单里的检查。形态取自官方 0.165 客户端的真实采样（压缩续聊：预热、两轮、压缩帧、压缩后的整段重放帧与两轮，
    # 标题线程各占一条连接）；负例是在同一会话上改一处，核对判据确实咬得住。

    SESSION = "11111111-1111-7111-8111-111111111111"

    def official_body(self, kind, *, window=0, first="message", model="gpt-6.1-sol", effort="low", thread="user",
                      previous=True, lite=True, root_in_client_metadata=True, mutate=None):
        """一个官方形态的 WS response.create 请求体（kind：prewarm／turn／compaction）。"""

        turn_id = "22222222-2222-7222-8222-222222222222"
        metadata = {
            "installation_id": "i", "session_id": self.SESSION, "thread_id": self.SESSION, "agent_name": "/root",
            "turn_id": "" if kind == "prewarm" else turn_id, "window_id": f"{self.SESSION}:{window}", "window_number": window,
            "context_window_id": "33333333-3333-7333-8333-333333333333", "request_kind": kind, "history_initialization": "new",
        }
        if kind != "prewarm":
            metadata["root_turn_id"] = turn_id
        metadata["thread_source"] = thread
        if kind == "turn":
            metadata["turn_trigger"] = "user"
        metadata.update({"sandbox": "none", "turn_started_at_unix_ms": 1})
        if kind != "compaction":
            metadata.update({"model": model, "reasoning_effort": effort})
        else:
            metadata["compaction"] = {"trigger": "manual", "reason": "user_requested", "implementation": "responses_compaction_v2",
                                      "phase": "standalone_turn", "strategy": "memento"}
        client_metadata = {"x-codex-turn-metadata": json.dumps(metadata), "x-codex-ws-stream-request-start-ms": "1793208577802"}
        if kind != "prewarm" and root_in_client_metadata:
            client_metadata["root_turn_id"] = turn_id
        if lite:
            client_metadata["ws_request_header_x_openai_internal_codex_responses_lite"] = "true"
        body = {"type": "response.create", "model": model, "client_metadata": client_metadata}
        if kind == "prewarm":
            body.update({"generate": False, "input": []})
        elif kind == "compaction":
            body.update({"previous_response_id": "resp_1", "input": [{"type": "compaction_trigger"}]})
        else:
            body["input"] = [{"type": first}, {"type": "message", "role": "user", "content": []}]
            if previous:
                body["previous_response_id"] = "resp_1"
        if mutate is not None:
            mutate(body, metadata)
            body["client_metadata"]["x-codex-turn-metadata"] = json.dumps(metadata)
        return body

    def session_observations(self, scenario, bodies_by_connection):
        """把各连接的请求体序列做成观测：摘要、引擎派生的线程角色与重放帧标签、握手记录。"""

        observations = []
        for connection, bodies in bodies_by_connection.items():
            frames = [
                {"frame_index": index, "opcode": "TEXT", "event_type": body["type"],
                 "shape": relay_extract.shape_top_level(body)}
                for index, body in enumerate(bodies)
            ]
            role, replay = candidate_rule_assertion.derive_connection_roles(frames)
            for frame in frames:
                labels = {"thread_role": role} if role else {}
                if frame["frame_index"] in replay:
                    labels["frame_role"] = "replay"
                observations.append(candidate_rule_assertion.Observation(
                    record_id=f"{scenario}-c{connection}-f{frame['frame_index']}", scenario_id=scenario, record_type="websocket_frame",
                    artifact_path=f"relay/conn00{connection}.client_to_upstream.bin",
                    evidence_paths=(f"relay/conn00{connection}.client_to_upstream.bin",), labels=labels,
                    data={"event_type": frame["event_type"], "opcode": "TEXT", "frame_index": frame["frame_index"], "shape": frame["shape"]},
                ))
        return observations

    def compact_session(self, **overrides):
        """官方压缩续聊：主线程 预热、两轮、压缩帧、压缩后的整段重放帧与一轮；另有一条标题线程连接。"""

        build = self.official_body
        main = [
            build("prewarm", previous=False),
            build("turn", first="additional_tools"),
            build("turn"),
            build("compaction"),
            build("turn", first="additional_tools", window=1, previous=False),
            build("turn", window=1),
        ]
        title = [build("prewarm", model="gpt-5.6-luna", thread="thread_title", previous=False),
                 build("turn", first="additional_tools", model="gpt-5.6-luna", thread="thread_title", previous=False)]
        if "main" in overrides:
            main = overrides["main"](main)
        return self.session_observations("A21", {1: main, 2: title})

    W3P2_BODY_008 = ("ws-turn-frame-root-turn-id", "ws-prewarm-frame-no-root-turn-id", "ws-frame-request-start-ms",
                     "ws-frame-responses-lite-flag", "ws-compaction-frame-no-access-programs")
    W3P2_EP_021 = ("ws-compaction-frame-request-kind", "ws-compaction-frame-no-model-key", "ws-compaction-frame-no-reasoning-effort-key",
                   "ws-compaction-frame-no-turn-trigger-key", "ws-compaction-frame-compaction-object")
    W3P2_BODY_009 = ("ws-turn-frame-root-turn-id-key", "ws-turn-frame-agent-name-key", "ws-prewarm-frame-no-root-turn-id-key")
    W3P2_WS_005_A21 = ("compaction-frame-in-old-window", "window-advances-after-compaction", "window-stays-advanced-after-compaction")

    def all_new_checks_hold(self, observations, *, rules=None) -> dict[str, bool]:
        groups = {"SPEC-BODY-008": self.W3P2_BODY_008, "SPEC-EP-021": self.W3P2_EP_021, "SPEC-BODY-009": self.W3P2_BODY_009,
                  "SPEC-WS-005": self.W3P2_WS_005_A21}
        return {f"{rule}/{check}": self.run_check(rule, check, observations)
                for rule, checks in groups.items() if rules is None or rule in rules for check in checks}

    def test_official_shaped_compaction_session_satisfies_every_new_check(self) -> None:
        results = self.all_new_checks_hold(self.compact_session())
        self.assertEqual([name for name, ok in results.items() if not ok], [])
        # 派生标签确实把压缩后的重放帧认了出来（前一业务帧是压缩请求），标题线程没有被算成主线程。
        observations = self.compact_session()
        replay = [item for item in observations if item.labels.get("frame_role") == "replay"]
        self.assertEqual([(item.artifact_path, item.data["frame_index"]) for item in replay], [("relay/conn001.client_to_upstream.bin", 4)])
        self.assertEqual({item.labels["thread_role"] for item in observations}, {"main", "title"})

    def test_window_checks_reject_a_gateway_that_never_advances_the_window(self) -> None:
        # 旧行为：窗口号固定为 0，压缩之后也不推进。
        observations = self.compact_session(main=lambda frames: [self.rebuild_with_window(body, 0) for body in frames])
        self.assertFalse(self.run_check("SPEC-WS-005", "window-advances-after-compaction", observations))
        self.assertFalse(self.run_check("SPEC-WS-005", "window-stays-advanced-after-compaction", observations))
        # 压缩帧本身仍在旧窗口这一条不受影响。
        self.assertTrue(self.run_check("SPEC-WS-005", "compaction-frame-in-old-window", observations))

    def rebuild_with_window(self, body, window):
        metadata = json.loads(body["client_metadata"]["x-codex-turn-metadata"])
        metadata["window_number"] = window
        metadata["window_id"] = f"{self.SESSION}:{window}"
        clone = json.loads(json.dumps(body))
        clone["client_metadata"]["x-codex-turn-metadata"] = json.dumps(metadata)
        return clone

    def test_window_checks_reject_an_early_advance_and_an_unadvanced_replay(self) -> None:
        # 压缩帧自己就写成新窗口：旧窗口判据失败。
        early = self.compact_session(main=lambda frames: [*frames[:3], self.rebuild_with_window(frames[3], 1), *frames[4:]])
        self.assertFalse(self.run_check("SPEC-WS-005", "compaction-frame-in-old-window", early))
        # 重放帧停在旧窗口。
        stale = self.compact_session(main=lambda frames: [*frames[:4], self.rebuild_with_window(frames[4], 0), frames[5]])
        self.assertFalse(self.run_check("SPEC-WS-005", "window-advances-after-compaction", stale))
        # 其后的回合退回旧窗口：新窗口保持判据失败（只剩重放帧一个窗口号 1 的帧）。
        regressed = self.compact_session(main=lambda frames: [*frames[:5], self.rebuild_with_window(frames[5], 0)])
        self.assertFalse(self.run_check("SPEC-WS-005", "window-stays-advanced-after-compaction", regressed))

    def test_window_stays_zero_without_compaction_in_the_other_replay_scenarios(self) -> None:
        build = self.official_body
        model_switch = [build("prewarm", previous=False), build("turn", first="additional_tools"),
                        build("turn", first="additional_tools", model="gpt-6-astra", previous=False)]
        good = self.session_observations("A20", {1: model_switch})
        self.assertTrue(self.run_check("SPEC-WS-005", "window-stays-zero-without-compaction", good))
        # 换模型的整段重放被网关误当压缩推进了窗口。
        advanced = self.session_observations("A20", {1: [*model_switch[:2], self.rebuild_with_window(model_switch[2], 1)]})
        self.assertFalse(self.run_check("SPEC-WS-005", "window-stays-zero-without-compaction", advanced))
        # 标题线程的窗口号不归这条判据管；失败关闭：没有 A20、A22 主线帧时不得空过。
        self.assertFalse(self.run_check("SPEC-WS-005", "window-stays-zero-without-compaction", self.compact_session()))

    def test_compaction_frame_metadata_checks_reject_the_ordinary_turn_form(self) -> None:
        # 旧行为：压缩帧按普通业务帧处理——request_kind=turn，带 model、reasoning_effort、turn_trigger，没有 compaction 对象。
        def ordinary(frames):
            def become_turn(body, metadata):
                metadata["request_kind"] = "turn"
                metadata.pop("compaction")
                metadata.update({"model": "gpt-6.1-sol", "reasoning_effort": "low", "turn_trigger": "user"})
            return [*frames[:3], self.official_body("compaction", mutate=become_turn), *frames[4:]]
        observations = self.compact_session(main=ordinary)
        for check in ("ws-compaction-frame-request-kind", "ws-compaction-frame-no-model-key", "ws-compaction-frame-no-reasoning-effort-key",
                      "ws-compaction-frame-no-turn-trigger-key", "ws-compaction-frame-compaction-object"):
            with self.subTest(check=check):
                self.assertFalse(self.run_check("SPEC-EP-021", check, observations))
        # compaction 子键顺序不对（取值不入摘要，键序是线上字节）。
        def reordered(body, metadata):
            metadata["compaction"] = dict(reversed(list(metadata["compaction"].items())))
        shuffled = self.compact_session(main=lambda frames: [*frames[:3], self.official_body("compaction", mutate=reordered), *frames[4:]])
        self.assertFalse(self.run_check("SPEC-EP-021", "ws-compaction-frame-compaction-object", shuffled))
        # 失败关闭：没有压缩帧（A20 之类的会话）时不得空过。
        self.assertFalse(self.run_check("SPEC-EP-021", "ws-compaction-frame-request-kind", self.session_observations("A21", {1: [self.official_body("prewarm", previous=False)]})))

    def test_dynamic_key_checks_reject_missing_root_turn_id_timestamp_and_lite_flag(self) -> None:
        def drop(key):
            def mutate(frames):
                clones = json.loads(json.dumps(frames))
                for body in clones[1:]:
                    body["client_metadata"].pop(key, None)
                return clones
            return mutate
        cases = {
            "root_turn_id": "ws-turn-frame-root-turn-id",
            "x-codex-ws-stream-request-start-ms": "ws-frame-request-start-ms",
            "ws_request_header_x_openai_internal_codex_responses_lite": "ws-frame-responses-lite-flag",
        }
        for key, check in cases.items():
            with self.subTest(key=key):
                self.assertFalse(self.run_check("SPEC-BODY-008", check, self.compact_session(main=drop(key))))
        # 预热帧不该带 root_turn_id。
        def prewarm_with_root(frames):
            clones = json.loads(json.dumps(frames))
            clones[0]["client_metadata"]["root_turn_id"] = "22222222-2222-7222-8222-222222222222"
            return clones
        self.assertFalse(self.run_check("SPEC-BODY-008", "ws-prewarm-frame-no-root-turn-id", self.compact_session(main=prewarm_with_root)))
        # 压缩帧带 access_programs（旧行为：按普通业务帧补）。
        def compaction_with_access(frames):
            clones = json.loads(json.dumps(frames))
            clones[3]["access_programs"] = {"cyber": "standard"}
            return clones
        self.assertFalse(self.run_check("SPEC-BODY-008", "ws-compaction-frame-no-access-programs", self.compact_session(main=compaction_with_access)))
        # 标题线程不归主线程判据管：标题线程没有 root_turn_id 键也不影响（这里标题线程帧本来就带）。
        self.assertTrue(self.run_check("SPEC-BODY-008", "ws-turn-frame-root-turn-id", self.compact_session()))

    def test_compaction_access_programs_check_is_bound_to_the_compaction_scenario_only(self) -> None:
        rule, check = self.check("SPEC-BODY-008", "ws-compaction-frame-no-access-programs")
        self.assertEqual(check["select"]["scenario_ids"], ["A21"])
        # 其余四条选 A20～A22；规则 scenario_ids 是它们的超集。
        for check_id in self.W3P2_BODY_008[:4]:
            _, item = self.check("SPEC-BODY-008", check_id)
            self.assertEqual(item["select"]["scenario_ids"], ["A20", "A21", "A22"])
        self.assertTrue({"A20", "A21", "A22"} <= set(rule["scenario_ids"]))

    def test_root_thread_key_checks_read_the_turn_metadata_key_list(self) -> None:
        def strip(key):
            def mutate(body, metadata):
                metadata.pop(key, None)
            return mutate
        def with_mutation(kind_index, key, **kwargs):
            def build(frames):
                frames = list(frames)
                kind = ("prewarm", "turn", "turn", "compaction", "turn", "turn")[kind_index]
                frames[kind_index] = self.official_body(kind, mutate=strip(key), **kwargs)
                return frames
            return build
        # 回合帧缺 root_turn_id 键、缺 agent_name 键。
        self.assertFalse(self.run_check("SPEC-BODY-009", "ws-turn-frame-root-turn-id-key",
                                        self.compact_session(main=with_mutation(2, "root_turn_id"))))
        self.assertFalse(self.run_check("SPEC-BODY-009", "ws-turn-frame-agent-name-key",
                                        self.compact_session(main=with_mutation(0, "agent_name", previous=False))))
        # 预热帧带了 root_turn_id 键。
        def prewarm_with_root(frames):
            def mutate(body, metadata):
                metadata["root_turn_id"] = "x"
            return [self.official_body("prewarm", previous=False, mutate=mutate), *frames[1:]]
        self.assertFalse(self.run_check("SPEC-BODY-009", "ws-prewarm-frame-no-root-turn-id-key", self.compact_session(main=prewarm_with_root)))
        # 失败关闭：没有任何带 turn metadata 的 WS 帧时不得空过。
        for check in self.W3P2_BODY_009:
            self.assertFalse(self.run_check("SPEC-BODY-009", check, []))

    def test_turn_state_new_turn_reset_check(self) -> None:
        def record(reason, channel, scenario="A05"):
            return candidate_rule_assertion.Observation(
                record_id="ts", scenario_id=scenario, record_type="turn_state_chain", artifact_path="trace", evidence_paths=("trace",),
                labels={}, data={"reset_reason": reason, "return_channel": channel})
        self.assertTrue(self.run_check("SPEC-BODY-004", "turn-state-new-turn-reset", [record("new_turn", "none")]))
        # 新用户轮次首请求仍回送上一回合的值（旧行为）。
        self.assertFalse(self.run_check("SPEC-BODY-004", "turn-state-new-turn-reset", [record("new_turn", "websocket_client_metadata")]))
        # 失败关闭：没有 new_turn 记录（候选追踪没产出事实）时不得空过。
        self.assertFalse(self.run_check("SPEC-BODY-004", "turn-state-new-turn-reset", [record("account_owner_changed", "none")]))
        self.assertFalse(self.run_check("SPEC-BODY-004", "turn-state-new-turn-reset", []))
        # 带 reset_reason 的记录不进“值保持不变／双通道”两条判据（它们只选没有 reset_reason 的链路）。
        for check in ("turn-state-value-preserved", "turn-state-channels"):
            _, item = self.check("SPEC-BODY-004", check)
            self.assertEqual(item["select"]["where"], [{"operator": "absent", "path": "data.reset_reason"}])


class CandidateA06FrameLabelsTests(unittest.TestCase):
    """证据标签里每个 A06 ``frame_labels`` 选择键，在真实候选帧布局上都必须落在 response.create 消息的起始帧上。

    背景：上一版登记时标签写成原始帧序号 1，候选网关向上游发出的压缩消息被拆成多个分片帧，预热消息占原始帧 0 至 6，
    首个业务消息的起始帧其实是 7；手造观测的单测从不核对标签声明与真实分片布局，所以登记全绿、实采才暴露。
    现在标签按事件类型序号命中（``event:response.create:N``，不依赖分片数），这里仍用真实候选实采的帧布局夹具
    （只记帧序号、事件类型、fin、分片数）核对：旧布局（一条消息拆 7 帧）与官方式布局（一消息一帧）下，选择键都必须
    命中、且命中的是 response.create 起始帧；写成原始帧序号落在续帧上的旧写法必须能被识别。
    """

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "candidate_a06_ws_frames_0164.json"

    @classmethod
    def setUpClass(cls) -> None:
        cls.frames = json.loads(cls.FIXTURE.read_text(encoding="utf-8"))["frames"]

    @staticmethod
    def with_opcode(frames: list[dict]) -> list[dict]:
        """夹具只记起始帧的事件类型；补上数据消息起始帧的操作码，便于调用引擎的选择键解析器。"""

        return [
            {**frame, "opcode": "TEXT" if frame["event_type"] else "CONT"}
            for frame in frames
        ]

    def a06_frame_labels(self, path: Path) -> dict[str, dict[str, str]]:
        document = json.loads(path.read_text(encoding="utf-8"))
        found = [
            rule["frame_labels"]
            for entry in document["entries"]
            if entry["job_id"] == "candidate-frozen-core"
            for rule in entry["rules"]
            if "A06" in rule.get("scenario_ids", []) and "frame_labels" in rule
        ]
        self.assertEqual(len(found), 1, path.name)
        return found[0]

    def test_fixture_is_a_real_fragmented_layout(self) -> None:
        by_index = {item["frame_index"]: item for item in self.frames}
        starts = sorted(index for index, item in by_index.items() if item["event_type"] == "response.create")
        self.assertEqual(starts, [0, 7, 14])
        # 每条消息的分片数与起始帧间距一致，最后一个分片带 fin。
        self.assertEqual([by_index[index]["message_fragment_count"] for index in starts], [7, 7, 3])
        self.assertTrue(by_index[6]["fin"] and by_index[13]["fin"] and by_index[16]["fin"])

    def test_declared_selectors_land_on_response_create_start_frames(self) -> None:
        for suffix in ("0_164_0", "0_165_0"):
            with self.subTest(suffix=suffix):
                labels = self.a06_frame_labels(TOOL_ROOT / f"codex_upgrade_evidence_labels_{suffix}.json")
                resolved = candidate_rule_assertion.resolve_frame_label_selectors(labels, self.with_opcode(self.frames), "A06")
                by_index = {item["frame_index"]: item for item in self.frames}
                for index in resolved:
                    self.assertEqual(by_index[index]["event_type"], "response.create", f"标签落在续帧 {index} 上")
                # 旧写法（原始帧序号 0／7）与新写法（事件类型序号 0／1）在 0.164 的分片布局上落到同一组起始帧。
                self.assertEqual({index: value["frame_role"] for index, value in resolved.items()}, {0: "warmup", 7: "first_business"})

    def test_0165_selectors_do_not_depend_on_fragment_count(self) -> None:
        """0.165 声明按事件类型序号命中：候选改成一消息一帧后（每帧都是起始帧）标签自动落到官方式的 0／1。"""

        labels = self.a06_frame_labels(TOOL_ROOT / "codex_upgrade_evidence_labels_0_165_0.json")
        self.assertEqual(sorted(labels), ["event:response.create:0", "event:response.create:1"])
        single_frame = [
            {"frame_index": index, "event_type": "response.create", "opcode": "TEXT", "fin": True, "message_fragment_count": 1}
            for index in range(3)
        ]
        resolved = candidate_rule_assertion.resolve_frame_label_selectors(labels, single_frame, "A06")
        self.assertEqual({index: value["frame_role"] for index, value in resolved.items()}, {0: "warmup", 1: "first_business"})

    def test_an_index_label_on_a_continuation_frame_is_rejected(self) -> None:
        """上一版登记的错误写法（first_business 绑在原始帧序号 1）在真实分片布局上落在续帧：解析器不拒，但续帧没有 event_type，
        选择器筛不到它——这条测试钉住“续帧没有事件类型”这一前提，旧写法因此会在封存预检零命中。"""

        by_index = {item["frame_index"]: item for item in self.frames}
        self.assertIsNone(by_index[1]["event_type"])
        resolved = candidate_rule_assertion.resolve_frame_label_selectors(
            {"1": {"frame_role": "first_business"}}, self.with_opcode(self.frames), "A06"
        )
        self.assertEqual(list(resolved), [1])
        self.assertIsNone(by_index[list(resolved)[0]]["event_type"])


if __name__ == "__main__":
    unittest.main()
