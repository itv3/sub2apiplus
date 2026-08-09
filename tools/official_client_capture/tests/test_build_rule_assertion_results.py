"""双侧断言汇总不得放过失败规则或伪造双侧一致。

accept 的 results 文档 schema 只接受 status="pass"；把失败规则写进去等于伪造验收结论。
双侧检查集合必须一致，否则"官方通过、候选也通过"比较的根本不是同一件事。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import build_rule_assertion_results as builder  # noqa: E402


def _document(checks: list[dict]) -> dict:
    return {
        "schema_version": builder.SINGLE_SCHEMA,
        "rule_id": "SPEC-TLS-001",
        "status": "pass",
        "checks": checks,
    }


PASSING = [
    {"id": "cipher-count", "passed": True, "evidence_paths": ["a.pcap"]},
    {"id": "no-alpn", "passed": True, "evidence_paths": ["a.pcap"]},
]


class RuleAssertionResultsTest(unittest.TestCase):
    def test_按执行器报告切分正负断言(self) -> None:
        positive, negative = builder.split_assertions(
            _document(
                [
                    {"id": "cipher-count", "passed": True},
                    {"id": "alpn-absent", "passed": False},
                ]
            )
        )
        self.assertEqual(positive, ["cipher-count"])
        self.assertEqual(negative, ["alpn-absent"])

    def test_没有任何_check_不能作为验收依据(self) -> None:
        with self.assertRaises(builder.RuleAssertionError):
            builder.split_assertions(_document([]))

    def test_双侧检查集合必须一致(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.pcap").write_bytes(b"x")
            official_out = root / "o.json"
            candidate_out = root / "c.json"
            official_out.write_text("{}", encoding="utf-8")
            candidate_out.write_text("{}", encoding="utf-8")
            with self.assertRaises(builder.RuleAssertionError):
                builder.build_rule_result(
                    rule_id="SPEC-TLS-001",
                    official=(["cmd", "a"], _document(PASSING), official_out),
                    candidate=(
                        ["cmd", "b"],
                        _document(
                            [{"id": "other-check", "passed": True, "evidence_paths": ["a.pcap"]}]
                        ),
                        candidate_out,
                    ),
                    official_root=root,
                    candidate_root=root,
                    results_root=root,
                    rationale="双侧独立执行",
                )

    def test_证据引用必须真实存在(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(builder.RuleAssertionError):
                builder.collect_evidence_bindings(_document(PASSING), root)

    def test_未绑定证据的断言不被接受(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(builder.RuleAssertionError):
                builder.collect_evidence_bindings(
                    _document([{"id": "x", "passed": True}]), root
                )

    def test_汇总文档字段闭合且规则有序(self) -> None:
        document = builder.build_results_document(
            candidate_id="candidate-x",
            target_version="0.147.0",
            profile_id="codex-0.147.0-official-k30-v1",
            profile_digest="0" * 64,
            official_package_digest="1" * 64,
            candidate_package_digest="2" * 64,
            comparison_package_digest="3" * 64,
            rules=[{"rule": "SPEC-WS-004"}, {"rule": "SPEC-TLS-001"}],
        )
        self.assertEqual(document["schema_version"], builder.RESULTS_SCHEMA)
        self.assertEqual(document["document_kind"], "results")
        self.assertEqual(
            [item["rule"] for item in document["rules"]],
            ["SPEC-TLS-001", "SPEC-WS-004"],
        )

    def test_空规则集不能生成验收结果(self) -> None:
        with self.assertRaises(builder.RuleAssertionError):
            builder.build_results_document(
                candidate_id="candidate-x",
                target_version="0.147.0",
                profile_id="p",
                profile_digest="0" * 64,
                official_package_digest="1" * 64,
                candidate_package_digest="2" * 64,
                comparison_package_digest="3" * 64,
                rules=[],
            )

    def test_成功路径产出完整双侧绑定(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.pcap").write_bytes(b"evidence")
            official_out = root / "o.json"
            candidate_out = root / "c.json"
            official_out.write_text(json.dumps(_document(PASSING)), encoding="utf-8")
            candidate_out.write_text(json.dumps(_document(PASSING)), encoding="utf-8")
            result = builder.build_rule_result(
                rule_id="SPEC-TLS-001",
                official=(["python3", "assert.py"], _document(PASSING), official_out),
                candidate=(["python3", "assert.py"], _document(PASSING), candidate_out),
                official_root=root,
                candidate_root=root,
                results_root=root,
                rationale="双侧独立执行并通过",
            )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["evidence_level"], "full")
            self.assertEqual(result["positive_assertions"], ["cipher-count", "no-alpn"])
            self.assertEqual(result["negative_assertions"], [])
            self.assertEqual(len(result["official_evidence_refs"]), 1)
            self.assertEqual(len(result["candidate_evidence_refs"]), 1)
            self.assertIn("sha256", result["official_machine_result"])


if __name__ == "__main__":
    unittest.main()
