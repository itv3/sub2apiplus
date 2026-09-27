"""EvidenceManifest 不可变 stat 边界漂移的机器失败分类（2026-09-22，v14r4 批次 15 事故）。

事故：候选 attempt 已封存 EvidenceManifest 之后，驱动脚本重跑时对 ``evidence/client/**`` 再次
chmod，16 个条目的 ctime_ns 漂移；accept 读侧只抛通用 ``ConfigurationError``，父监督器按默认
``execution-failure`` 升级为 ``post-run-tooling`` 并建议逐字重派——但 ctime 不可合法回写、
manifest 是 write-once 产物，当前 attempt 不可恢复。老板修订方案：生产者明确给出
``failure_class="evidence-integrity"`` 与观测 ``evidence-manifest.boundary／stat-boundary-drift``，
监督器只校验记录不反推；账本直接停线；reconciler 固定终态 ``integrity_mismatch``、不生成
post-run-tooling 收据、不返回同批次重派建议。

本文件的端到端用例复用改造 2 的 VC 链集成夹具（只读导入形态的 0.154 Formal Campaign，动作
用合成脚本），VC-5 失败动作在子进程里对**真实**证据根执行 ``verify_manifest_boundary``，
再沿生产包装（``_evidence_manifest_configuration_error`` → ``_record_campaign_run_action_failure``）
写动作诊断；父监督器与 reconciler 全部走真实入口。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_evidence_manifest as evidence_manifest
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import test_codex_upgrade
from tools.official_client_capture.tests.test_codex_upgrade_candidate_revision import R1, _ChainMixin

EXPECTED_OBSERVATION = {
    "check_id": "evidence-manifest.boundary",
    "failure_code": "stat-boundary-drift",
}


def _sealed_evidence_root(root: Path) -> tuple[Path, Path]:
    """建一个私有证据根并生成 EvidenceManifest；返回 (证据根, manifest 路径)。"""

    evidence_root = root / "evidence"
    (evidence_root / "client" / "receipts").mkdir(parents=True)
    for directory in (root, evidence_root, evidence_root / "client", evidence_root / "client" / "receipts"):
        directory.chmod(0o700)
    for name in ("client/receipts/observed-profile-receipt.json", "client/kilo-facts.json"):
        path = evidence_root / name
        path.write_text(json.dumps({"name": name}) + "\n", encoding="utf-8")
        path.chmod(0o600)
    manifest = evidence_manifest.build_evidence_manifest(
        [evidence_root],
        checkpoint_path=root / "manifest.checkpoint.json",
        secret_env_names=(),
    )
    manifest_path = root / "evidence-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return evidence_root, manifest_path


def _drift_ctime_only(evidence_root: Path) -> None:
    """复现事故形态：对已封存文件再次 chmod，最终 mode 不变、只有 ctime_ns 漂移（第三批 R3：元数据漂移，可 rebind）。"""

    for path in sorted(evidence_root.rglob("*")):
        if path.is_file():
            path.chmod(0o400)
            path.chmod(0o600)


def _drift_mode(evidence_root: Path) -> None:
    """完整性异常形态：权限位变化（0600→0400），零内容读取即可判定，仍永久停线。"""

    for path in sorted(evidence_root.rglob("*")):
        if path.is_file():
            path.chmod(0o400)
            break


def _drift_content_same_size(evidence_root: Path) -> None:
    """内容改一个字节、大小不变：零内容读取的边界核验只看到元数据漂移，rebind 复算内容时必须拒绝。"""

    target = next(path for path in sorted(evidence_root.rglob("*")) if path.is_file())
    data = bytearray(target.read_bytes())
    data[0] = (data[0] + 1) % 256
    target.write_bytes(bytes(data))


def _write_rebind(manifest_path: Path, manifest: dict, evidence_root: Path, chain: list) -> list:
    """按生产口径生成并落盘下一份 rebind 收据，返回重放后的链。"""

    payload = evidence_manifest.build_boundary_rebind(
        manifest, manifest_path, [evidence_root], existing_rebinds=chain,
        campaign_id="c", phase="candidate", candidate_id="cand", attempt_id="a1", operator="test",
    )
    path = manifest_path.parent / f"evidence-manifest-rebind-{payload['index']:02d}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return evidence_manifest.load_boundary_rebinds(manifest_path, manifest)


METADATA_OBSERVATION = {
    "check_id": "evidence-manifest.boundary",
    "failure_code": "metadata-only-drift",
}


class EvidenceIntegrityClassificationTests(unittest.TestCase):
    def test_boundary_drift_error_carries_machine_classification(self) -> None:
        """生产者异常自带 failure_class／failure_observations；包装为 ConfigurationError 后原样保留。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            evidence_root, manifest_path = _sealed_evidence_root(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            # 未漂移：复核通过、零扫描。
            passed = evidence_manifest.verify_manifest_boundary(manifest, [evidence_root])
            self.assertEqual((passed["status"], passed["scanned_bytes"]), ("passed", 0))
            # 第三批 R3：只有 ctime 漂移是可恢复的元数据漂移，rebind 后通过并成链。
            _drift_ctime_only(evidence_root)
            with self.assertRaises(evidence_manifest.EvidenceManifestMetadataDriftError) as caught:
                evidence_manifest.verify_manifest_boundary(manifest, [evidence_root])
            error = caught.exception
            self.assertIsInstance(error, evidence_manifest.EvidenceManifestError)
            self.assertEqual(error.failure_class, "evidence-metadata-drift")
            self.assertEqual(error.failure_observations, [METADATA_OBSERVATION])
            self.assertTrue(error.drifted_entries)
            self.assertTrue(all(set(item["fields"]) <= {"mtime_ns", "ctime_ns", "inode"} for item in error.drifted_entries))
            self.assertIn(error.failure_class, supervisor.ACTION_DIAGNOSTIC_FAILURE_CLASSES)
            self.assertIn(error.failure_class, supervisor.RECOVERABLE_ACTION_FAILURE_CLASSES)
            self.assertNotIn(error.failure_class, supervisor.PERMANENT_ACTION_FAILURE_CLASSES)

            wrapped = codex_upgrade._evidence_manifest_configuration_error(error)
            self.assertIsInstance(wrapped, codex_upgrade.EvidenceIntegrityError)
            self.assertIsInstance(wrapped, codex_upgrade.ConfigurationError)
            self.assertEqual(str(wrapped), str(error))
            self.assertEqual((wrapped.failure_class, wrapped.failure_observations), ("evidence-metadata-drift", [METADATA_OBSERVATION]))
            chain = _write_rebind(manifest_path, manifest, evidence_root, [])
            self.assertEqual(len(chain), 1)
            passed = evidence_manifest.verify_manifest_boundary(manifest, [evidence_root], rebinds=chain)
            self.assertEqual((passed["status"], passed["scanned_bytes"], passed["rebind_index"]), ("passed", 0, 1))
            self.assertEqual(codex_upgrade._verify_evidence_manifest_file(manifest_path, [evidence_root])["rebind_index"], 1)
            # 第二次漂移相对链末判定，再 rebind 成链；无漂移时拒绝重复 rebind。
            _drift_ctime_only(evidence_root)
            with self.assertRaises(evidence_manifest.EvidenceManifestMetadataDriftError):
                evidence_manifest.verify_manifest_boundary(manifest, [evidence_root], rebinds=chain)
            chain = _write_rebind(manifest_path, manifest, evidence_root, chain)
            self.assertEqual([item["index"] for item in chain], [1, 2])
            self.assertEqual(chain[1]["previous_rebind_sha256"], chain[0]["receipt_sha256"])
            self.assertEqual(chain[1]["previous_boundary_sha256"], chain[0]["new_boundary_sha256"])
            self.assertEqual(codex_upgrade._verify_evidence_manifest_file(manifest_path, [evidence_root])["rebind_index"], 2)
            with self.assertRaisesRegex(evidence_manifest.EvidenceManifestError, "无需 rebind"):
                _write_rebind(manifest_path, manifest, evidence_root, chain)
            # 篡改链：自摘要与衔接都拦住。
            second_path = manifest_path.parent / "evidence-manifest-rebind-02.json"
            original = second_path.read_text(encoding="utf-8")
            tampered = json.loads(original)
            tampered["new_boundary_sha256"] = "0" * 64
            second_path.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(evidence_manifest.EvidenceManifestError, "自摘要不一致"):
                evidence_manifest.load_boundary_rebinds(manifest_path, manifest)
            second_path.write_text(original, encoding="utf-8")
            first_path = manifest_path.parent / "evidence-manifest-rebind-01.json"
            first_text = first_path.read_text(encoding="utf-8")
            first_path.unlink()
            with self.assertRaisesRegex(evidence_manifest.EvidenceManifestError, "链缺少"):
                evidence_manifest.load_boundary_rebinds(manifest_path, manifest)
            first_path.write_text(first_text, encoding="utf-8")
            first_path.chmod(0o600)
            chain = evidence_manifest.load_boundary_rebinds(manifest_path, manifest)
            # mode 变化：完整性异常（永久类），不能 rebind。
            _drift_mode(evidence_root)
            with self.assertRaises(evidence_manifest.EvidenceManifestBoundaryDriftError) as boundary:
                evidence_manifest.verify_manifest_boundary(manifest, [evidence_root], rebinds=chain)
            self.assertEqual(boundary.exception.failure_class, "evidence-integrity")
            self.assertEqual(boundary.exception.failure_observations, [EXPECTED_OBSERVATION])
            self.assertIn("evidence-integrity", supervisor.PERMANENT_ACTION_FAILURE_CLASSES)
            with self.assertRaisesRegex(evidence_manifest.EvidenceManifestBoundaryDriftError, "不能 rebind"):
                _write_rebind(manifest_path, manifest, evidence_root, chain)
            for path in sorted(evidence_root.rglob("*")):
                if path.is_file():
                    path.chmod(0o600)
            # 内容改一字节（大小不变）：零内容核验只看到元数据漂移，rebind 复算内容时拒绝。
            _drift_content_same_size(evidence_root)
            with self.assertRaises(evidence_manifest.EvidenceManifestMetadataDriftError):
                evidence_manifest.verify_manifest_boundary(manifest, [evidence_root], rebinds=chain)
            with self.assertRaisesRegex(evidence_manifest.EvidenceManifestBoundaryDriftError, "内容与 EvidenceManifest 不一致"):
                _write_rebind(manifest_path, manifest, evidence_root, chain)
            # 其他 EvidenceManifest 异常没有生产者分类：仍是普通 ConfigurationError，不得凭 message 猜。
            plain = codex_upgrade._evidence_manifest_configuration_error(
                evidence_manifest.EvidenceManifestError("EvidenceManifest 的不可变 stat 边界发生漂移。")
            )
            self.assertIs(type(plain), codex_upgrade.ConfigurationError)
            self.assertFalse(hasattr(plain, "failure_class"))


class EvidenceIntegrityCampaignTests(_ChainMixin, unittest.TestCase):
    """端到端：真实漂移 → 动作诊断 evidence-integrity → 账本停线 → 对账终态 integrity_mismatch。"""

    def setUp(self) -> None:
        super().setUp()
        self.helper = test_codex_upgrade.CodexUpgradeTest(
            "test_bound_evidence_path_accepts_legacy_attempt_relative_binding"
        )
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def _drift_plan(self, root: Path, campaign_dir: Path, evidence_root: Path, manifest_path: Path) -> Path:
        """VC-5 动作：在子进程里对真实证据根复核 EvidenceManifest，沿生产包装写动作诊断后退出 1。"""

        repo_root = Path(codex_upgrade.__file__).resolve().parents[2]
        # 第三批 R3：按生产的文件口径核验（含同目录 rebind 链），通过即退出 0，便于 rebind 后逐字重派同一计划。
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[3])\n"
            "from tools.official_client_capture import codex_upgrade\n"
            "try:\n"
            "    codex_upgrade._verify_evidence_manifest_file(Path(sys.argv[2]), [Path(sys.argv[1])])\n"
            "except codex_upgrade.ConfigurationError as error:\n"
            "    codex_upgrade._record_campaign_run_action_failure('handled-error', error)\n"
            "    print(f'升级审计失败：{error}', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n"
        )
        action_id = "candidate-accept"
        plan = {
            "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
            "execute_item_ids": [action_id],
            "reuse_item_ids": [],
            "actions": [
                {
                    "action_id": action_id,
                    "operation": "VC-5:accept",
                    "timeout_seconds": 120,
                    "command": [sys.executable, "-c", script, str(evidence_root), str(manifest_path), str(repo_root)],
                    "item_ids": [action_id],
                }
            ],
        }
        path = root / "action-plans" / "vc-5-drift.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path.resolve(strict=True)

    def test_boundary_drift_action_stops_line_and_reconciles_as_integrity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            campaign_id = str(codex_upgrade._require_formal_campaign(campaign_dir)["campaign_id"])
            self._advance_to_vc3(fixture, root)
            self._open(fixture, R1, initial=True)
            result, returncode = self._dispatch(fixture, root, "VC-4", 4, tag="vc4-r1")
            self.assertEqual(returncode, 0, result)

            evidence_root, manifest_path = _sealed_evidence_root(root / "sealed")
            # 第三批 R3 后 ctime 漂移可恢复，这里用权限位漂移复现完整性异常。
            _drift_mode(evidence_root)
            plan = self._drift_plan(root, campaign_dir, evidence_root, manifest_path)
            result, returncode = codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, "VC-5", 5, plan))
            self.assertEqual(returncode, 1)
            self.assertEqual(result["status"], "failed")
            run = result["campaign_run"]
            self.assertEqual(run["reason"], "action-failed:candidate-accept")
            diagnostic = run["actions"][0]["diagnostic"]
            self.assertEqual(diagnostic["failure_class"], "evidence-integrity")
            self.assertEqual(diagnostic["effective_failure_class"], "evidence-integrity")
            self.assertEqual(diagnostic["failure_observations"], [EXPECTED_OBSERVATION])
            self.assertNotIn("post_run_tooling_receipt", diagnostic)
            self.assertNotIn("post_run_tooling_rejected", diagnostic)
            run_dir = Path(str(run["run_dir"]))
            stored = json.loads((run_dir / diagnostic["path"]).read_text(encoding="utf-8"))
            self.assertEqual(stored["schema_version"], supervisor.ACTION_DIAGNOSTIC_SCHEMA)
            self.assertEqual(stored["error_type"], "EvidenceIntegrityError")
            self.assertEqual(stored["failure_class"], "evidence-integrity")
            self.assertEqual(stored["failure_observations"], [EXPECTED_OBSERVATION])
            self.assertEqual(
                sorted(path.name for path in (run_dir / "action-diagnostics").iterdir()),
                ["action-candidate-accept-failure.json"],
                "evidence-integrity 不得生成 post-run-tooling 收据",
            )
            # 账本：永久失败类直接停线，不进入 recovery_required／candidate_review_required。
            closeout = run["timing_closeout"]
            self.assertEqual((closeout["ledger_status"], closeout["failure_class"]), ("stopped", "evidence-integrity"))
            self.assertNotIn("重派", closeout["next_action"])
            summary = self._summary(fixture)
            self.assertEqual((summary["status"], summary["active_phase"]), ("stopped", None))
            events = [event_type for event_type, _event_id in self._events(fixture)]
            self.assertEqual(events[-1], "stop_the_line")
            self.assertNotIn("recovery_required", events)
            self.assertNotIn("candidate_review_required", events)

            # 对账：固定终态 integrity_mismatch，next_command 是永久停线而不是任何重派建议。
            outcome = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(outcome["status"], reconciler.DECISION_STOP)
            decision = outcome["decision"]
            self.assertEqual(decision["terminal_reason"], "integrity_mismatch")
            self.assertIn("不可变控制或证据制品完整性异常", decision["reasons"][0])
            self.assertEqual(outcome["next_command"], "permanent-stop-integrity_mismatch")
            self.assertNotIn("重派", outcome["next_command"])
            self.assertNotIn("compile-and-run-vc-batch", outcome["next_command"])
            self.assertEqual(
                [(item["check_id"], item["failure_code"]) for item in outcome["failure_observations"]],
                [(EXPECTED_OBSERVATION["check_id"], EXPECTED_OBSERVATION["failure_code"])],
            )
            self.assertEqual(outcome["permanent_stop"]["next_action"], "permanent-stop-integrity_mismatch")
            self.assertEqual(len(str(outcome["permanent_stop"]["terminal_batch"]["batch_sha256"])), 64)
            head = self._head(fixture)
            terminal = head["terminal_campaigns"][campaign_id]
            self.assertEqual(terminal["terminal_reason"], "integrity_mismatch")
            self.assertEqual(terminal["operation_id"], f"campaign-terminal:{campaign_id}")
            self.assertFalse(head["blocked"])
            self.assertEqual(
                sorted(path.name for path in (run_dir / "action-diagnostics").iterdir()),
                ["action-candidate-accept-failure.json"],
                "对账不得补写 post-run-tooling 收据",
            )
            # 对账收据里的父动作有效分类与 declared 分类一致（监督器只校验记录，不反推）。
            receipt_path = campaign_dir / str(outcome["reconciliation_receipt"]["path"])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            action = receipt["run"]["action_diagnostic"]
            self.assertEqual((action["failure_class"], action["declared_failure_class"]), ("evidence-integrity", "evidence-integrity"))
            self.assertEqual(action["error_type"], "EvidenceIntegrityError")
            self.assertNotIn("post_run_tooling", action)
            self.assertEqual(receipt["run"]["failure_class"], "evidence-integrity")
            # 重入：对账幂等，终态不变。
            again = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual((again["status"], again["decision"]["terminal_reason"]), (reconciler.DECISION_STOP, "integrity_mismatch"))
            self.assertEqual(self._head(fixture)["sequence"], head["sequence"])

    def test_metadata_drift_action_pauses_then_rebind_allows_verbatim_redispatch(self) -> None:
        """第三批 R3：seal 后再次 chmod 只让 ctime 漂移——动作诊断 evidence-metadata-drift（可恢复），账本 recovery_required
        而不是 stopped，对账 recoverable 且下一步指向 rebind-boundary；rebind 后逐字重派同一批次通过，Campaign 与 revision 都不变。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            self._advance_to_vc3(fixture, root)
            self._open(fixture, R1, initial=True)
            result, returncode = self._dispatch(fixture, root, "VC-4", 4, tag="vc4-r1")
            self.assertEqual(returncode, 0, result)

            evidence_root, manifest_path = _sealed_evidence_root(root / "sealed")
            _drift_ctime_only(evidence_root)
            plan = self._drift_plan(root, campaign_dir, evidence_root, manifest_path)
            result, returncode = codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, "VC-5", 5, plan))
            self.assertEqual(returncode, 1)
            run = result["campaign_run"]
            self.assertEqual(run["reason"], "action-failed:candidate-accept")
            diagnostic = run["actions"][0]["diagnostic"]
            self.assertEqual(
                (diagnostic["failure_class"], diagnostic["effective_failure_class"]),
                ("evidence-metadata-drift", "evidence-metadata-drift"),
            )
            self.assertEqual(diagnostic["failure_observations"], [METADATA_OBSERVATION])
            closeout = run["timing_closeout"]
            self.assertEqual((closeout["ledger_status"], closeout["failure_class"]), ("recovery_required", "evidence-metadata-drift"))
            self.assertIn("rebind-boundary", closeout["next_action"])
            self.assertEqual(self._summary(fixture)["status"], "recovery_required")
            events = [event_type for event_type, _event_id in self._events(fixture)]
            self.assertNotIn("stop_the_line", events)
            self.assertEqual(events[-1], "recovery_required")

            run_dir = Path(str(run["run_dir"]))
            outcome = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(outcome["status"], reconciler.DECISION_RECOVERABLE, outcome.get("decision"))
            self.assertIn("rebind-boundary", outcome["next_command"])
            self.assertEqual(self._summary(fixture)["status"], "active")
            self.assertEqual(self._head(fixture)["terminal_campaigns"], {})
            receipt = json.loads((campaign_dir / str(outcome["reconciliation_receipt"]["path"])).read_text(encoding="utf-8"))
            self.assertEqual(receipt["run"]["failure_class"], "evidence-metadata-drift")

            # rebind（证据根不在 Campaign attempt 结构内，按生产口径直接写收据），文件口径核验通过。
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            chain = _write_rebind(manifest_path, manifest, evidence_root, [])
            self.assertEqual(len(chain), 1)
            self.assertEqual(codex_upgrade._verify_evidence_manifest_file(manifest_path, [evidence_root])["rebind_index"], 1)
            # 逐字重派同一批次：通过，Campaign 不新建、revision 不变。
            result2, returncode2 = codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, "VC-5", 6, plan))
            self.assertEqual(returncode2, 0, result2)
            self.assertEqual(result2["campaign_run"]["reason"], "queue-complete")
            self.assertEqual(self._summary(fixture)["status"], "active")
            self.assertEqual(self._head(fixture)["terminal_campaigns"], {})


if __name__ == "__main__":
    unittest.main()
