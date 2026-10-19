"""上游新增发送点预先登记补丁（UM-25）：补丁可直接 git apply，算法后继摘要与补丁后源码一致。"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge.__main__ import main
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.sink_registration import (
    ACCEPTANCE_RELATIVE,
    CLASSIFY_RELATIVE,
    SUCCESSOR_RELATIVE,
    VERSION_LITERAL_RE,
    default_notes,
    draft_sink_registration,
    parse_candidate,
    scanner_algorithm_digest,
    write_sink_registration,
)
from tools.upstream_merge.workflow import _scanner_algorithm_digest

MISSING = "github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testTypeSafeAccountConnection@backend/internal/service/account_test_service_typesafe.go#facade_http_upstream_do#1"
CLASSIFIED = "github.com/Wei-Shaw/sub2api/internal/service.*GatewayService.ForwardSystemOne@backend/internal/service/gateway_systemone.go#facade_http_upstream_do#1"

CLASSIFY_GO = '''package main

var classifyRules = []classifyRule{
\toos("service/seedance.go", "Seedance 视频任务"),
\toos("service/gateway_", "Anthropic 网关"),
}

func classify() {}
'''

ACCEPTANCE_GO = '''package main

// upstreamPendingAdditionsEvidence 是合并前在主干预先登记上游新增发送点的承接收据。
var upstreamPendingAdditionsEvidence = "docs/egress/maintenance/upstream-v0210-scanner-pending-additions-20260930-freeze-successor.json"

var reviewedPostBootstrapSinkAdditions = []postBootstrapSinkAddition{
\t{
\t\tname:              "upstream-seedance",
\t\tcandidateID:       "github.com/Wei-Shaw/sub2api/internal/service.seedance@backend/internal/service/seedance.go#facade_http_upstream_do#1",
\t\tabsentBeforeMerge: true,
\t\tmergeGroup:        "upstream-typesafe-seedance-opencode-go-claude-reset",
\t},
}

func validate() {}
'''


class SinkRegistrationDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        scanner = self.root / "backend/cmd/egressscan"
        scanner.mkdir(parents=True)
        (scanner / "classify.go").write_text(CLASSIFY_GO, encoding="utf-8")
        (scanner / "post_bootstrap_acceptance.go").write_text(ACCEPTANCE_GO, encoding="utf-8")
        (scanner / "main.go").write_text("package main\n", encoding="utf-8")
        (scanner / "classify_test.go").write_text("package main\n", encoding="utf-8")
        successor = self.root / SUCCESSOR_RELATIVE
        successor.parent.mkdir(parents=True)
        successor.write_text(
            json.dumps(
                {
                    "schema_version": "official-egress-scanner-algorithm-successor/v1",
                    "from_sha256": "c" * 64,
                    "to_sha256": "0" * 64,
                    "source_transition": "docs/egress/maintenance/old.json",
                    "reviewed_by": "old",
                    "reason": "old",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        for argv in (("git", "init", "-q"), ("git", "add", "-A")):
            subprocess.run(argv, cwd=self.root, check=True)
        self.sinks = [
            {"scan_candidate_id": CLASSIFIED, "missing_classification": False},
            {"scan_candidate_id": MISSING, "missing_classification": True},
        ]

    def _apply(self, patch: str) -> None:
        path = self.root / "registration.patch"
        path.write_text(patch, encoding="utf-8")
        subprocess.run(("git", "apply", "--check", str(path)), cwd=self.root, check=True)
        subprocess.run(("git", "apply", str(path)), cwd=self.root, check=True)
        path.unlink()

    def test_candidate_parsing_and_default_names(self) -> None:
        parsed = parse_candidate(CLASSIFIED)
        self.assertEqual(parsed["function"], "*GatewayService.ForwardSystemOne")
        self.assertEqual(parsed["file"], "backend/internal/service/gateway_systemone.go")
        self.assertEqual(parsed["sink_kind"], "facade_http_upstream_do")
        notes = default_notes(self.sinks, upstream_tag="v0.2.13", date="20261004")
        self.assertEqual(notes["receipt_tag"], "v0213-scanner-pending-additions-20261004")
        self.assertEqual(notes["merge_group"], "upstream-v0213-additions")
        self.assertEqual(
            [entry["name"] for entry in notes["sinks"]],
            ["upstream-v0213-forward-system-one", "upstream-v0213-test-type-safe-account-connection"],
        )
        # 只有缺分类规则的发送点才起草分类规则说明。
        self.assertEqual([entry["classify_rationale"] is None for entry in notes["sinks"]], [True, False])
        with self.assertRaisesRegex(UpstreamMergeError, "无法解析"):
            parse_candidate("not-a-candidate")

    def test_patch_applies_and_successor_matches_patched_scanner(self) -> None:
        draft = draft_sink_registration(self.root, self.sinks, upstream_tag="v0.2.13", date="20261004")
        self._apply(draft["patch"])

        classify = (self.root / CLASSIFY_RELATIVE).read_text(encoding="utf-8")
        # 缺分类的文件在规则表末尾补精确规则；已被前缀规则分类的文件不动分类规则。
        # 写进 Go 源码的默认说明不带版本号，否则会被版本泄漏检查拦下（v0.2.17 合并时手工改过文字）。
        self.assertIn(
            '\toos("service/account_test_service_typesafe.go", '
            '"上游新增发送点（*AccountTestService.testTypeSafeAccountConnection），范围外"),\n',
            classify,
        )
        self.assertLess(classify.index('"service/gateway_"'), classify.index("account_test_service_typesafe.go"))
        self.assertNotIn("gateway_systemone.go", classify)

        acceptance = (self.root / ACCEPTANCE_RELATIVE).read_text(encoding="utf-8")
        self.assertIn(
            'var upstreamV0213PendingAdditionsEvidence = "docs/egress/maintenance/'
            'upstream-v0213-scanner-pending-additions-20261004-freeze-successor.json"',
            acceptance,
        )
        for identifier in (CLASSIFIED, MISSING):
            self.assertIn(f'\t\tcandidateID:       "{identifier}",\n', acceptance)
        self.assertEqual(acceptance.count('\t\tmergeGroup:        "upstream-v0213-additions",\n'), 2)
        self.assertEqual(acceptance.count("\t\tevidenceRef:       upstreamV0213PendingAdditionsEvidence,\n"), 2)
        self.assertIn('\t\tpersona:           "out-of-scope",\n\t\truntimeSinkID:     "",\n', acceptance)
        # 新条目追加在原有条目之后、块结束之前。
        self.assertLess(acceptance.index("upstream-seedance"), acceptance.index("upstream-v0213-forward-system-one"))
        self.assertTrue(acceptance.rstrip().endswith("func validate() {}"))

        successor = json.loads((self.root / SUCCESSOR_RELATIVE).read_text(encoding="utf-8"))
        # 与工作流里扫描器算法摘要的实现逐字节一致（测试文件不计入）。
        self.assertEqual(successor["to_sha256"], _scanner_algorithm_digest(self.root / "backend/cmd/egressscan"))
        self.assertEqual(successor["to_sha256"], draft["scanner_algorithm_sha256"])
        self.assertEqual(successor["from_sha256"], "c" * 64)
        self.assertEqual(successor["source_transition"], draft["receipt_path"])
        self.assertEqual(successor["reviewed_by"], "sub2api-v0.2.13-merge-scanner-pending-additions")
        self.assertEqual(draft["merge_group_size"], 2)

    def test_notes_override_texts_and_digest_follows(self) -> None:
        first = draft_sink_registration(self.root, self.sinks, upstream_tag="v0.2.13", date="20261004")
        notes = json.loads(json.dumps(first["notes"]))
        notes["merge_group"] = "upstream-v0213-typesafe-systemone"
        notes["sinks"][1]["rationale"] = "TypeSafe 账号连通性测试，第三方 API Key 平台"
        notes["successor_reason"] = "人工复核后的原因"
        second = draft_sink_registration(self.root, self.sinks, upstream_tag="v0.2.13", date="20261004", notes=notes)
        self.assertNotEqual(first["scanner_algorithm_sha256"], second["scanner_algorithm_sha256"])
        self._apply(second["patch"])
        acceptance = (self.root / ACCEPTANCE_RELATIVE).read_text(encoding="utf-8")
        self.assertIn('rationale:         "TypeSafe 账号连通性测试，第三方 API Key 平台",', acceptance)
        self.assertEqual(acceptance.count('mergeGroup:        "upstream-v0213-typesafe-systemone",'), 2)
        successor = json.loads((self.root / SUCCESSOR_RELATIVE).read_text(encoding="utf-8"))
        self.assertEqual(successor["reason"], "人工复核后的原因")
        self.assertEqual(successor["to_sha256"], _scanner_algorithm_digest(self.root / "backend/cmd/egressscan"))

    @staticmethod
    def _added_lines_by_file(patch: str) -> dict[str, list[str]]:
        """按文件拆开补丁，收集每个文件的新增行（``+`` 开头，排除 ``+++`` 文件头），去掉行首的 ``+``。"""

        result: dict[str, list[str]] = {}
        current: str | None = None
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                current = line.split(" b/", 1)[1]
                result[current] = []
            elif current is not None and line.startswith("+") and not line.startswith("+++"):
                result[current].append(line[1:])
        return result

    def test_default_notes_carry_no_version_text(self) -> None:
        """默认说明会逐字写进 Go 源码，不得带三段版本号（v0.2.17 合并时因此被版本泄漏检查拦下）。"""

        notes = default_notes(self.sinks, upstream_tag="v0.2.13", date="20261004")
        by_id = {entry["scan_candidate_id"]: entry for entry in notes["sinks"]}
        self.assertEqual(
            by_id[MISSING]["classify_rationale"],
            "上游新增发送点（*AccountTestService.testTypeSafeAccountConnection），范围外",
        )
        self.assertEqual(
            by_id[MISSING]["rationale"],
            "上游新增发送点（*AccountTestService.testTypeSafeAccountConnection，"
            "service/account_test_service_typesafe.go），按范围外登记，不承载官方 OAuth 出站。",
        )
        self.assertEqual(
            by_id[CLASSIFIED]["rationale"],
            "上游新增发送点（*GatewayService.ForwardSystemOne，service/gateway_systemone.go），"
            "按范围外登记，不承载官方 OAuth 出站。",
        )
        for entry in notes["sinks"]:
            for field in ("classify_rationale", "rationale"):
                if entry[field] is not None:
                    self.assertIsNone(VERSION_LITERAL_RE.search(entry[field]), (entry["name"], field))
        # 批次信息改由 name／mergeGroup／收据路径承载，只用 v0213 这种压缩写法，不是三段版本。
        for entry in notes["sinks"]:
            self.assertIn("-v0213-", entry["name"])
            self.assertIsNone(VERSION_LITERAL_RE.search(entry["name"]))
        self.assertIsNone(VERSION_LITERAL_RE.search(notes["merge_group"]))
        self.assertIsNone(VERSION_LITERAL_RE.search(notes["receipt_tag"]))

    def test_default_patch_has_no_version_literal_in_go_sources(self) -> None:
        """默认 notes 生成的补丁里，classify.go 与 post_bootstrap_acceptance.go 的新增行都不含三段版本号。"""

        draft = draft_sink_registration(self.root, self.sinks, upstream_tag="v0.2.13", date="20261004")
        sections = self._added_lines_by_file(draft["patch"])
        for relative in (CLASSIFY_RELATIVE, ACCEPTANCE_RELATIVE):
            # 新增行必须非空，避免补丁拆分出错时空循环假绿。
            self.assertTrue(sections[relative], relative)
            for line in sections[relative]:
                self.assertIsNone(VERSION_LITERAL_RE.search(line), (relative, line))
        # 自检：同一正则确实能在补丁里找到版本号。后继 JSON 的 reason 带“上游 v0.2.13”，它不进 Go 源码，允许保留。
        self.assertTrue(any(VERSION_LITERAL_RE.search(line) for line in sections[SUCCESSOR_RELATIVE]))
        # 应用补丁后整个文件（含上下文行）同样不含三段版本号。
        self._apply(draft["patch"])
        for relative in (CLASSIFY_RELATIVE, ACCEPTANCE_RELATIVE):
            text = (self.root / relative).read_text(encoding="utf-8")
            self.assertIsNone(VERSION_LITERAL_RE.search(text), relative)

    def test_notes_with_version_text_are_rejected(self) -> None:
        """人工改写回灌的说明若带三段版本号，写进 Go 源码会被版本泄漏检查拦下，起草时直接拒绝并点明发送点与字段。"""

        cases = (
            (MISSING, "rationale", "v0.2.13 批次新增，范围外"),
            (MISSING, "classify_rationale", "上游 v0.2.13 新增发送点，范围外"),
            (MISSING, "rationale", "沿用 1.2.3 的做法"),
            # classify_rationale 不是 None 就要检查，哪怕该发送点本来不需要补分类规则。
            (CLASSIFIED, "classify_rationale", "上游 v0.2.13 新增发送点，范围外"),
        )
        for identifier, field, text in cases:
            with self.subTest(identifier=identifier, field=field, text=text):
                notes = default_notes(self.sinks, upstream_tag="v0.2.13", date="20261004")
                entry = next(item for item in notes["sinks"] if item["scan_candidate_id"] == identifier)
                entry[field] = text
                with self.assertRaises(UpstreamMergeError) as caught:
                    draft_sink_registration(
                        self.root, self.sinks, upstream_tag="v0.2.13", date="20261004", notes=notes
                    )
                message = str(caught.exception)
                self.assertIn("版本泄漏", message)
                self.assertIn(identifier, message)
                self.assertIn(field, message)

    def test_version_guard_ignores_numbers_that_are_not_three_segment_versions(self) -> None:
        """防线与 Go 测试同一正则：四段数字、两段数字、v0213 这种压缩写法都不算三段版本号，不误拒。"""

        notes = default_notes(self.sinks, upstream_tag="v0.2.13", date="20261004")
        notes["sinks"][0]["rationale"] = "回环地址 127.0.0.1，v0213 批次，第 1.2 节说明，日期 20261004"
        draft = draft_sink_registration(self.root, self.sinks, upstream_tag="v0.2.13", date="20261004", notes=notes)
        self.assertIn("回环地址 127.0.0.1，v0213 批次，第 1.2 节说明，日期 20261004", draft["patch"])

    def test_notes_must_match_listed_sinks_and_registered_sinks_are_skipped(self) -> None:
        notes = default_notes(self.sinks, upstream_tag="v0.2.13", date="20261004")
        notes["sinks"] = notes["sinks"][:1]
        with self.assertRaisesRegex(UpstreamMergeError, "逐个对应"):
            draft_sink_registration(self.root, self.sinks, upstream_tag="v0.2.13", date="20261004", notes=notes)
        with self.assertRaisesRegex(UpstreamMergeError, "目标 tag 不一致"):
            draft_sink_registration(
                self.root,
                self.sinks,
                upstream_tag="v0.2.14",
                date="20261004",
                notes=default_notes(self.sinks, upstream_tag="v0.2.13", date="20261004"),
            )
        draft = draft_sink_registration(self.root, self.sinks, upstream_tag="v0.2.13", date="20261004")
        self._apply(draft["patch"])
        subprocess.run(("git", "add", "-A"), cwd=self.root, check=True)
        # 已有登记条目的发送点不再起草；全部已登记时直接拒绝。
        with self.assertRaisesRegex(UpstreamMergeError, "都已有登记条目"):
            draft_sink_registration(self.root, self.sinks, upstream_tag="v0.2.13", date="20261004")

    def _report(self) -> dict:
        return {
            "covered_tags": {"tags": [{"tag": "v0.2.12"}, {"tag": "v0.2.13"}]},
            "report": {"scanner_coverage": {"unregistered_added_sinks": self.sinks}},
        }

    def _outside(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_write_from_preflight_report_with_notes_round_trip(self) -> None:
        outside = self._outside()
        with self.assertRaisesRegex(UpstreamMergeError, "主仓库之外"):
            write_sink_registration(self.root, self._report(), self.root / "draft.patch")
        first = write_sink_registration(self.root, self._report(), outside / "pre.sink-registration.patch", date="20261004")
        # 目标 tag 取覆盖区间最后一个；不给 notes 时另写 notes 模板。
        self.assertEqual(first["receipt_tag"], "v0213-scanner-pending-additions-20261004")
        notes_path = outside / "pre.sink-registration.notes.json"
        self.assertEqual(first["notes"], str(notes_path))
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        notes["sinks"][0]["rationale"] = "System One 网关转发，第三方 API Key 平台"
        edited = outside / "edited-notes.json"
        edited.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")
        second = write_sink_registration(self.root, self._report(), outside / "second.patch", notes_path=edited)
        self.assertNotEqual(first["scanner_algorithm_sha256"], second["scanner_algorithm_sha256"])
        self.assertIn("System One 网关转发，第三方 API Key 平台", (outside / "second.patch").read_text(encoding="utf-8"))
        self.assertEqual(second["notes"], str(edited))
        with self.assertRaisesRegex(UpstreamMergeError, "禁止覆盖"):
            write_sink_registration(self.root, self._report(), outside / "second.patch", notes_path=edited)

    def test_write_draft_in_designated_private_root(self) -> None:
        (self.root / ".gitignore").write_text("/local-analysis/\n", encoding="utf-8")
        output = self.root / "local-analysis/upstream/preflight/draft.patch"
        before = subprocess.check_output(["git", "status", "--porcelain"], cwd=self.root)
        result = write_sink_registration(self.root, self._report(), output, date="20261006")
        self.assertEqual(result["result"], "drafted")
        self.assertTrue(output.is_file())
        self.assertTrue(output.with_suffix(".notes.json").is_file())
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.root), before)

    def test_preflight_command_writes_patch_next_to_report(self) -> None:
        outside = self._outside()
        report = {**self._report(), "result": "blocked", "plan_id": "p", "identity_sha256": "0" * 64, "blockers": ["发送点"]}
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with mock.patch("tools.upstream_merge.__main__.run_preflight", return_value=report), mock.patch(
            "sys.stdout", new=stdout
        ), contextlib.redirect_stderr(io.StringIO()):
            code = main(
                [
                    "preflight",
                    "--repository",
                    str(self.root),
                    "--request",
                    str(outside / "request.json"),
                    "--output",
                    str(outside / "preflight.json"),
                ]
            )
        self.assertEqual(code, 0)
        stdout.seek(0)
        summary = json.loads(stdout.buffer.getvalue().decode("utf-8"))
        self.assertEqual(summary["sink_registration"]["result"], "drafted")
        self.assertTrue((outside / "preflight.sink-registration.patch").is_file())
        self.assertTrue((outside / "preflight.sink-registration.notes.json").is_file())

    def test_digest_algorithm_ignores_test_files(self) -> None:
        sources = {"a.go": b"x", "a_test.go": b"y", "notes.txt": b"z"}
        self.assertEqual(scanner_algorithm_digest(sources), scanner_algorithm_digest({"a.go": b"x"}))


if __name__ == "__main__":
    unittest.main()
