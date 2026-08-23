"""UpstreamMergePlan 与 Codex 上游 overlay 台账的失败关闭测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import check_ledger_completeness as ledger


class CheckLedgerCompletenessTests(unittest.TestCase):
    commit = "1" * 40

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "docs/egress/maintenance").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": ledger.UPSTREAM_MERGE_PLAN_SCHEMA,
            "plan_id": "sub2api-v9.9.9-test",
            "purpose": "upstream_merge",
            "upstream": {
                "url": "https://example.invalid/sub2api.git",
                "tag": "v9.9.9",
                "commit": self.commit,
            },
            "outputs": {
                "egress_merge_ledger": (
                    "docs/egress/maintenance/"
                    "upstream-v9.9.9-egress-merge-ledger.json"
                )
            },
            "identity_sha256": "",
        }
        document["identity_sha256"] = ledger.upstream_merge_plan_identity(document)
        return document

    def _write_plan(self, document: dict[str, object], name: str = "plan.json") -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def _load(self, document: dict[str, object]) -> ledger.UpstreamMergePlan:
        return ledger.load_upstream_merge_plan(
            self._write_plan(document),
            repository_root=self.root,
            tag_resolver=lambda _root, _tag: self.commit,
        )

    def test_loads_target_and_output_only_from_plan(self) -> None:
        plan = self._load(self._document())
        self.assertEqual(plan.upstream_tag, "v9.9.9")
        self.assertEqual(plan.upstream_commit, self.commit)
        self.assertEqual(
            plan.ledger_relative,
            "docs/egress/maintenance/upstream-v9.9.9-egress-merge-ledger.json",
        )

    def test_rejects_empty_target_version(self) -> None:
        document = self._document()
        document["upstream"]["tag"] = ""
        document["identity_sha256"] = ledger.upstream_merge_plan_identity(document)
        with self.assertRaisesRegex(RuntimeError, "版本 tag"):
            self._load(document)

    def test_rejects_tag_commit_mismatch(self) -> None:
        document = self._document()
        path = self._write_plan(document)
        with self.assertRaisesRegex(RuntimeError, "tag／commit 不匹配"):
            ledger.load_upstream_merge_plan(
                path,
                repository_root=self.root,
                tag_resolver=lambda _root, _tag: "2" * 40,
            )

    def test_rejects_plan_identity_drift(self) -> None:
        document = self._document()
        document["purpose"] = "baseline_replay"
        with self.assertRaisesRegex(RuntimeError, "identity_sha256 漂移"):
            self._load(document)

    def test_rejects_output_path_escape(self) -> None:
        document = self._document()
        document["outputs"]["egress_merge_ledger"] = "../ledger.json"
        document["identity_sha256"] = ledger.upstream_merge_plan_identity(document)
        with self.assertRaisesRegex(RuntimeError, "逃逸"):
            self._load(document)

    def test_ledger_drift_and_overwrite_are_rejected(self) -> None:
        path = self.root / "docs/egress/maintenance/ledger.json"
        expected = {"schema_version": "upstream-egress-merge-ledger/v1", "overlays": []}
        ledger.write_json_once(path, expected)
        ledger.validate_upstream_merge_ledger(path, expected, "v9.9.9")
        with self.assertRaisesRegex(RuntimeError, "禁止覆盖"):
            ledger.write_json_once(path, expected)
        path.write_text(
            json.dumps({**expected, "overlays": [{"path": "drift"}]}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "overlay 不一致"):
            ledger.validate_upstream_merge_ledger(path, expected, "v9.9.9")

    def test_schema_matches_runtime_version(self) -> None:
        schema_path = Path(ledger.__file__).with_name("upstream_merge_plan.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$defs"]["v1"]["properties"]["schema_version"]["const"],
            ledger.UPSTREAM_MERGE_PLAN_SCHEMA,
        )
        self.assertEqual(
            schema["$defs"]["v2"]["properties"]["schema_version"]["const"],
            ledger.UPSTREAM_MERGE_PLAN_SCHEMA_V2,
        )

    def test_v2_projection_requires_complete_execution_worktree_validation(self) -> None:
        path = self._write_plan(
            {"schema_version": ledger.UPSTREAM_MERGE_PLAN_SCHEMA_V2},
            "v2-plan.json",
        )
        complete = SimpleNamespace(
            document={
                "upstream": {
                    "url": "https://example.invalid/sub2api.git",
                    "tag": "v9.9.9",
                    "commit": self.commit,
                },
                "outputs": {
                    "codex_overlay_ledger": (
                        "docs/egress/maintenance/"
                        "upstream-v9.9.9-egress-merge-ledger.json"
                    )
                },
            },
            plan_id="sub2api-v9.9.9-v2-test",
            identity="2" * 64,
        )
        with mock.patch(
            "tools.upstream_merge.contracts.load_plan",
            return_value=complete,
        ) as load_complete:
            projected = ledger.load_upstream_merge_plan(
                path,
                repository_root=self.root,
            )
        load_complete.assert_called_once_with(
            path.resolve(),
            self.root.resolve(),
            allow_execution_worktree=True,
        )
        self.assertEqual(projected.upstream_commit, self.commit)
        self.assertEqual(projected.identity_sha256, "2" * 64)


if __name__ == "__main__":
    unittest.main()
