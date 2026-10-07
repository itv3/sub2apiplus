"""Claude 升级取证 FW-F v5 场景目录（与 v4 同构，判定按场景清单冻结的版本事实取）。

不起客户端、不联网：wire 请求按中继落盘格式（``connNNN.client_to_upstream.bin``）在临时目录里构造；debug 日志行与
advisor 描述符取自 2.1.226 完整取证实测与 2.1.285 官方二进制的同一句式。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4
from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

TOOL_ROOT = Path(__file__).resolve().parents[1]
FACTS_PATH = TOOL_ROOT / "capturelib" / "claude_fw_f_v5_version_facts_2_1_285.json"
# 2.1.226 完整取证（complete-v21）实测的 advisor 宣告行；2.1.285 二进制里是同一个模板字面量。
DEBUG_LINE_226 = "2026-08-19T06:29:43.489Z [DEBUG] [AdvisorTool] Server-side tool enabled with claude-fable-5 as the advisor model"
DEBUG_LINE_285 = "2026-10-07T03:00:00.000Z [DEBUG] [AdvisorTool] Server-side tool enabled with claude-fable-5-1 as the advisor model"
PRIVACY = {"DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def _facts() -> dict:
    return json.loads(FACTS_PATH.read_text(encoding="utf-8"))


def _write_requests(path: Path, requests: list[tuple[str, dict[str, str], dict | None]]) -> None:
    """一条连接上的若干 HTTP/1.1 请求（请求行、额外 Header、JSON Body）。"""

    chunks = []
    for request_line, headers, body in requests:
        raw_body = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        lines = [request_line, "Host: api.anthropic.com", *(f"{name}: {value}" for name, value in headers.items())]
        if raw_body:
            lines += ["Content-Type: application/json", f"Content-Length: {len(raw_body)}"]
        chunks.append(("\r\n".join(lines) + "\r\n\r\n").encode() + raw_body)
    path.write_bytes(b"".join(chunks))


def _summary(*, environment: dict | None = None, **inner) -> dict:
    values = dict(PRIVACY if environment is None else environment)
    return {"inner_result": {"invocation": {"environment": {"values": values}}, **inner}}


class CatalogIsomorphismTests(unittest.TestCase):
    """v5 与 v4 同构：同一组 probe，只有登记的覆盖字段不同。"""

    def test_same_probes_and_only_registered_overrides(self) -> None:
        v5.validate_probe_catalog()
        self.assertEqual(v5.PROBE_IDS, v4.PROBE_IDS)
        self.assertEqual(len(v5.PROBES), 79)
        for probe_id in v5.PROBE_IDS:
            base, probe = v4.PROBES[probe_id], v5.PROBES[probe_id]
            changed = {field.name for field in dataclasses.fields(base) if getattr(base, field.name) != getattr(probe, field.name)}
            with self.subTest(probe=probe_id):
                self.assertEqual(changed, set(v5.PROBE_OVERRIDES.get(probe_id, {})))
                self.assertEqual((probe.dimensions, probe.driver, probe.response_plan), (base.dimensions, base.driver, base.response_plan))
        self.assertEqual(v5.SYNTHETIC_RESPONSE_PLANS, v4.SYNTHETIC_RESPONSE_PLANS)
        document = v5.catalog_document()
        self.assertEqual((document["schema_version"], document["probe_count"], document["required_matrix_dimension_count"]), (v5.SCHEMA_CATALOG, 79, 49))
        self.assertEqual(set(document["observed_only_dimensions"]), {"header.usage_limit", "header.dispatch_id", "aux.usage"})
        conclusions = {probe.runtime_conclusion for probe in v5.PROBES.values() if probe.runtime_conclusion}
        self.assertFalse(any("2_1_" in conclusion for conclusion in conclusions), conclusions)
        self.assertEqual(v5.get_probe("v4-tui-usage").forbidden_requests, ())
        self.assertEqual(set(v5.get_probe("v4-models-privacy-state").forbidden_requests), set(v5.MODELS_FORBIDDEN_IDENTITIES))

    def test_drift_from_v4_fails_closed(self) -> None:
        drifted = dataclasses.replace(v5.PROBES["v4-bash"], prompt="另一个提示词")
        with mock.patch.dict(v5.PROBES, {"v4-bash": drifted}):
            with self.assertRaisesRegex(v5.ClaudeFWFV5ProbeError, "登记的覆盖字段"):
                v5.validate_probe_catalog()
        noop = dict(v5.PROBE_OVERRIDES, **{"v4-bash": {"runtime_conclusion": None}})
        with mock.patch.object(v5, "PROBE_OVERRIDES", noop):
            with self.assertRaisesRegex(v5.ClaudeFWFV5ProbeError, "登记的覆盖字段"):
                v5.validate_probe_catalog()
        versioned = dataclasses.replace(v5.PROBES["v4-models-privacy-state"], runtime_conclusion="x_in_2_1_285")
        overrides = dict(v5.PROBE_OVERRIDES, **{"v4-models-privacy-state": {"runtime_conclusion": "x_in_2_1_285"}})
        with mock.patch.dict(v5.PROBES, {"v4-models-privacy-state": versioned}), mock.patch.object(v5, "PROBE_OVERRIDES", overrides):
            with self.assertRaisesRegex(v5.ClaudeFWFV5ProbeError, "版本号"):
                v5.validate_probe_catalog()


class VersionFactsTests(unittest.TestCase):
    def test_frozen_facts_load(self) -> None:
        digest = hashlib.sha256(FACTS_PATH.read_bytes()).hexdigest()
        facts, binding = v5.load_version_facts(FACTS_PATH.resolve(), digest, expected_version="2.1.285")
        self.assertEqual(facts["advisor"]["tool_type"], "advisor_20260301")
        self.assertEqual(binding["sha256"], digest)
        self.assertEqual(binding["canonical_sha256"], v5.version_facts_sha256(facts))
        self.assertEqual(v5.advisor_model_declarations(f"{DEBUG_LINE_226}\n{DEBUG_LINE_285}\n", facts), ["claude-fable-5", "claude-fable-5-1"])

    def test_rejections(self) -> None:
        cases = (
            ("字段不闭合", lambda facts: facts.update(extra=1), "字段不闭合"),
            ("schema", lambda facts: facts.update(schema_version="x/v1"), "schema_version"),
            ("版本", lambda facts: facts.update(claude_version="2.1.226"), "目标版本"),
            ("类型", lambda facts: facts["advisor"].update(tool_type="advisor tool"), "tool_type"),
            ("句式换行", lambda facts: facts["advisor"]["model_declaration"].update(suffix="a\nb"), "换行"),
            ("句式为空", lambda facts: facts["advisor"]["model_declaration"].update(prefix=""), "非空"),
            ("来源摘要", lambda facts: facts["provenance"].update(binary_sha256="xyz"), "binary_sha256"),
            ("来源依据为空", lambda facts: facts["provenance"].update(basis=" "), "basis"),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                facts = _facts()
                mutate(facts)
                with self.assertRaisesRegex(v5.ClaudeFWFV5ProbeError, message):
                    v5.validate_version_facts(facts, expected_version="2.1.285")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "facts.json"
            target.write_bytes(FACTS_PATH.read_bytes())
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            link = root / "link.json"
            os.symlink(target, link)
            with self.assertRaisesRegex(v5.ClaudeFWFV5ProbeError, "可信绝对普通文件"):
                v5.load_version_facts(link, digest, expected_version="2.1.285")
            with self.assertRaisesRegex(v5.ClaudeFWFV5ProbeError, "摘要"):
                v5.load_version_facts(target, "0" * 64, expected_version="2.1.285")
            target.write_bytes(b"not json")
            with self.assertRaisesRegex(v5.ClaudeFWFV5ProbeError, "合法 JSON"):
                v5.load_version_facts(target, hashlib.sha256(b"not json").hexdigest(), expected_version="2.1.285")

    def test_declaration_parser_ignores_invalid_model_ids(self) -> None:
        facts = _facts()
        lines = "\n".join(
            [
                "[AdvisorTool] Server-side tool enabled with  as the advisor model",
                "[AdvisorTool] Server-side tool enabled with bad model id as the advisor model",
                "[AdvisorTool] Server-side tool enabled with claude-opus-5[1m] as the advisor model",
                "[AdvisorTool] Advisor model: fable",
            ]
        )
        self.assertEqual(v5.advisor_model_declarations(lines, facts), ["claude-opus-5[1m]"])


class JudgmentTests(unittest.TestCase):
    """判定：advisor 正例按 debug 宣告比对；灰度 Header 与用量查询只记录；模型目录与 beta 等其余判定沿用 v4。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.relay = Path(directory.name)
        self.facts = _facts()

    def evidence(self, probe_id: str, summary: dict) -> dict:
        return v5.validate_v5_probe_evidence(
            probe_id=probe_id, relay_root=self.relay, scenario_summary=summary, relay_integrity={}, pcap_receipt={},
            version_facts=self.facts,
        )

    def assertion(self, evidence: dict, dimension: str) -> dict:
        return next(item for item in evidence["dimension_assertions"] if item["dimension"] == dimension)

    def write_advisor_request(self, model: str, **extra) -> None:
        tool = {"type": "advisor_20260301", "name": "advisor", "model": model, **extra}
        _write_requests(self.relay / "conn001.client_to_upstream.bin", [("POST /v1/messages?beta=true HTTP/1.1", {}, {"model": "claude-sonnet-5", "tools": [tool]})])

    def test_advisor_positive_follows_debug_declaration(self) -> None:
        # 2.1.226 实测形态：描述符与宣告都是 claude-fable-5，v4 与 v5 都通过。
        self.write_advisor_request("claude-fable-5")
        declared = _summary(debug_declarations={"present": True, "advisor_models": ["claude-fable-5"]})
        self.assertEqual(self.evidence("v4-advisor-enabled-positive", declared)["result"], "passed")
        legacy = v4.validate_complete_probe_evidence(
            probe_id="v4-advisor-enabled-positive", relay_root=self.relay, scenario_summary=declared, relay_integrity={}, pcap_receipt={}
        )
        self.assertEqual(legacy["result"], "passed")
        # 2.1.285 形态：fable 别名解析到 claude-fable-5-1，描述符多一个 defer_loading；v4 写死的模型名判失败，v5 通过。
        self.write_advisor_request("claude-fable-5-1", defer_loading=True)
        declared = _summary(debug_declarations={"present": True, "advisor_models": ["claude-fable-5-1", "claude-fable-5-1"]})
        evidence = self.evidence("v4-advisor-enabled-positive", declared)
        self.assertEqual(evidence["result"], "passed", evidence["failed_dimensions"])
        observation = self.assertion(evidence, "tool.advisor")["observation"]
        self.assertEqual((observation["declared_advisor_models"], observation["declaration_count"]), (["claude-fable-5-1"], 2))
        self.assertEqual(observation["advisor_descriptors"], [{"type": "advisor_20260301", "model": "claude-fable-5-1", "defer_loading": True}])
        legacy = v4.validate_complete_probe_evidence(
            probe_id="v4-advisor-enabled-positive", relay_root=self.relay, scenario_summary=declared, relay_integrity={}, pcap_receipt={}
        )
        self.assertIn("tool.advisor", legacy["failed_dimensions"])
        for label, summary, facts_type in (
            ("宣告与描述符不符", _summary(debug_declarations={"advisor_models": ["claude-fable-5"]}), None),
            ("没有宣告", _summary(debug_declarations={"advisor_models": []}), None),
            ("宣告了两个模型", _summary(debug_declarations={"advisor_models": ["claude-fable-5-1", "claude-opus-5"]}), None),
            ("类型与版本事实不符", declared, "advisor_20991231"),
        ):
            with self.subTest(label=label):
                if facts_type:
                    self.facts["advisor"]["tool_type"] = facts_type
                evidence = self.evidence("v4-advisor-enabled-positive", summary)
                self.assertIn("tool.advisor", evidence["failed_dimensions"])
                self.facts = _facts()

    def test_gray_headers_are_observed_only(self) -> None:
        subagent = {"x-claude-code-agent-id": "agent-1", "x-claude-code-parent-agent-id": "main"}
        body = {"model": "claude-sonnet-5", "metadata": {"user_id": "x"}, "system": [{"type": "text", "text": "cc_is_subagent=true;"}],
                "tools": [{"name": "Agent"}]}
        _write_requests(
            self.relay / "conn001.client_to_upstream.bin",
            [
                ("POST /v1/messages?beta=true HTTP/1.1", {"anthropic-usage-limit": "slow"}, body),
                ("POST /v1/messages?beta=true HTTP/1.1", {**subagent, "anthropic-usage-limit": "extended"}, body),
            ],
        )
        evidence = self.evidence("v4-agent-usage-limit", _summary())
        observation = self.assertion(evidence, "header.usage_limit")["observation"]
        self.assertEqual(self.assertion(evidence, "header.usage_limit")["result"], "passed")
        self.assertEqual((observation["judgment"], observation["observed"], observation["observed_values"]), ("observed_only", True, ["extended", "slow"]))
        self.assertEqual([(row["subagent_request"], row["value"]) for row in observation["per_request"]], [(False, "slow"), (True, "extended")])
        self.assertEqual(observation["conditions"]["subagent_request_count"], 1)
        self.assertEqual(observation["conditions"]["privacy_environment"], PRIVACY)
        summary = next(item for item in evidence["observed_only"] if item["dimension"] == "header.usage_limit")
        self.assertEqual((summary["observed"], summary["evaluable"], summary["target"]), (True, True, "anthropic-usage-limit"))
        # 同一组请求在 v4 下（实测账号未灰度）判失败；v5 不对「发没发」下结论。
        legacy = v4.validate_complete_probe_evidence(
            probe_id="v4-agent-usage-limit", relay_root=self.relay, scenario_summary=_summary(), relay_integrity={}, pcap_receipt={}
        )
        self.assertIn("header.usage_limit", legacy["failed_dimensions"])
        # dispatch：出现与不出现都通过，事实如实记录；没有推理请求时不可观测，wire 合同与维度一起判失败。
        _write_requests(self.relay / "conn001.client_to_upstream.bin", [("POST /v1/messages?beta=true HTTP/1.1", {"anthropic-dispatch-id": "v2s"}, {"model": "m"})])
        dispatched = self.evidence("v4-dispatch-account-negative", _summary())
        self.assertEqual(dispatched["result"], "passed")
        self.assertEqual(self.assertion(dispatched, "header.dispatch_id")["observation"]["observed_values"], ["v2s"])
        _write_requests(self.relay / "conn001.client_to_upstream.bin", [("POST /v1/messages?beta=true HTTP/1.1", {}, {"model": "m"})])
        quiet = self.evidence("v4-dispatch-account-negative", _summary())
        self.assertEqual((quiet["result"], quiet["observed_only"][0]["observed"]), ("passed", False))
        _write_requests(self.relay / "conn001.client_to_upstream.bin", [("HEAD /api/hello HTTP/1.1", {}, None)])
        empty = self.evidence("v4-dispatch-account-negative", _summary())
        self.assertEqual(empty["result"], "failed")
        self.assertIn("messages_request_missing", empty["wire_contract"]["failures"])
        self.assertFalse(empty["observed_only"][0]["evaluable"])

    def test_usage_query_is_observed_only(self) -> None:
        _write_requests(
            self.relay / "conn001.client_to_upstream.bin",
            [("HEAD /api/hello HTTP/1.1", {}, None), ("GET /api/oauth/usage?at_wall=1&skip_spend=1 HTTP/1.1", {}, None)],
        )
        summary = _summary(input_complete=True)
        evidence = self.evidence("v4-tui-usage", summary)
        self.assertEqual(evidence["result"], "passed", evidence["failed_dimensions"])
        self.assertEqual(evidence["wire_contract"]["forbidden_requests"], [])
        observation = self.assertion(evidence, "aux.usage")["observation"]
        self.assertTrue(observation["observed"])
        self.assertEqual([row["request_target"] for row in observation["observed_requests"]], ["/api/oauth/usage?at_wall=1&skip_spend=1"])
        # 隐私条件没按冻结值生效时不可观测，判失败。
        failed = self.evidence("v4-tui-usage", _summary(environment={"DISABLE_TELEMETRY": "1"}, input_complete=True))
        self.assertIn("aux.usage", failed["failed_dimensions"])

    def test_models_keep_v4_judgment_with_neutral_conclusion(self) -> None:
        _write_requests(self.relay / "conn001.client_to_upstream.bin", [("POST /v1/messages?beta=true HTTP/1.1", {}, {"model": "m"})])
        clean = self.evidence("v4-models-privacy-state", _summary())
        self.assertEqual(clean["result"], "passed", clean["failed_dimensions"])
        self.assertEqual(clean["runtime_conclusion"], v5.MODELS_CONCLUSION)
        _write_requests(
            self.relay / "conn002.client_to_upstream.bin", [("GET /v1/models?beta=true HTTP/1.1", {}, None)]
        )
        requested = self.evidence("v4-models-privacy-state", _summary())
        self.assertIn("aux.models", requested["failed_dimensions"])
        self.assertEqual([row["request_target"] for row in self.assertion(requested, "aux.models")["observation"]["model_list_requests"]], ["/v1/models?beta=true"])

    def test_other_dimensions_and_inventory_match_v4(self) -> None:
        betas = " oauth-2025-04-20, claude-code-20250219,oauth-2025-04-20,, ,oauth-2025-04-20"
        _write_requests(
            self.relay / "conn001.client_to_upstream.bin",
            [("POST /v1/messages?beta=true HTTP/1.1", {"anthropic-beta": betas, "user-agent": "claude-cli/2.1.285 (external, sdk-cli)"}, {"model": "m"})],
        )
        summary = _summary()
        current = self.evidence("v4-replay-beta-deduplicate", summary)
        legacy = v4.validate_complete_probe_evidence(
            probe_id="v4-replay-beta-deduplicate", relay_root=self.relay, scenario_summary=summary, relay_integrity={}, pcap_receipt={}
        )
        self.assertEqual(current["result"], legacy["result"])
        self.assertEqual(current["failed_dimensions"], legacy["failed_dimensions"])
        self.assertEqual(current["actual_wire_inventory"], legacy["actual_wire_inventory"])
        self.assertEqual(current["schema_version"], v5.SCHEMA_DIMENSION_EVIDENCE)
        self.assertEqual(current["version_facts"]["claude_version"], "2.1.285")


class RunnerTests(unittest.TestCase):
    """执行复用 v4 驱动，写 v5 结果；advisor 正例从脱敏归档的 debug 日志取宣告。"""

    def test_print_probe_writes_v5_summary(self) -> None:
        record = {"type": "result", "subtype": "success", "result": "FW_F_V4_OK", "is_error": False}
        completed = subprocess.CompletedProcess(["claude"], 0, stdout=json.dumps(record) + "\n", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            with mock.patch.object(v4.subprocess, "run", return_value=completed):
                result = v5.run_claude_fw_f_v5_probe(
                    claude_bin="/opt/claude", model="claude-sonnet-5", probe_id="v4-models-privacy-state",
                    environment={"PATH": "/usr/bin"}, output_dir=output, timeout=30, known_secrets={}, version_facts=_facts(),
                )
            saved = json.loads((output / v5.SUMMARY_FILENAME).read_text(encoding="utf-8"))
            self.assertTrue(result["valid"])
            self.assertEqual(saved["schema_version"], v5.SCHEMA_RESULT)
            self.assertEqual(saved["runtime_conclusion"], v5.MODELS_CONCLUSION)
            self.assertEqual(saved["catalog_producer"]["sha256"], hashlib.sha256((TOOL_ROOT / "capturelib" / "claude_fw_f_v5.py").read_bytes()).hexdigest())
            self.assertEqual(saved["driver_producer"]["sha256"], hashlib.sha256((TOOL_ROOT / "capturelib" / "claude_fw_f_v4.py").read_bytes()).hexdigest())
            self.assertEqual(saved["version_facts"]["canonical_sha256"], v5.version_facts_sha256(_facts()))
            self.assertFalse((output / "v4-summary.json").exists())

    def test_advisor_probe_records_debug_declarations(self) -> None:
        record = {"type": "result", "subtype": "success", "result": "FW_F_V4_OK", "is_error": False}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result"
            raw_debug = root / "raw-debug.log"

            def run(command, **_kwargs):
                raw_debug.write_text(f"noise\n{DEBUG_LINE_285}\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(record) + "\n", stderr="")

            environment = {"PATH": "/usr/bin", "CLAUDE_CODE_ENABLE_EXPERIMENTAL_ADVISOR_TOOL": "1"}
            with mock.patch.object(v4.subprocess, "run", side_effect=run), mock.patch.object(v4, "_new_debug_path", return_value=raw_debug):
                result = v5.run_claude_fw_f_v5_probe(
                    claude_bin="/opt/claude", model="claude-sonnet-5", probe_id="v4-advisor-enabled-positive",
                    environment=environment, output_dir=output, timeout=30, known_secrets={}, version_facts=_facts(),
                )
            declarations = result["inner_result"]["debug_declarations"]
            self.assertEqual((declarations["present"], declarations["advisor_models"]), (True, ["claude-fable-5-1"]))
            self.assertEqual(declarations["source_sha256"], hashlib.sha256((output / "debug.log").read_bytes()).hexdigest())
            with self.assertRaisesRegex(v5.ClaudeFWFV5ProbeError, "运行环境与冻结目录不一致"):
                v5.run_claude_fw_f_v5_probe(
                    claude_bin="/opt/claude", model="claude-sonnet-5", probe_id="v4-advisor-enabled-positive",
                    environment={"PATH": "/usr/bin"}, output_dir=root / "other", timeout=30, known_secrets={}, version_facts=_facts(),
                )


if __name__ == "__main__":
    unittest.main()
