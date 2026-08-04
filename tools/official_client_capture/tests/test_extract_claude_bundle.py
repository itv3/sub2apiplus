"""Claude Code 2.1.220 bundle 结构化探针的离线测试。"""

from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest


CAPTURE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(CAPTURE_ROOT) not in sys.path:
    sys.path.insert(0, str(CAPTURE_ROOT))

import extract_claude_bundle as extractor  # noqa: E402


EXPECTED_STRUCTURED_PROBES = {
    "CAND-HDR-CLIENT-APP-CONSTRUCTION": (
        '"x-client-app"',
        ("SPEC-HDR-016", "SPEC-HDR-021", "SPEC-HDR-022"),
    ),
    "CAND-HDR-REMOTE-CONTAINER-CONSTRUCTION": (
        '"x-claude-remote-container-id"',
        ("SPEC-HDR-017", "SPEC-HDR-023"),
    ),
    "CAND-HDR-REMOTE-SESSION-CONSTRUCTION": (
        '"x-claude-remote-session-id"',
        ("SPEC-HDR-018", "SPEC-HDR-024"),
    ),
    "CAND-HDR-PARENT-AGENT-CONSTRUCTION": (
        '"x-claude-code-parent-agent-id"',
        ("SPEC-HDR-019", "SPEC-HDR-025"),
    ),
    "CAND-RETRY-DELAY-FORMULA": (
        "Math.round(n+Math.random()*0.25*n)",
        ("SPEC-CONN-002",),
    ),
    "CAND-RETRY-CONSTANT-BLOCK": (
        "qU_=500,VU_=60000,zU_=300000,Flp=21600000,KU_=30000",
        ("SPEC-CONN-002", "SPEC-CONN-003"),
    ),
    "CAND-RETRY-MAIN-MESSAGES-CREATE": (
        "stream:!0},{signal:o,...Object.keys(pi).length>0&&"
        "{headers:pi}}).withResponse()",
        ("SPEC-CONN-002", "SPEC-CONN-003"),
    ),
}


def _fixture_bundle(platform: str) -> str:
    """生成结构相同、minify 符号名不同的两份离线 bundle。"""
    if platform == "linux":
        names = {
            "client_function": "Mde",
            "container": "s",
            "session": "a",
            "client_app": "l",
            "context": "c",
            "encoder": "Zji",
            "retry_function": "Z2e",
            "attempt": "e",
            "retry_after": "t",
            "cap": "r",
            "base": "n",
            "base_constant": "qU_",
            "delay": "o",
            "retry_after_limit": "VU_",
            "persistent_cap": "zU_",
            "persistent_reset_cap": "Flp",
            "heartbeat": "KU_",
            "request_function": "ztp",
            "client": "Qo",
            "params": "ss",
            "credit": "Ya",
            "credit_key": "fallback_credit_token",
            "signal": "o",
            "headers": "pi",
            "response": "kn",
            "entry": "cli",
        }
    elif platform == "darwin":
        names = {
            "client_function": "kde",
            "container": "u",
            "session": "d",
            "client_app": "p",
            "context": "f",
            "encoder": "t6i",
            "retry_function": "eUe",
            "attempt": "i",
            "retry_after": "j",
            "cap": "k",
            "base": "m",
            "base_constant": "e3y",
            "delay": "v",
            "retry_after_limit": "t3y",
            "persistent_cap": "r3y",
            "persistent_reset_cap": "Glp",
            "heartbeat": "n3y",
            "request_function": "wlp",
            "client": "Br",
            "params": "Cr",
            "credit": "Dr",
            "credit_key": "credit_token",
            "signal": "Er",
            "headers": "Fr",
            "response": "Gr",
            "entry": "main",
        }
    else:  # pragma: no cover - 测试调用只会传入两个闭集值
        raise ValueError(platform)

    return (
        f"async function {names['client_function']}(i){{"
        f"let {names['container']}=process.env.CLAUDE_CODE_CONTAINER_ID,"
        f"{names['session']}=process.env.CLAUDE_CODE_REMOTE_SESSION_ID,"
        f"{names['client_app']}=process.env.CLAUDE_AGENT_SDK_CLIENT_APP,"
        f"{names['context']}=i;let h={{"
        f"...{names['container']}&&{{\"x-claude-remote-container-id\":"
        f"{names['container']}}},"
        f"...{names['session']}&&{{\"x-claude-remote-session-id\":"
        f"{names['session']}}},"
        f"...{names['client_app']}&&{{\"x-client-app\":"
        f"{names['client_app']}}},"
        f"...{names['context']}?.agentId&&{{\"x-claude-code-agent-id\":"
        f"{names['encoder']}({names['context']}.agentId)}},"
        f"...{names['context']}?.parentAgentId&&"
        f"{{\"x-claude-code-parent-agent-id\":"
        f"{names['encoder']}({names['context']}.parentAgentId)}}}};return h}}"
        f"function {names['retry_function']}("
        f"{names['attempt']},{names['retry_after']},{names['cap']}=32000){{"
        f"let {names['base']}=Math.min({names['base_constant']}*Math.pow(2,"
        f"{names['attempt']}-1),{names['cap']}),"
        f"{names['delay']}=Math.round({names['base']}+Math.random()*0.25*"
        f"{names['base']});return {names['delay']}}}"
        f"var {names['base_constant']}=500,"
        f"{names['retry_after_limit']}=60000,"
        f"{names['persistent_cap']}=300000,"
        f"{names['persistent_reset_cap']}=21600000,"
        f"{names['heartbeat']}=30000;"
        f"async function {names['request_function']}(){{"
        f"let {names['response']}=await {names['client']}.beta.messages.create("
        f"{{...{names['params']},...{names['credit']}!==void 0&&"
        f"{{{names['credit_key']}:{names['credit']}}},stream:!0}},"
        f"{{signal:{names['signal']},...Object.keys({names['headers']}).length>0&&"
        f"{{headers:{names['headers']}}}}}).withResponse();"
        f"return {names['response']}}}"
        f"function {names['entry']}(){{{names['client_function']}();"
        f"{names['retry_function']}();{names['request_function']}()}}"
        f"{names['entry']}();"
    )


def _structured_by_id(index: dict) -> dict[str, dict]:
    return {
        probe["candidate"]: probe
        for probe in index["probes"]
        if probe.get("locator_kind") == "regex"
    }


class StructuredProbeDefinitionTests(unittest.TestCase):
    def test_probe_ids_literals_and_rule_bindings_are_closed(self) -> None:
        actual = {
            probe.candidate: (probe.literal, probe.rule_ids)
            for probe in extractor.CLAUDE_2_1_220_STRUCTURED_PROBES
        }
        self.assertEqual(set(actual), set(EXPECTED_STRUCTURED_PROBES))
        for probe_id, (literal, rule_ids) in EXPECTED_STRUCTURED_PROBES.items():
            self.assertEqual(actual[probe_id], (literal, rule_ids))

    def test_every_pattern_matches_both_minified_platform_shapes_once(self) -> None:
        for platform in ("linux", "darwin"):
            source = _fixture_bundle(platform)
            for probe in extractor.CLAUDE_2_1_220_STRUCTURED_PROBES:
                with self.subTest(platform=platform, probe=probe.candidate):
                    matches = list(re.finditer(probe.locator_pattern, source))
                    self.assertEqual(len(matches), 1)
                    self.assertIsNotNone(matches[0].group(probe.anchor_group))

    def test_named_backreferences_reject_broken_value_flow(self) -> None:
        source = _fixture_bundle("linux")
        broken = source.replace(
            '...l&&{"x-client-app":l}',
            '...l&&{"x-client-app":wrong}',
        )
        probe = next(
            item
            for item in extractor.CLAUDE_2_1_220_STRUCTURED_PROBES
            if item.candidate == "CAND-HDR-CLIENT-APP-CONSTRUCTION"
        )
        self.assertIsNone(re.search(probe.locator_pattern, broken))

        broken = source.replace(
            "Math.round(n+Math.random()*0.25*n)",
            "Math.round(n+Math.random()*0.25*wrong)",
        )
        probe = next(
            item
            for item in extractor.CLAUDE_2_1_220_STRUCTURED_PROBES
            if item.candidate == "CAND-RETRY-DELAY-FORMULA"
        )
        self.assertIsNone(re.search(probe.locator_pattern, broken))


class StructuredProbeIndexTests(unittest.TestCase):
    def _build(self, root: pathlib.Path, platform: str) -> dict:
        path = root / f"{platform}.js"
        path.write_text(_fixture_bundle(platform), encoding="utf-8")
        return extractor.build_reachability_index(path)

    def test_linux_and_darwin_alpha_anchors_are_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            linux = _structured_by_id(self._build(root, "linux"))
            darwin = _structured_by_id(self._build(root, "darwin"))

        self.assertEqual(set(linux), set(EXPECTED_STRUCTURED_PROBES))
        self.assertEqual(set(darwin), set(EXPECTED_STRUCTURED_PROBES))
        for probe_id in EXPECTED_STRUCTURED_PROBES:
            with self.subTest(probe=probe_id):
                self.assertEqual(linux[probe_id]["hit_count"], 1)
                self.assertEqual(darwin[probe_id]["hit_count"], 1)
                self.assertEqual(
                    linux[probe_id]["hits"][0]["alpha_sha256"],
                    darwin[probe_id]["hits"][0]["alpha_sha256"],
                )

    def test_output_is_deterministic_and_keeps_old_probe_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            path = root / "linux.js"
            path.write_text(_fixture_bundle("linux"), encoding="utf-8")
            first = extractor.build_reachability_index(path)
            second = extractor.build_reachability_index(path)

        self.assertEqual(first["probes"], second["probes"])
        old_count = len(extractor.CANDIDATE_PROBES)
        for record, definition in zip(
            first["probes"][:old_count],
            extractor.CANDIDATE_PROBES,
            strict=True,
        ):
            self.assertEqual(record["candidate"], definition[0])
            self.assertEqual(record["literal"], definition[1])
            self.assertNotIn("locator_kind", record)
            self.assertEqual(
                set(record),
                {"candidate", "literal", "hit_count", "hits"},
            )

        for record in first["probes"][old_count:]:
            self.assertEqual(record["locator_kind"], "regex")
            self.assertEqual(record["hit_count"], 1)
            hit = record["hits"][0]
            self.assertRegex(hit["match_sha256"], r"^[0-9a-f]{64}$")
            self.assertLessEqual(hit["match_start"], hit["offset"])
            self.assertLess(hit["offset"], hit["match_end"])

    def test_anchor_text_covers_required_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = _structured_by_id(
                self._build(pathlib.Path(temporary), "linux")
            )

        client_app = index["CAND-HDR-CLIENT-APP-CONSTRUCTION"]["hits"][0][
            "alpha_text"
        ]
        self.assertIn("process . env . CLAUDE_AGENT_SDK_CLIENT_APP", client_app)
        self.assertIn('"x-client-app"', client_app)

        parent = index["CAND-HDR-PARENT-AGENT-CONSTRUCTION"]["hits"][0][
            "alpha_text"
        ]
        self.assertIn(". parentAgentId", parent)
        self.assertIn('"x-claude-code-parent-agent-id"', parent)

        formula = index["CAND-RETRY-DELAY-FORMULA"]["hits"][0]["alpha_text"]
        self.assertIn("= 32000", formula)
        self.assertIn("Math . pow ( 2", formula)
        self.assertIn("Math . random ( ) * 0.25", formula)

        constants = index["CAND-RETRY-CONSTANT-BLOCK"]["hits"][0]["alpha_text"]
        for value in ("500", "60000", "300000", "21600000", "30000"):
            self.assertIn(value, constants)

        messages = index["CAND-RETRY-MAIN-MESSAGES-CREATE"]["hits"][0][
            "alpha_text"
        ]
        self.assertIn(". beta . messages . create", messages)
        self.assertIn(". withResponse ( )", messages)


if __name__ == "__main__":
    unittest.main()
