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


class CodexTerminalStateReceiptGateTests(unittest.TestCase):
    """通用终态收据门禁（Framework §5.7）的正例与失败关闭测试。

    夹具在临时仓库里造一份 0.151 风格终态收据：四份阶段收据、审计索引、
    Runtime Catalog（active 0.151.0，source 指向链末 Campaign）。审计索引复核
    与 git 都走 subprocess，这里用替身按命令区分返回。
    """

    audit_identity = "a" * 64

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.maintenance = self.root / "docs/egress/maintenance"
        self.maintenance.mkdir(parents=True)
        runtime = self.root / "backend/internal/officialegress/catalogdata/runtime"
        (runtime / "release-graphs").mkdir(parents=True)
        (runtime / "profiles/0.151.0").mkdir(parents=True)
        self.stage_paths: dict[str, Path] = {}
        for key in ledger.CODEX_TERMINAL_STAGE_RECEIPT_FIELDS + ("audit_index",):
            path = self.maintenance / f"CODEX_CLI_TEST_{key.upper()}.json"
            path.write_text(json.dumps({"stage": key}) + "\n", encoding="utf-8")
            self.stage_paths[key] = path
        self.graph_path = runtime / "release-graphs/graph.json"
        self.graph_path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {"mode": "active", "build": {"version": "0.151.0", "source": "campaign:c-last/formal"}},
                        {"mode": "retired", "build": {"version": "0.149.1", "source": "campaign:c-old/formal"}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.snapshot_path = runtime / "snapshot-catalog.json"
        self.snapshot_path.write_text("{}\n", encoding="utf-8")
        self.profile_path = runtime / "profiles/0.151.0/profile.json"
        self.profile_path.write_text("{}\n", encoding="utf-8")
        self.catalog_path = runtime / "release-catalog.json"
        self._write_catalog("campaign:c-last/retirement:codex-legacy-profile")
        self.receipt_path = self.maintenance / "CODEX_CLI_TEST_TERMINAL_STATE_RECEIPT.json"
        self.audit_status = "passed"
        self.audit_calls: list[list[str]] = []
        self._write_receipt(self._receipt())
        self.patches = [
            mock.patch.object(ledger, "ROOT", self.root),
            mock.patch.object(ledger, "MAINTENANCE_ROOT", self.maintenance),
            mock.patch.object(ledger, "RUNTIME_CATALOG_PATH", self.catalog_path),
            mock.patch.object(
                ledger,
                "CODEX_01491_TERMINAL_STATE",
                self.maintenance / "CODEX_CLI_0147_TO_01491_TERMINAL_STATE_RECEIPT.json",
            ),
            mock.patch.object(ledger.subprocess, "run", side_effect=self._fake_subprocess),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def _write_catalog(self, source: str) -> None:
        self.catalog_path.write_text(
            json.dumps(
                {
                    "schema_version": "test",
                    "release_graph": {"path": "catalogdata/runtime/release-graphs/graph.json", "sha256": "0" * 64},
                    "source": source,
                }
            ),
            encoding="utf-8",
        )

    def _binding(self, path: Path) -> dict[str, str]:
        return {"path": path.relative_to(self.root).as_posix(), "sha256": ledger.sha256(path.read_bytes())}

    def _receipt(self, *, trailing_newline: bool = False) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": "official-client-codex-0.151.0-terminal-state/v1",
            "target": {"version": "0.151.0", "previous_version": "0.149.1"},
            "result": "passed",
            "completed_at_utc": "2026-09-11T00:00:00Z",
            "runtime_catalog": {
                "catalog": self._binding(self.catalog_path),
                "release_graph": self._binding(self.graph_path),
                "snapshot_catalog": self._binding(self.snapshot_path),
                "active_profile": self._binding(self.profile_path),
            },
            "audit_index_identity_sha256": self.audit_identity,
            "retired_runtime_profiles": [
                {
                    "path": "backend/internal/officialegress/catalogdata/runtime/profiles/0.147.0/gone.json",
                    "state": "absent",
                }
            ],
            "campaign_chain": [{"campaign_id": "c-first"}, {"campaign_id": "c-last"}],
        }
        for key, path in self.stage_paths.items():
            document[key] = self._binding(path)
        document["identity_sha256"] = ledger.codex_terminal_state_identity(
            document, trailing_newline=trailing_newline
        )
        return document

    def _write_receipt(self, document: dict[str, object]) -> None:
        self.receipt_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _fake_subprocess(self, command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "git":
            raise AssertionError(f"正例不应回读 git 历史：{command}")
        self.audit_calls.append([str(item) for item in command])
        report = {"status": self.audit_status, "identity_sha256": self.audit_identity}
        return SimpleNamespace(returncode=0, stdout=json.dumps(report) + "\n", stderr="")

    def test_generic_receipt_passes_and_reruns_audit_index_check(self) -> None:
        self.assertEqual(
            ledger.validate_codex_terminal_state_receipts(),
            ["docs/egress/maintenance/CODEX_CLI_TEST_TERMINAL_STATE_RECEIPT.json"],
        )
        self.assertEqual(len(self.audit_calls), 1)
        self.assertEqual(self.audit_calls[0][2:4], ["check", "--index"])
        self.assertTrue(self.audit_calls[0][4].endswith("CODEX_CLI_TEST_AUDIT_INDEX.json"))

    def test_accepts_trailing_newline_identity_variant(self) -> None:
        self._write_receipt(self._receipt(trailing_newline=True))
        self.assertEqual(len(ledger.validate_codex_terminal_state_receipts()), 1)

    def test_rejects_identity_drift(self) -> None:
        document = self._receipt()
        document["completed_at_utc"] = "2026-09-12T00:00:00Z"
        self._write_receipt(document)
        with self.assertRaisesRegex(RuntimeError, "自摘要不一致"):
            ledger.validate_codex_terminal_state_receipts()

    def test_rejects_stage_receipt_drift(self) -> None:
        self.stage_paths["post_promotion_gate"].write_text("{\"stage\": \"edited\"}\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "post_promotion_gate 收据 摘要漂移"):
            ledger.validate_codex_terminal_state_receipts()

    def test_rejects_failed_audit_index_check(self) -> None:
        self.audit_status = "failed"
        with self.assertRaisesRegex(RuntimeError, "审计索引复核失败"):
            ledger.validate_codex_terminal_state_receipts()

    def test_rejects_catalog_source_outside_campaign_chain(self) -> None:
        self._write_catalog("campaign:c-other/retirement:codex-legacy-profile")
        self._write_receipt(self._receipt())
        with self.assertRaisesRegex(RuntimeError, "未指向 0.151.0 终态收据的末级 Campaign"):
            ledger.validate_codex_terminal_state_receipts()

    def _write_graph(self, extra_nodes: list[dict[str, object]]) -> None:
        self.graph_path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {"mode": "active", "build": {"version": "0.151.0", "source": "campaign:c-last/formal"}},
                        {"mode": "retired", "build": {"version": "0.149.1", "source": "campaign:c-old/formal"}},
                        *extra_nodes,
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_accepts_candidate_catalog_source_bound_to_previous_nodes(self) -> None:
        # VC-4 候选中间态：0.154.0 候选以 previous 入库，catalog source 指向候选 Campaign。
        candidate = "campaign:c-next/classification:" + "b" * 64
        self._write_graph([{"mode": "previous", "build": {"version": "0.154.0", "source": candidate}}])
        self._write_catalog(candidate)
        self._write_receipt(self._receipt())
        self.assertEqual(len(ledger.validate_codex_terminal_state_receipts()), 1)

    def test_rejects_candidate_catalog_source_without_matching_previous_node(self) -> None:
        candidate = "campaign:c-next/classification:" + "b" * 64
        other = "campaign:c-else/classification:" + "c" * 64
        self._write_graph([{"mode": "previous", "build": {"version": "0.154.0", "source": other}}])
        self._write_catalog(candidate)
        self._write_receipt(self._receipt())
        with self.assertRaisesRegex(RuntimeError, "未指向 0.151.0 终态收据的末级 Campaign"):
            ledger.validate_codex_terminal_state_receipts()

    def test_rejects_candidate_catalog_source_on_terminal_chain(self) -> None:
        candidate = "campaign:c-first/classification:" + "b" * 64
        self._write_graph([{"mode": "previous", "build": {"version": "0.154.0", "source": candidate}}])
        self._write_catalog(candidate)
        self._write_receipt(self._receipt())
        with self.assertRaisesRegex(RuntimeError, "未指向 0.151.0 终态收据的末级 Campaign"):
            ledger.validate_codex_terminal_state_receipts()

    def test_rejects_candidate_catalog_source_without_previous_node(self) -> None:
        candidate = "campaign:c-next/classification:" + "b" * 64
        self._write_catalog(candidate)
        self._write_receipt(self._receipt())
        with self.assertRaisesRegex(RuntimeError, "未指向 0.151.0 终态收据的末级 Campaign"):
            ledger.validate_codex_terminal_state_receipts()

    def test_rejects_retired_profile_still_present(self) -> None:
        still_there = self.root / "backend/internal/officialegress/catalogdata/runtime/profiles/0.147.0/gone.json"
        still_there.parent.mkdir(parents=True)
        still_there.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "已退休的画像仍存在"):
            ledger.validate_codex_terminal_state_receipts()

    def test_active_version_requires_terminal_state_receipt(self) -> None:
        self.receipt_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "0.151.0 缺少终态收据"):
            ledger.validate_codex_terminal_state_receipts()


if __name__ == "__main__":
    unittest.main()
