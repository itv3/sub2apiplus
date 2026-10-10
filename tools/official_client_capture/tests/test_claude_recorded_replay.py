"""B5b-02 录制快照验收：来源只读、凭据目录不读、不可变摘要与真实录制评估输入。

这里只验收快照与 regression_fixture，不登记 VC-1／完整链认证。真实录制用例按显式环境变量启用；缺材料如实跳过。
"""

from __future__ import annotations

import hashlib
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import claude_2_1_226_evaluator_fixture as evaluator_fixture
from tools.official_client_capture.tests import claude_recorded_replay as replay


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ClaudeRecordedSnapshotTests(unittest.TestCase):
    """最小来源只服务于快照边界测试，不冒充历史真实录制或正式运行结果。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-recorded-source-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.recorded = self.root / "source" / evaluator_fixture.CAPTURE_ID
        self.supplement = self.recorded.parent / evaluator_fixture.SUPPLEMENT_DIRECTORY
        self.staging = self.root / "staging"
        self._source(self.recorded, ["v4-fixture-probe"])
        self._source(self.supplement, [*evaluator_fixture.SUPPLEMENT_PROBES, "v4-thinking-display-summarized"])

    def _source(self, root: Path, probes: list[str]) -> None:
        _write(root / "campaign.json", {"campaign_id": root.name, "target_version": "2.1.226"})
        _write(root / "scenario-catalog.json", {
            "target_version": "2.1.226",
            "probes": [{"probe_id": probe, "require_debug_log": True, "require_pcap": True} for probe in probes],
        })
        for probe in probes:
            attempt = root / "attempts" / probe / "attempt-001"
            receipt = _write(root / "runtime-receipts" / f"{probe}-attempt-001.json", {
                "schema_version": "official-client-runtime-host-receipt/v1", "run_nonce": probe,
            })
            _write(attempt / "relay-manifest.json", {
                "schema_version": "claude-code-fw-e-relay-run/v1", "status": "complete", "probe_id": probe,
                "client": {"version": "2.1.226 (Claude Code)"}, "dimension_evidence": {"actual_wire_inventory": []},
                "runtime": {"host_runtime_receipt": {
                    "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(), "run_nonce": probe,
                }},
            })
            _write(attempt / "relay" / "relay.json", {"connections": []})
            (attempt / "relay" / "intervention.jsonl").write_bytes(b'{"fixture_only":true}\n')
            (attempt / "relay" / "conn001.client_to_upstream.bin").write_bytes(b"historical-regression-bytes\x00\xff")
            (attempt / "results").mkdir()
            (attempt / "results" / "debug.log").write_text("仅供测试的调试记录", encoding="utf-8")
            (attempt / "tls-clienthello.pcap").write_bytes(b"pcap-fixture")
            for directory in ("state", "recovery"):
                (attempt / directory).mkdir()
                (attempt / directory / "credential-private").write_text("不得读取此测试标记", encoding="utf-8")

    def test_snapshot_copies_exact_allowed_bytes_and_never_reads_credentials(self) -> None:
        before = replay.production_sentinel(self.recorded)
        original_open = Path.open

        def guarded_open(path: Path, *args, **kwargs):
            if any(part in {"state", "recovery"} for part in path.parts):
                self.fail(f"快照读取了凭据目录：{path}")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            binding = replay.build_snapshot(self.staging, recorded=self.recorded)
            index = replay.load_snapshot(binding)
            replay.assert_source_untouched(binding)
        self.assertEqual(replay.production_sentinel(self.recorded), before)
        self.assertEqual(index["purpose"], "regression_fixture")
        self.assertEqual(len(index["probes"]), 2)
        self.assertEqual(index["target_version"], "2.1.226")
        for relative in index["files"]:
            self.assertFalse({"state", "recovery"} & set(Path(relative).parts))
            copied = Path(index["evidence_root"]) / relative
            source = self.recorded.parent / relative
            self.assertEqual(copied.read_bytes(), source.read_bytes())
        self.assertFalse((Path(index["evidence_root"]) / evaluator_fixture.SUPPLEMENT_DIRECTORY / "attempts" / "v4-thinking-display-summarized").exists())

    def second_attempt_selection(self) -> dict:
        """登记明确的第二次测试原件及收据，保留第一次失败；不模拟历史真实来源的摘要。"""

        probe = "v4-thinking-display-summarized"
        first = self.supplement / "attempts" / probe / "attempt-001"
        second = first.with_name("attempt-002")
        import shutil
        shutil.copytree(first, second)
        receipt = self.supplement / "runtime-receipts" / (probe + "-attempt-002.json")
        _write(receipt, {"schema_version": "official-client-runtime-host-receipt/v1", "run_nonce": probe + "-second"})
        document = json.loads((second / "relay-manifest.json").read_text())
        document["runtime"]["host_runtime_receipt"] = {
            "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(), "run_nonce": probe + "-second"}
        _write(second / "relay-manifest.json", document)
        failed = json.loads((first / "relay-manifest.json").read_text())
        failed.update(status="failed", dimension_evidence={})
        _write(first / "relay-manifest.json", failed)
        return {"source_id": self.supplement.name, "probe_id": probe,
                "attempt": f"{self.supplement.name}/attempts/{probe}/attempt-002",
                "host_receipt": f"{self.supplement.name}/runtime-receipts/{probe}-attempt-002.json",
                "manifest_sha256": replay._digest(second / "relay-manifest.json")["sha256"],
                "host_receipt_sha256": replay._digest(receipt)["sha256"]}

    def test_explicit_second_attempt_keeps_old_default_and_binds_its_receipt(self) -> None:
        selection = self.second_attempt_selection()
        default = replay._source_plan(self.recorded)
        self.assertEqual(len(default["probes"]), 2)
        selections = (selection,)
        before = replay.production_sentinel(self.recorded, source_selections=selections)
        binding = replay.build_snapshot(self.staging, recorded=self.recorded, source_selections=selections)
        index = replay.load_snapshot(binding)
        selected = next(row for row in index["probes"] if row["probe_id"] == selection["probe_id"])
        self.assertEqual(selected["attempt"], selection["attempt"])
        self.assertEqual(selected["host_receipt"], selection["host_receipt"])
        self.assertEqual(index["source_selections"], [selection])
        self.assertFalse(index["excluded_sources"])
        self.assertNotIn(selection["attempt"].replace("attempt-002", "attempt-001") + "/relay-manifest.json", index["files"])
        replay.assert_source_untouched(binding)
        self.assertEqual(replay.production_sentinel(self.recorded, source_selections=selections), before)

    def test_explicit_selection_rejects_wrong_receipt_digest_and_missing_exact_attempt(self) -> None:
        selection = self.second_attempt_selection()
        wrong = copy.deepcopy(selection)
        wrong["host_receipt"] = wrong["host_receipt"].replace("attempt-002", "attempt-001")
        with self.assertRaisesRegex(replay.ReplayError, "选择坐标"):
            replay._source_plan(self.recorded, source_selections=(wrong,))
        wrong = {**selection, "manifest_sha256": "0" * 64}
        with self.assertRaisesRegex(replay.ReplayError, "清单摘要漂移"):
            replay._source_plan(self.recorded, source_selections=(wrong,))
        wrong = {**selection, "host_receipt_sha256": "0" * 64}
        with self.assertRaisesRegex(replay.ReplayError, "宿主收据摘要漂移"):
            replay._source_plan(self.recorded, source_selections=(wrong,))
        (self.recorded.parent / selection["attempt"] / "relay-manifest.json").unlink()
        with self.assertRaisesRegex(replay.ReplayError, "不存在"):
            replay._source_plan(self.recorded, source_selections=(selection,))

    def test_snapshot_rejects_artifact_tampering(self) -> None:
        binding = replay.build_snapshot(self.staging, recorded=self.recorded)
        index = replay.load_snapshot(binding)
        relative = next(name for name in index["files"] if name.endswith("client_to_upstream.bin"))
        (Path(index["evidence_root"]) / relative).write_bytes(b"changed")
        with self.assertRaisesRegex(replay.ReplayError, "附件摘要漂移"):
            replay.load_snapshot(binding)

    def test_unregistered_relay_file_is_still_rejected(self) -> None:
        """只补登记干预证据，未知中继附件仍不能混进快照。"""

        path = self.recorded / "attempts" / "v4-fixture-probe" / "attempt-001" / "relay" / "unknown.jsonl"
        path.write_bytes(b'{}\n')
        with self.assertRaisesRegex(replay.ReplayError, "未登记的文件"):
            replay.build_snapshot(self.staging, recorded=self.recorded)

    def test_index_cannot_be_replaced_to_bless_other_bytes(self) -> None:
        binding = replay.build_snapshot(self.staging, recorded=self.recorded)
        path = Path(binding["path"])
        document = json.loads(path.read_text(encoding="utf-8"))
        document["purpose"] = "formal"
        _write(path, document)
        with self.assertRaisesRegex(replay.ReplayError, "索引摘要漂移"):
            replay.load_snapshot(binding)

    def test_extra_credential_directory_is_rejected(self) -> None:
        binding = replay.build_snapshot(self.staging, recorded=self.recorded)
        index = replay.load_snapshot(binding)
        (Path(index["evidence_root"]) / "state").mkdir()
        with self.assertRaisesRegex(replay.ReplayError, "禁止目录"):
            replay.load_snapshot(binding)

    def test_missing_required_pcap_does_not_become_optional(self) -> None:
        (self.recorded / "attempts" / "v4-fixture-probe" / "attempt-001" / "tls-clienthello.pcap").unlink()
        with self.assertRaisesRegex(replay.ReplayError, "不存在"):
            replay.build_snapshot(self.staging, recorded=self.recorded)
        self.assertFalse((self.staging / replay.SNAPSHOT_DIRNAME).exists())

    def test_symlink_in_source_is_not_followed(self) -> None:
        path = self.recorded / "attempts" / "v4-fixture-probe" / "attempt-001" / "relay" / "conn001.client_to_upstream.bin"
        path.unlink()
        path.symlink_to(path.parents[1] / "state" / "credential-private")
        with self.assertRaisesRegex(replay.ReplayError, "符号链接"):
            replay.build_snapshot(self.staging, recorded=self.recorded)

    def test_missing_supplement_and_incomplete_probe_are_rejected(self) -> None:
        path = self.supplement / "attempts" / evaluator_fixture.SUPPLEMENT_PROBES[0] / "attempt-001" / "relay-manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["dimension_evidence"] = {}
        _write(path, document)
        with self.assertRaisesRegex(replay.ReplayError, "缺 wire inventory"):
            replay.build_snapshot(self.staging, recorded=self.recorded)
        path.unlink()
        with self.assertRaisesRegex(replay.ReplayError, "不存在"):
            replay.build_snapshot(self.staging, recorded=self.recorded)

    def test_existing_snapshot_and_source_overlap_are_rejected(self) -> None:
        replay.build_snapshot(self.staging, recorded=self.recorded)
        with self.assertRaisesRegex(replay.ReplayError, "已存在"):
            replay.build_snapshot(self.staging, recorded=self.recorded)
        with self.assertRaisesRegex(replay.ReplayError, "重叠"):
            replay.build_snapshot(self.recorded / "scratch", recorded=self.recorded)

    def test_source_change_after_copy_is_visible(self) -> None:
        binding = replay.build_snapshot(self.staging, recorded=self.recorded)
        path = self.recorded / "attempts" / "v4-fixture-probe" / "attempt-001" / "relay" / "conn001.client_to_upstream.bin"
        path.write_bytes(b"source changed")
        with self.assertRaisesRegex(replay.ReplayError, "历史录制来源"):
            replay.assert_source_untouched(binding)

    def test_evaluation_output_cannot_modify_source_or_snapshot(self) -> None:
        binding = replay.build_snapshot(self.staging, recorded=self.recorded)
        index = replay.load_snapshot(binding)
        for output in (self.recorded / "new.json", Path(index["evidence_root"]) / "new.json"):
            with self.subTest(output=output), self.assertRaisesRegex(replay.ReplayError, "不得写回"):
                replay.write_evaluation_manifest(binding, output)


class ClaudeRecordedEvaluationInputTests(unittest.TestCase):
    """可选真实来源回归：不把历史字节或快照检查冒充 VC 链认证。"""

    def test_recorded_snapshot_feeds_existing_evaluator_fixture(self) -> None:
        configured = os.environ.get(evaluator_fixture.FIXTURE_ENV)
        if not configured:
            self.skipTest("需显式设置 CLAUDE_2_1_226_RELAY_FIXTURE；本用例只验收真实录制的离线回归输入")
        with tempfile.TemporaryDirectory(prefix="claude-recorded-evaluation-") as directory:
            staging = Path(directory)
            binding = replay.build_snapshot(staging, recorded=Path(configured))
            result = replay.write_evaluation_manifest(binding, staging / "evaluation-manifest.json")
            index = replay.load_snapshot(binding)
            self.assertEqual(result["purpose"], "regression_fixture")
            self.assertEqual(len(index["probes"]), 78)
            self.assertGreater(result["artifact_count"], 0)
            replay.assert_source_untouched(binding)


if __name__ == "__main__":
    unittest.main()
