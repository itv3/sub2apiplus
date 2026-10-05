"""Codex candidate 与 post-promotion 外部门禁收据测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_gate_receipt as receipt
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts


class CodexUpgradeGateReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.profile_digest = "1" * 64
        self.package_digest = "2" * 64
        self.source_tree = "3" * 64
        self.image_id = f"sha256:{'4' * 64}"
        self.image_reference = f"registry/sub2api@{self.image_id}"
        self.continuity_by_attempt: dict[str, str] = {}
        self.environment_patcher = mock.patch.object(
            receipt.codex_upgrade_arm64_environment_receipt,
            "replay",
            side_effect=self._replay_environment,
        )
        self.environment_patcher.start()
        # 启动后立即登记清理，setUp 后续步骤失败也不会把替身泄漏给其他测试模块。
        self.addCleanup(self.environment_patcher.stop)
        # 本类使用极简环境 API 替身；完整原 producer 重放及投影在环境收据测试中验证。
        equivalent = mock.patch.object(receipt.codex_upgrade_arm64_environment_receipt, "receipts_equivalent",
                                       side_effect=lambda left_root, left, right_root, right:
                                       left["continuity_identity_sha256"] == right["continuity_identity_sha256"])
        equivalent.start()
        self.addCleanup(equivalent.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _replay_environment(self, root: Path, relative: str) -> dict[str, object]:
        del root
        name = Path(relative).stem
        attempt_id, role = name.rsplit("-", 1)
        return {
            "status": "passed",
            "phase": "gate_before" if role == "before" else "gate_after",
            "subject_id": attempt_id,
            "continuity_identity_sha256": self.continuity_by_attempt.get(
                attempt_id, "8" * 64
            ),
        }

    def _write(self, relative: str, value: object) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _subject(self, phase: str) -> dict[str, object]:
        return {
            "campaign_id": "codex-0_999_0-campaign",
            "campaign_mode": "formal",
            "campaign_purpose": "production_replacement",
            "candidate_id": "candidate-a",
            "candidate_purpose": "production_replacement",
            "target_version": "0.999.0",
            "target_architecture": "linux/amd64",
            "profile_id": "codex-0.999.0",
            "profile_digest": self.profile_digest,
            "candidate_package_digest": self.package_digest,
            "candidate_source_tree_sha256": self.source_tree,
            "candidate_image_id": self.image_id,
            "candidate_image_reference": self.image_reference,
            "production_tree_sha256": "5" * 64 if phase == receipt.POST_PROMOTION_PHASE else None,
            "acceptance_sha256": None,
            "promotion_receipt_sha256": None,
        }

    def _post_gate_plan(
        self,
        subject: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, str]]:
        requirements = vc_artifacts.build_gate_requirements(
            campaign_id=str(subject["campaign_id"]),
            target_version=str(subject["target_version"]),
            joint_manifest_sha256="a" * 64,
            affected_rule_ids=["SPEC-HDR-005"],
            inherited_rule_ids=["SPEC-BODY-001"],
            migration_manifest={"path": "inputs/migration.json", "sha256": "b" * 64},
        )
        mapping = {
            "schema_version": vc_artifacts.GATE_MAPPING_SCHEMA,
            "requirements_sha256": requirements["requirements_sha256"],
            "gates": [
                {
                    "gate_id": row["gate_id"],
                    "test_id": f"post-test-{index:03d}",
                    "working_directory": "backend" if index % 2 else ".",
                    "command": ["python3", "-m", f"post_gate_{index:03d}"],
                    "requirement_sha256": vc_artifacts.digest(row),
                }
                for index, row in enumerate(requirements["requirements"], 1)
            ],
        }
        plan = vc_artifacts.build_gate_plan(requirements, mapping)
        path = self._write("inputs/gate-plan.json", plan)
        return plan, {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": self._digest(path),
        }

    def _gates(
        self,
        phase: str,
        gate_plan: dict[str, object] | None,
    ) -> list[dict[str, object]]:
        if phase == receipt.CANDIDATE_PHASE:
            contracts = [
                {
                    "gate_id": gate_id,
                    "working_directory": cwd,
                    "command": list(command),
                    "test_id": None,
                }
                for gate_id, (cwd, command) in receipt.CANDIDATE_COMMANDS.items()
            ]
        else:
            assert gate_plan is not None
            contracts = list(gate_plan["gates"])
        gates: list[dict[str, object]] = []
        for index, contract in enumerate(
            sorted(contracts, key=lambda item: str(item["gate_id"]))
        ):
            gate_id = str(contract["gate_id"])
            evidence = self._write(
                f"evidence/{phase}-{gate_id}.json",
                {"gate_id": gate_id, "passed": True},
            )
            gate = {
                "gate_id": gate_id,
                "command": list(contract["command"]),
                "working_directory": contract["working_directory"],
                "host": "runner-1",
                "architecture": "linux/amd64",
                "started_at_utc": f"2026-08-23T00:{index:02d}:00Z",
                "completed_at_utc": f"2026-08-23T00:{index:02d}:30Z",
                "exit_code": 0,
                "status": "passed",
                "passed_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "stdout_sha256": "6" * 64,
                "stderr_sha256": "7" * 64,
                "evidence": [
                    {
                        "path": evidence.relative_to(self.root).as_posix(),
                        "sha256": self._digest(evidence),
                    }
                ],
            }
            if phase == receipt.POST_PROMOTION_PHASE:
                gate["test_id"] = contract["test_id"]
            gates.append(gate)
        return gates

    def _environment(self, attempt_id: str) -> dict[str, dict[str, str]]:
        values: dict[str, dict[str, str]] = {}
        for role in ("before", "after"):
            path = self._write(
                f"environment/{attempt_id}-{role}.json",
                {"attempt_id": attempt_id, "role": role},
            )
            values[role] = {
                "path": path.relative_to(self.root).as_posix(),
                "sha256": self._digest(path),
            }
        return values

    def _facts(
        self,
        phase: str,
        *,
        attempt_id: str = "gate-attempt-001",
        root_cause_id: str | None = None,
        previous_receipt: str | None = None,
    ) -> dict[str, object]:
        subject = self._subject(phase)
        gate_plan: dict[str, object] | None = None
        gate_plan_reference: dict[str, str] | None = None
        inputs: list[dict[str, str]] = []
        if phase == receipt.POST_PROMOTION_PHASE:
            gate_plan, gate_plan_reference = self._post_gate_plan(subject)
            acceptance = self._write(
                "inputs/acceptance.json",
                {
                    "status": "complete",
                    "accepted": True,
                    "campaign_mode": subject["campaign_mode"],
                    "campaign_purpose": subject["campaign_purpose"],
                    "candidate_id": subject["candidate_id"],
                    "candidate_purpose": subject["candidate_purpose"],
                    "production_state": "accepted_not_activated",
                    "target_version": subject["target_version"],
                    "profile_id": subject["profile_id"],
                    "profile_digest": subject["profile_digest"],
                    "candidate_package_digest": subject["candidate_package_digest"],
                    "candidate_identity": {
                        "gate_plan": {
                            "sha256": gate_plan_reference["sha256"],
                            "plan_sha256": gate_plan["plan_sha256"],
                            "requirements_sha256": gate_plan["requirements_sha256"],
                        }
                    },
                },
            )
            subject["acceptance_sha256"] = self._digest(acceptance)
            promotion = self._write(
                "inputs/promotion.json",
                {
                    "schema_version": "official-egress-catalog-promotion/v1",
                    "campaign_id": subject["campaign_id"],
                    "acceptance_sha256": subject["acceptance_sha256"],
                    "target_version": subject["target_version"],
                    "target_profile_digest": subject["profile_digest"],
                    "production_selector_changed": True,
                },
            )
            subject["promotion_receipt_sha256"] = self._digest(promotion)
            inputs = [
                {
                    "role": "acceptance",
                    "path": acceptance.relative_to(self.root).as_posix(),
                    "sha256": self._digest(acceptance),
                },
                {
                    "role": "promotion",
                    "path": promotion.relative_to(self.root).as_posix(),
                    "sha256": self._digest(promotion),
                },
            ]
        return {
            "schema_version": receipt.FACTS_SCHEMA,
            "phase": phase,
            "attempt": {
                "attempt_id": attempt_id,
                "root_cause_id": root_cause_id,
                "previous_receipt": (
                    {
                        "path": previous_receipt,
                        "sha256": self._digest(self.root / previous_receipt),
                    }
                    if previous_receipt is not None
                    else None
                ),
            },
            "subject": subject,
            "inputs": inputs,
            "gate_plan": gate_plan_reference,
            "environment": self._environment(attempt_id),
            "gates": self._gates(phase, gate_plan),
        }

    def test_candidate_finalize_and_replay(self) -> None:
        self._write("candidate-facts.json", self._facts(receipt.CANDIDATE_PHASE))
        finalized = receipt.finalize(
            self.root, "candidate-facts.json", "candidate-receipt.json"
        )
        replayed = receipt.replay(self.root, "candidate-receipt.json")
        self.assertEqual(finalized, replayed)
        self.assertEqual(finalized["phase"], receipt.CANDIDATE_PHASE)
        self.assertEqual(finalized["status"], "passed")

    def _target_unit_facts(self):
        from tools.official_client_capture.tests import test_ci_target_platform_gate as target_tests
        case = target_tests.TargetPlatformGateTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        proof = case.target_evidence()
        path = self._write("target-units/evidence.json", proof)
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["subject"].update(**case.request["target"],
                                candidate_image_reference="registry/sub2api@" + case.request["target"]["candidate_image_id"])
        gate = next(row for row in facts["gates"] if row["gate_id"] == "target-platform")
        gate.update(command=target_tests.target.COMMAND, architecture="linux/arm64",
                    unit_execution={"path": "target-units/evidence.json", "sha256": self._digest(path)})
        return case, facts, gate

    def test_target_units_finalize_replay_and_expired_new_consumption(self):
        case, facts, _gate = self._target_unit_facts()
        self._write("unit-facts.json", facts)
        with mock.patch.object(receipt.time, "time", return_value=case.now):
            finalized = receipt.finalize(self.root, "unit-facts.json", "unit-receipt.json")
        gate = next(row for row in finalized["effective_gates"] if row["gate_id"] == "target-platform")
        self.assertNotEqual(gate["command"], ["make", "test"])
        self.assertIn("unit_execution", gate)
        with mock.patch.object(receipt.time, "time", return_value=case.now + 86400):
            self.assertEqual(receipt.replay(self.root, "unit-receipt.json"), finalized)
            with self.assertRaises(receipt.GateReceiptError):
                receipt.build_receipt(self.root, "unit-facts.json")

    def test_target_units_cannot_masquerade_as_fresh_make_test(self):
        case, facts, gate = self._target_unit_facts()
        gate["command"] = ["make", "test"]
        self._write("unit-facts.json", facts)
        with mock.patch.object(receipt.time, "time", return_value=case.now), self.assertRaises(receipt.GateReceiptError):
            receipt.build_receipt(self.root, "unit-facts.json")

    def test_target_verifier_is_pinned_and_cannot_be_selected_by_evidence(self):
        with mock.patch.object(receipt, "TARGET_VERIFIER_SHA256S", {"target_platform_gate.py": "0" * 64}):
            with self.assertRaises(receipt.GateReceiptError):
                receipt._target_gate_verifier()

    def test_post_promotion_binds_acceptance_and_promotion(self) -> None:
        self._write("post-facts.json", self._facts(receipt.POST_PROMOTION_PHASE))
        finalized = receipt.finalize(self.root, "post-facts.json", "post-receipt.json")
        self.assertEqual([item["role"] for item in finalized["inputs"]], ["acceptance", "promotion"])
        self.assertEqual(finalized, receipt.replay(self.root, "post-receipt.json"))

    def test_post_promotion_requires_gate_plan(self) -> None:
        facts = self._facts(receipt.POST_PROMOTION_PHASE)
        facts["gate_plan"] = None
        self._write("missing-plan.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "必须绑定 VC-4"):
            receipt.build_receipt(self.root, "missing-plan.json")

    def test_post_promotion_rejects_command_or_test_id_drift(self) -> None:
        for field, value in (("command", ["python3", "-m", "other"]), ("test_id", "other-test")):
            with self.subTest(field=field):
                facts = self._facts(receipt.POST_PROMOTION_PHASE)
                facts["gates"][0][field] = value
                path = f"drift-{field}.json"
                self._write(path, facts)
                with self.assertRaisesRegex(receipt.GateReceiptError, "冻结合同"):
                    receipt.build_receipt(self.root, path)

    def test_invalid_gate_id_fails_as_managed_error(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["gates"][0]["gate_id"] = []
        self._write("bad-gate-id.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "gate_id"):
            receipt.build_receipt(self.root, "bad-gate-id.json")

    def test_v3_facts_cannot_finalize_but_historical_receipt_replays(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["schema_version"] = receipt.LEGACY_FACTS_SCHEMA
        facts.pop("gate_plan")
        self._write("legacy-facts.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "只允许历史收据重放"):
            receipt.finalize(self.root, "legacy-facts.json", "legacy-new.json")

        historical = receipt.build_receipt(
            self.root,
            "legacy-facts.json",
            _allow_legacy=True,
        )
        historical_path = self.root / "legacy-receipt.json"
        historical_path.write_bytes(receipt._canonical(historical))
        historical_path.chmod(0o600)
        self.assertEqual(
            receipt.replay(self.root, "legacy-receipt.json"),
            historical,
        )

    def test_missing_gate_fails_closed(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["gates"].pop()
        self._write("missing.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "补跑集合非法"):
            receipt.build_receipt(self.root, "missing.json")

    def test_nonzero_or_skipped_gate_fails_closed(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["gates"][0]["skipped_count"] = 1
        self._write("skipped.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "非预期跳过"):
            receipt.build_receipt(self.root, "skipped.json")

    def test_candidate_purpose_drift_fails_closed(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["subject"]["candidate_purpose"] = "validation_only"
        self._write("purpose-drift.json", facts)
        with self.assertRaisesRegex(
            receipt.GateReceiptError,
            "与 Campaign 用途不一致",
        ):
            receipt.build_receipt(self.root, "purpose-drift.json")

    def test_replay_rejects_tampered_gate_evidence(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        self._write("facts.json", facts)
        receipt.finalize(self.root, "facts.json", "receipt.json")
        evidence = self.root / facts["gates"][0]["evidence"][0]["path"]
        self._write(evidence.relative_to(self.root).as_posix(), {"tampered": True})
        with self.assertRaisesRegex(receipt.GateReceiptError, "摘要不一致"):
            receipt.replay(self.root, "receipt.json")

    def test_output_is_write_once(self) -> None:
        self._write("facts.json", self._facts(receipt.CANDIDATE_PHASE))
        receipt.finalize(self.root, "facts.json", "receipt.json")
        with self.assertRaisesRegex(receipt.GateReceiptError, "禁止覆盖"):
            receipt.finalize(self.root, "facts.json", "receipt.json")

    def test_retry_carries_passed_gates_and_only_executes_failed_gate(self) -> None:
        first = self._facts(
            receipt.CANDIDATE_PHASE,
            root_cause_id="root-cause-a",
        )
        first["gates"][1].update(
            {"status": "failed", "exit_code": 1, "failed_count": 1}
        )
        failed_id = first["gates"][1]["gate_id"]
        self._write("attempt-1-facts.json", first)
        failed = receipt.finalize(
            self.root, "attempt-1-facts.json", "attempt-1-receipt.json"
        )
        self.assertEqual(failed["status"], "failed")

        second = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-002",
            root_cause_id="root-cause-a",
            previous_receipt="attempt-1-receipt.json",
        )
        second["gates"] = [
            item for item in second["gates"] if item["gate_id"] == failed_id
        ]
        self._write("attempt-2-facts.json", second)
        completed = receipt.finalize(
            self.root, "attempt-2-facts.json", "attempt-2-receipt.json"
        )
        self.assertEqual(completed["status"], "passed")
        self.assertEqual(completed["executed_gate_ids"], [failed_id])
        self.assertNotIn(failed_id, completed["carried_gate_ids"])
        self.assertEqual(
            sorted(completed["passed_gate_ids"]),
            sorted(receipt.CANDIDATE_COMMANDS),
        )

    def test_retry_rejects_rerunning_an_already_passed_gate(self) -> None:
        first = self._facts(
            receipt.CANDIDATE_PHASE,
            root_cause_id="root-cause-a",
        )
        first["gates"][0].update(
            {"status": "failed", "exit_code": 1, "failed_count": 1}
        )
        self._write("first-facts.json", first)
        receipt.finalize(self.root, "first-facts.json", "first-receipt.json")
        second = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-002",
            root_cause_id="root-cause-a",
            previous_receipt="first-receipt.json",
        )
        self._write("illegal-rerun.json", second)
        with self.assertRaisesRegex(receipt.GateReceiptError, "禁止重跑已通过项"):
            receipt.build_receipt(self.root, "illegal-rerun.json")

    def test_third_same_root_cause_attempt_is_rejected(self) -> None:
        previous: str | None = None
        failed_id = sorted(receipt.CANDIDATE_COMMANDS)[0]
        for index in (1, 2):
            facts = self._facts(
                receipt.CANDIDATE_PHASE,
                attempt_id=f"gate-attempt-00{index}",
                root_cause_id="root-cause-a",
                previous_receipt=previous,
            )
            facts["gates"] = [
                item for item in facts["gates"] if item["gate_id"] == failed_id
            ] if previous else facts["gates"]
            failed_gate = next(
                item for item in facts["gates"] if item["gate_id"] == failed_id
            )
            failed_gate.update(
                {"status": "failed", "exit_code": 1, "failed_count": 1}
            )
            facts_name = f"attempt-{index}-facts.json"
            receipt_name = f"attempt-{index}-receipt.json"
            self._write(facts_name, facts)
            receipt.finalize(self.root, facts_name, receipt_name)
            previous = receipt_name
        third = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-003",
            root_cause_id="root-cause-a",
            previous_receipt=previous,
        )
        third["gates"] = [
            item for item in third["gates"] if item["gate_id"] == failed_id
        ]
        self._write("attempt-3-facts.json", third)
        with self.assertRaisesRegex(receipt.GateReceiptError, "禁止第三次"):
            receipt.build_receipt(self.root, "attempt-3-facts.json")

    def test_retry_rejects_environment_continuity_drift(self) -> None:
        first = self._facts(
            receipt.CANDIDATE_PHASE,
            root_cause_id="root-cause-a",
        )
        first["gates"][0].update(
            {"status": "failed", "exit_code": 1, "failed_count": 1}
        )
        failed_id = first["gates"][0]["gate_id"]
        self._write("first-facts.json", first)
        receipt.finalize(self.root, "first-facts.json", "first-receipt.json")
        self.continuity_by_attempt["gate-attempt-002"] = "9" * 64
        second = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-002",
            root_cause_id="root-cause-a",
            previous_receipt="first-receipt.json",
        )
        second["gates"] = [
            item for item in second["gates"] if item["gate_id"] == failed_id
        ]
        self._write("drift.json", second)
        with self.assertRaisesRegex(receipt.GateReceiptError, "连续性无法证明"):
            receipt.build_receipt(self.root, "drift.json")

    def test_environment_comparison_errors_surface_as_gate_errors(self) -> None:
        """等价比较无法完成（环境模块抛出）时，单次 attempt 与补跑连续性都统一按门禁错误失败关闭。"""

        environment = receipt.codex_upgrade_arm64_environment_receipt
        failure = environment.Arm64EnvironmentReceiptError("facts不是可信普通文件")
        self._write("single.json", self._facts(receipt.CANDIDATE_PHASE))
        with mock.patch.object(environment, "receipts_equivalent", side_effect=failure):
            with self.assertRaisesRegex(receipt.GateReceiptError, "无法完成等价比较：facts不是可信普通文件"):
                receipt.build_receipt(self.root, "single.json")

        first = self._facts(receipt.CANDIDATE_PHASE, root_cause_id="root-cause-a")
        first["gates"][0].update({"status": "failed", "exit_code": 1, "failed_count": 1})
        failed_id = first["gates"][0]["gate_id"]
        self._write("first-facts.json", first)
        receipt.finalize(self.root, "first-facts.json", "first-receipt.json")
        second = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-002",
            root_cause_id="root-cause-a",
            previous_receipt="first-receipt.json",
        )
        second["gates"] = [item for item in second["gates"] if item["gate_id"] == failed_id]
        self._write("retry.json", second)

        def cross_attempt_failure(left_root, left, right_root, right):
            del left_root, right_root
            if left["subject_id"] != right["subject_id"]:
                raise failure
            return left["continuity_identity_sha256"] == right["continuity_identity_sha256"]

        with mock.patch.object(environment, "receipts_equivalent", side_effect=cross_attempt_failure):
            with self.assertRaisesRegex(receipt.GateReceiptError, "连续性无法证明：facts不是可信普通文件"):
                receipt.build_receipt(self.root, "retry.json")

    def test_schema_matches_runtime_version(self) -> None:
        schema_path = Path(receipt.__file__).with_name(
            "codex_upgrade_gate_receipt.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            receipt.RECEIPT_SCHEMA,
        )

    def test_direct_script_help_uses_repository_import_root(self) -> None:
        script = Path(receipt.__file__).resolve()
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parents[2],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("finalize", completed.stdout)

    # ------------------------------------------------------------------
    # 修好接着跑第 40 项：修好生成器后，修改前版本生成的收据按已登记的旧摘要只读重放。
    # 这些用例把当前生成器逐字复制到临时受管坐标（证据根之外），用副本生成收据，再真实改动
    # 副本字节（模拟修好生成器），用改后的副本重放——生成器身份就是文件摘要，必须真改文件。
    # ------------------------------------------------------------------

    GENERATOR_RELATIVE = "tools/official_client_capture/codex_upgrade_gate_receipt.py"

    def _generator_copy(self) -> Path:
        """把当前生成器逐字复制到一个新的临时受管坐标（证据根之外），返回副本路径。"""

        holder = tempfile.TemporaryDirectory(prefix="gate-receipt-generator-")
        self.addCleanup(holder.cleanup)
        path = Path(holder.name) / self.GENERATOR_RELATIVE
        path.parent.mkdir(parents=True)
        path.write_bytes(Path(receipt.__file__).resolve().read_bytes())
        return path

    @staticmethod
    def _load_generator(path: Path):
        """以包内模块名加载副本，使其内部的包内导入照常解析；加载后从 sys.modules 移除。"""

        name = f"tools.official_client_capture._gate_receipt_copy_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module

    @staticmethod
    def _edit_generator(path: Path, *, register: str | None) -> str:
        """改动副本字节；register 给出时把该摘要登记进 v4 只读重放分组。返回改后摘要。"""

        text = path.read_text(encoding="utf-8")
        if register is not None:
            markers = list(re.finditer(r"PRODUCER_SCHEMA: frozenset\(\s*\{\s*", text))
            assert len(markers) == 1, "生成器登记格式变化，测试夹具需要同步更新"
            end = markers[0].end()
            text = text[:end] + f'"{register}",\n            ' + text[end:]
        text += "\n# 测试：修好生成器（字节变化）。\n"
        path.write_text(text, encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _historical_receipt(self, generator: Path, output: str = "receipt.json") -> dict[str, object]:
        """用修改前的副本生成一份 v4 候选外部门禁收据。"""

        before = self._load_generator(generator)
        self._write("facts.json", self._facts(receipt.CANDIDATE_PHASE))
        historical = before.finalize(self.root, "facts.json", output)
        self.assertEqual(
            historical["producer"]["tool_sha256"],
            hashlib.sha256(generator.read_bytes()).hexdigest(),
        )
        self.assertEqual(historical["producer"]["tool"], str(generator.resolve()))
        return historical

    def test_registry_is_keyed_by_current_v4_producer_schema(self) -> None:
        self.assertEqual(set(receipt.REGISTERED_REPLAY_PRODUCER_HASHES), {receipt.PRODUCER_SCHEMA})
        for digest in receipt.REGISTERED_REPLAY_PRODUCER_HASHES[receipt.PRODUCER_SCHEMA]:
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        current = hashlib.sha256(Path(receipt.__file__).resolve().read_bytes()).hexdigest()
        self.assertNotIn(current, receipt.REGISTERED_REPLAY_PRODUCER_HASHES[receipt.PRODUCER_SCHEMA])

    def test_edited_generator_replays_registered_historical_receipt(self) -> None:
        generator = self._generator_copy()
        historical = self._historical_receipt(generator)
        old_digest = str(historical["producer"]["tool_sha256"])
        new_digest = self._edit_generator(generator, register=old_digest)
        self.assertNotEqual(new_digest, old_digest)
        after = self._load_generator(generator)
        self.assertEqual(after.replay(self.root, "receipt.json"), historical)

    def test_edited_generator_without_registration_fails_closed(self) -> None:
        generator = self._generator_copy()
        self._historical_receipt(generator)
        self._edit_generator(generator, register=None)
        after = self._load_generator(generator)
        with self.assertRaisesRegex(after.GateReceiptError, "未登记为只读重放身份"):
            after.replay(self.root, "receipt.json")

    def test_registered_digest_only_replays_and_never_generates(self) -> None:
        generator = self._generator_copy()
        historical = self._historical_receipt(generator, "old-receipt.json")
        new_digest = self._edit_generator(
            generator, register=str(historical["producer"]["tool_sha256"])
        )
        after = self._load_generator(generator)
        fresh = after.finalize(self.root, "facts.json", "new-receipt.json")
        # 新收据只写当前生成器的路径与摘要；除摘要外与修改前生成的收据逐字相同。
        self.assertEqual(fresh["producer"]["tool_sha256"], new_digest)
        self.assertEqual(fresh["producer"]["tool"], str(generator.resolve()))

        def without_digest(value: dict[str, object]) -> dict[str, object]:
            return {**value, "producer": {**value["producer"], "tool_sha256": None}}

        self.assertEqual(without_digest(fresh), without_digest(historical))
        self.assertEqual(after.replay(self.root, "new-receipt.json"), fresh)
        self.assertEqual(after.replay(self.root, "old-receipt.json"), historical)

    def test_current_digest_receipt_still_requires_same_generator_path(self) -> None:
        # 既有行为不变：摘要等于当前生成器的收据，路径不同仍在逐字比较处失败。
        generator = self._generator_copy()
        self._historical_receipt(generator)
        elsewhere = self._generator_copy()
        relocated = self._load_generator(elsewhere)
        self.assertEqual(
            hashlib.sha256(elsewhere.read_bytes()).hexdigest(),
            hashlib.sha256(generator.read_bytes()).hexdigest(),
        )
        with self.assertRaisesRegex(relocated.GateReceiptError, "门禁收据重放结果不一致"):
            relocated.replay(self.root, "receipt.json")

    def test_registered_digest_does_not_relax_generator_path(self) -> None:
        generator = self._generator_copy()
        historical = self._historical_receipt(generator)
        elsewhere = self._generator_copy()
        self._edit_generator(elsewhere, register=str(historical["producer"]["tool_sha256"]))
        relocated = self._load_generator(elsewhere)
        with self.assertRaisesRegex(relocated.GateReceiptError, "不是当前受管生成器路径"):
            relocated.replay(self.root, "receipt.json")

    def test_retry_after_generator_fix_chains_registered_previous_receipt(self) -> None:
        generator = self._generator_copy()
        original = generator.read_bytes()
        before = self._load_generator(generator)
        first = self._facts(receipt.CANDIDATE_PHASE, root_cause_id="root-cause-a")
        first["gates"][1].update({"status": "failed", "exit_code": 1, "failed_count": 1})
        failed_id = first["gates"][1]["gate_id"]
        self._write("attempt-1-facts.json", first)
        failed = before.finalize(self.root, "attempt-1-facts.json", "attempt-1-receipt.json")
        second = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-002",
            root_cause_id="root-cause-a",
            previous_receipt="attempt-1-receipt.json",
        )
        second["gates"] = [item for item in second["gates"] if item["gate_id"] == failed_id]
        self._write("attempt-2-facts.json", second)

        # 修好生成器但未登记修改前摘要：补跑重放前序失败收据时失败关闭，不写输出。
        self._edit_generator(generator, register=None)
        unregistered = self._load_generator(generator)
        with self.assertRaisesRegex(unregistered.GateReceiptError, "未登记为只读重放身份"):
            unregistered.finalize(self.root, "attempt-2-facts.json", "attempt-2-receipt.json")
        self.assertFalse((self.root / "attempt-2-receipt.json").exists())

        # 登记修改前摘要后：补跑承接前序失败收据，新收据由修好后的生成器写入。
        generator.write_bytes(original)
        new_digest = self._edit_generator(generator, register=str(failed["producer"]["tool_sha256"]))
        registered = self._load_generator(generator)
        completed = registered.finalize(self.root, "attempt-2-facts.json", "attempt-2-receipt.json")
        self.assertEqual(completed["status"], "passed")
        self.assertEqual(completed["executed_gate_ids"], [failed_id])
        self.assertEqual(completed["producer"]["tool_sha256"], new_digest)
        self.assertEqual(registered.replay(self.root, "attempt-2-receipt.json"), completed)

    def test_retired_and_unknown_producer_schemas_are_rejected(self) -> None:
        current = str(Path(receipt.__file__).resolve())
        for schema in (
            "codex-upgrade-external-gate-producer/v1",
            "codex-upgrade-external-gate-producer/v2",
            "codex-upgrade-external-gate-producer/v5",
        ):
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(receipt.GateReceiptError, "不受支持"):
                    receipt._replay_producer_identity(
                        {"schema_version": schema, "tool": current, "tool_sha256": "a" * 64}
                    )
        # v3 仍原样承接历史身份（第 40 项之前的既有行为）。
        self.assertEqual(
            receipt._replay_producer_identity(
                {"schema_version": receipt.LEGACY_PRODUCER_SCHEMA, "tool": "/old/tool.py", "tool_sha256": "b" * 64}
            ),
            ("/old/tool.py", "b" * 64),
        )


if __name__ == "__main__":
    unittest.main()
