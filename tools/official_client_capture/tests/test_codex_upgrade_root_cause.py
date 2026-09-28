"""结构化根因编码的稳定性、拒绝边界与枚举表身份校验。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_root_cause as root_cause


class StructuredRootCauseTests(unittest.TestCase):
    def test_same_inputs_yield_same_id_across_campaigns(self) -> None:
        """根因 ID 只由稳定输入决定，与 Campaign、时间、路径无关。"""

        first = root_cause.structured_root_cause(
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            failed_step="prepare-official-assertion-bundle",
            stable_dimensions={"phase": "VC-1"},
        )
        second = root_cause.structured_root_cause(
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            failed_step="prepare-official-assertion-bundle",
            stable_dimensions={"phase": "VC-1"},
        )
        self.assertEqual(first, second)
        self.assertTrue(root_cause.is_structured(first))
        other_phase = root_cause.structured_root_cause(
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            failed_step="prepare-official-assertion-bundle",
            stable_dimensions={"phase": "VC-2"},
        )
        self.assertNotEqual(first, other_phase)
        other_step = root_cause.structured_root_cause(
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            failed_step="seal-official-preview",
            stable_dimensions={"phase": "VC-1"},
        )
        self.assertNotEqual(first, other_step)

    def test_describe_exposes_audit_payload_without_diagnostic(self) -> None:
        described = root_cause.describe_root_cause(
            component="vc0-closeout",
            stable_error_code="vc0-closeout.step-failed",
            failed_step="validate-inputs",
        )
        self.assertEqual(
            set(described),
            {
                "algorithm_version",
                "component",
                "stable_error_code",
                "failed_step",
                "stable_dimensions",
                "root_cause_id",
                "codes_sha256",
            },
        )
        self.assertEqual(described["stable_dimensions"], {})
        self.assertEqual(described["algorithm_version"], root_cause.ALGORITHM_VERSION)

    def test_unregistered_code_or_wrong_component_is_rejected(self) -> None:
        with self.assertRaisesRegex(root_cause.RootCauseError, "未登记"):
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="campaign-run.unknown",
                failed_step="x",
                stable_dimensions={"phase": "VC-1"},
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "不能由"):
            root_cause.structured_root_cause(
                component="reconciler",
                stable_error_code="campaign-run.action-failed",
                failed_step="x",
                stable_dimensions={"phase": "VC-1"},
            )

    def test_dimension_keys_must_match_whitelist_exactly(self) -> None:
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="campaign-run.action-failed",
                failed_step="x",
                stable_dimensions={},
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="campaign-run.action-failed",
                failed_step="x",
                stable_dimensions={"phase": "VC-1", "job_id": "official-core"},
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="vc0-closeout",
                stable_error_code="vc0-closeout.step-failed",
                failed_step="x",
                stable_dimensions={"phase": "VC-0"},
            )

    def test_volatile_values_are_rejected_not_stripped(self) -> None:
        """Campaign ID、attempt ID、run ID、时间戳、路径、长摘要一律拒绝。"""

        volatile = (
            "c0154-formal-vc1-bwg-fresh-20260914t223835z",
            "20260914T102852Z-04996800fbbe4e94",
            "run-" + "a" * 64,
            "2026-09-15T08:12:43+00:00",
            "/root/docker/capture-cli/data/runs",
            "step-" + "b" * 32,
        )
        for value in volatile:
            with self.subTest(value=value):
                with self.assertRaisesRegex(root_cause.RootCauseError, "不是稳定根因输入"):
                    root_cause.structured_root_cause(
                        component="vc0-closeout",
                        stable_error_code="vc0-closeout.step-failed",
                        failed_step=value,
                    )
                with self.assertRaisesRegex(root_cause.RootCauseError, "不是稳定根因输入"):
                    root_cause.structured_root_cause(
                        component="supervisor",
                        stable_error_code="campaign-run.action-failed",
                        failed_step="x",
                        stable_dimensions={"phase": value},
                    )

    def test_staging_wal_codes_are_registered_with_expected_components_and_dimensions(self) -> None:
        """改造 4 登记的四条 code：组件与稳定维度闭合，且能生成跨 Campaign 稳定的 ID。"""

        expected = {
            "staging.abandoned": ("orchestrator", ("phase", "stage")),
            "staging.commit-failed": ("orchestrator", ("phase", "stage")),
            "parent-start.failed": ("supervisor", ("phase",)),
            "commit.integrity-mismatch": ("supervisor", ("phase",)),
        }
        codes = root_cause.load_codes()["codes"]
        for code, (component, dimensions) in expected.items():
            with self.subTest(code=code):
                self.assertEqual(codes[code]["component"], component)
                self.assertEqual(codes[code]["stable_dimensions"], dimensions)
                self.assertFalse(codes[code]["legacy"])
                payload = {"phase": "VC-2"}
                if "stage" in dimensions:
                    payload["stage"] = "commit-publish"
                first = root_cause.structured_root_cause(
                    component=component, stable_error_code=code, failed_step="commit-publish", stable_dimensions=payload
                )
                second = root_cause.structured_root_cause(
                    component=component, stable_error_code=code, failed_step="commit-publish", stable_dimensions=payload
                )
                self.assertEqual(first, second)
                with self.assertRaisesRegex(root_cause.RootCauseError, "不能由"):
                    root_cause.structured_root_cause(
                        component="reconciler", stable_error_code=code, failed_step="x", stable_dimensions=payload
                    )
        # 同 code 不同 stage 是不同根因；abandoned 与 commit-failed 互不累计。
        abandoned = root_cause.structured_root_cause(
            component="orchestrator", stable_error_code="staging.abandoned", failed_step="prepare", stable_dimensions={"phase": "VC-2", "stage": "prepare"}
        )
        parent_run = root_cause.structured_root_cause(
            component="orchestrator", stable_error_code="staging.abandoned", failed_step="parent-run", stable_dimensions={"phase": "VC-2", "stage": "parent-run"}
        )
        commit_failed = root_cause.structured_root_cause(
            component="orchestrator", stable_error_code="staging.commit-failed", failed_step="prepare", stable_dimensions={"phase": "VC-2", "stage": "prepare"}
        )
        self.assertEqual(len({abandoned, parent_run, commit_failed}), 3)

    def test_staging_attempt_failed_splits_same_step_by_error_and_keeps_historical_ids(self) -> None:
        """修好接着跑第 32 项：staging.attempt-failed 按异常类型与归一化拒因签名细分同一步骤的失败。

        登记为 orchestrator 生产、维度恰为 phase、stage、error_type、error_signature；同一拒因（签名相同）得同一 ID，
        不同拒因或不同异常类型得不同 ID，且都不等于历史 staging.abandoned 的 ID。历史 ID 字面量逐字不变：194249z
        批次 17 两次 parent-run-create 失败记成的 rc1-9878a53825675a7668ea 仍由 staging.abandoned 旧维度复算得到。
        """

        entry = root_cause.load_codes()["codes"]["staging.attempt-failed"]
        self.assertEqual(entry["component"], "orchestrator")
        self.assertEqual(entry["stable_dimensions"], ("phase", "stage", "error_type", "error_signature"))
        self.assertFalse(entry["legacy"])
        historical = root_cause.structured_root_cause(
            component="orchestrator",
            stable_error_code="staging.abandoned",
            failed_step="parent-run-create",
            stable_dimensions={"phase": "VC-5", "stage": "parent-run-create"},
        )
        self.assertEqual(historical, "rc1-9878a53825675a7668ea")

        def cause(signature: str, error_type: str = "SupervisorError") -> str:
            return root_cause.structured_root_cause(
                component="orchestrator",
                stable_error_code="staging.attempt-failed",
                failed_step="parent-run-create",
                stable_dimensions={
                    "phase": "VC-5",
                    "stage": "parent-run-create",
                    "error_type": error_type,
                    "error_signature": signature,
                },
            )

        unreconciled = cause("es1-" + "1" * 16)
        not_handled = cause("es1-" + "2" * 16)
        self.assertEqual(unreconciled, cause("es1-" + "1" * 16))
        self.assertNotEqual(unreconciled, not_handled)
        self.assertNotEqual(unreconciled, cause("es1-" + "1" * 16, error_type="ConfigurationError"))
        self.assertNotIn(historical, {unreconciled, not_handled})
        # 签名维度同样受波动值拒绝：长摘要、绝对路径不能冒充签名。
        for value in ("f" * 64, "/root/docker/capture-cli/data"):
            with self.subTest(value=value), self.assertRaisesRegex(root_cause.RootCauseError, "不是稳定根因输入"):
                cause(value)
        # 维度键必须恰好四个：少了签名或异常类型都拒绝（不能退化成旧的两维）。
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="orchestrator",
                stable_error_code="staging.attempt-failed",
                failed_step="parent-run-create",
                stable_dimensions={"phase": "VC-5", "stage": "parent-run-create"},
            )

    def test_staging_commit_step_failed_splits_same_step_by_error_and_keeps_historical_ids(self) -> None:
        """修好接着跑第 51 项：staging.commit-step-failed 按异常类型与归一化拒因签名细分同一提交步骤的失败。

        登记为 orchestrator 生产、维度恰为 phase、stage、error_type、error_signature；同一拒因得同一 ID，不同拒因得不同 ID，
        且都不等于历史 staging.commit-failed 的 ID；历史 ID 字面量逐字不变（没有提交失败诊断的父 run 仍按旧维度复算）。
        """

        entry = root_cause.load_codes()["codes"]["staging.commit-step-failed"]
        self.assertEqual(entry["component"], "orchestrator")
        self.assertEqual(entry["stable_dimensions"], ("phase", "stage", "error_type", "error_signature"))
        self.assertFalse(entry["legacy"])
        historical = root_cause.structured_root_cause(
            component="orchestrator",
            stable_error_code="staging.commit-failed",
            failed_step="commit-publish",
            stable_dimensions={"phase": "VC-2", "stage": "commit-publish"},
        )
        self.assertEqual(historical, "rc1-a484558473bf5300fd9b")

        def cause(signature: str, stage: str = "commit-publish") -> str:
            return root_cause.structured_root_cause(
                component="orchestrator",
                stable_error_code="staging.commit-step-failed",
                failed_step=stage,
                stable_dimensions={
                    "phase": "VC-2",
                    "stage": stage,
                    "error_type": "StagingCommitError",
                    "error_signature": signature,
                },
            )

        first = cause("es1-" + "1" * 16)
        self.assertEqual(first, cause("es1-" + "1" * 16))
        self.assertNotEqual(first, cause("es1-" + "2" * 16))
        self.assertNotEqual(first, cause("es1-" + "1" * 16, stage="commit-ledger"))
        self.assertNotIn(historical, {first, cause("es1-" + "2" * 16)})
        # 与第 32 项的 staging.attempt-failed 同维度不同码：同一签名也不会混成一个根因。
        attempt_failed = root_cause.structured_root_cause(
            component="orchestrator",
            stable_error_code="staging.attempt-failed",
            failed_step="commit-publish",
            stable_dimensions={
                "phase": "VC-2",
                "stage": "commit-publish",
                "error_type": "StagingCommitError",
                "error_signature": "es1-" + "1" * 16,
            },
        )
        self.assertNotEqual(first, attempt_failed)
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="orchestrator",
                stable_error_code="staging.commit-step-failed",
                failed_step="commit-publish",
                stable_dimensions={"phase": "VC-2", "stage": "commit-publish"},
            )

    def test_campaign_run_action_error_splits_same_operation_by_error_and_keeps_historical_ids(self) -> None:
        """修好接着跑第 60 项：campaign-run.action-error 按失败种类、异常类型与归一化拒因签名细分同一动作操作的失败。

        登记为 reconciler 生产（与 supervisor-run.interrupted 同一生产者）、维度恰为 phase、failure_kind、error_type、
        error_signature；同一原因（签名相同）得同一 ID，不同拒因、不同异常类型或不同失败种类得不同 ID，且都不等于历史
        supervisor-run.interrupted 的 ID。历史 ID 字面量逐字不变：194249z 项目总账第 287、300 条（VC-5:candidate-recovery
        两次不同原因的失败）都记成的 rc1-f3d2956a367d76818302 仍由 supervisor-run.interrupted 旧维度复算得到。
        """

        codes = root_cause.load_codes()["codes"]
        entry = codes["campaign-run.action-error"]
        self.assertEqual(entry["component"], "reconciler")
        self.assertEqual(entry["stable_dimensions"], ("phase", "failure_kind", "error_type", "error_signature"))
        self.assertFalse(entry["legacy"])
        # 旧码的生产者与维度不变（维度一变旧 ID 全变）。
        self.assertEqual(
            (codes["supervisor-run.interrupted"]["component"], codes["supervisor-run.interrupted"]["stable_dimensions"]),
            ("reconciler", ("phase",)),
        )
        historical = root_cause.structured_root_cause(
            component="reconciler",
            stable_error_code="supervisor-run.interrupted",
            failed_step="VC-5-candidate-recovery",
            stable_dimensions={"phase": "VC-5"},
        )
        self.assertEqual(historical, "rc1-f3d2956a367d76818302")

        def cause(signature: str, *, error_type: str = "ValueError", failure_kind: str = "handled-error") -> str:
            return root_cause.structured_root_cause(
                component="reconciler",
                stable_error_code="campaign-run.action-error",
                failed_step="VC-5-candidate-recovery",
                stable_dimensions={
                    "phase": "VC-5",
                    "failure_kind": failure_kind,
                    "error_type": error_type,
                    "error_signature": signature,
                },
            )

        probe_session = cause("es1-" + "1" * 16)
        rehearsal = cause("es1-" + "2" * 16, error_type="ConfigurationError")
        self.assertEqual(probe_session, cause("es1-" + "1" * 16))
        self.assertNotEqual(probe_session, rehearsal)
        self.assertNotEqual(probe_session, cause("es1-" + "2" * 16))
        self.assertNotEqual(probe_session, cause("es1-" + "1" * 16, error_type="ConfigurationError"))
        self.assertNotEqual(probe_session, cause("es1-" + "1" * 16, failure_kind="unexpected-error"))
        self.assertNotIn(historical, {probe_session, rehearsal})
        # 与第 32 项 staging.attempt-failed 用同一签名口径，但码与组件都不同，不会混成一个根因。
        attempt_failed = root_cause.structured_root_cause(
            component="orchestrator",
            stable_error_code="staging.attempt-failed",
            failed_step="VC-5-candidate-recovery",
            stable_dimensions={
                "phase": "VC-5",
                "stage": "VC-5-candidate-recovery",
                "error_type": "ValueError",
                "error_signature": "es1-" + "1" * 16,
            },
        )
        self.assertNotEqual(probe_session, attempt_failed)
        # 签名维度同样受波动值拒绝：长摘要、绝对路径不能冒充签名。
        for value in ("f" * 64, "/root/docker/capture-cli/data"):
            with self.subTest(value=value), self.assertRaisesRegex(root_cause.RootCauseError, "不是稳定根因输入"):
                cause(value)
        # 维度键必须恰好四个：退化成旧的单维不行；也不能由其它组件生产。
        with self.assertRaisesRegex(root_cause.RootCauseError, "维度键"):
            root_cause.structured_root_cause(
                component="reconciler",
                stable_error_code="campaign-run.action-error",
                failed_step="VC-5-candidate-recovery",
                stable_dimensions={"phase": "VC-5"},
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "不能由"):
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="campaign-run.action-error",
                failed_step="VC-5-candidate-recovery",
                stable_dimensions={
                    "phase": "VC-5",
                    "failure_kind": "handled-error",
                    "error_type": "ValueError",
                    "error_signature": "es1-" + "1" * 16,
                },
            )

    def test_legacy_literals_map_to_structured_ids(self) -> None:
        mapped = root_cause.legacy_root_cause_id("vc1-parent-lease-deadline-missing")
        self.assertTrue(root_cause.is_structured(mapped))
        self.assertEqual(
            mapped,
            root_cause.structured_root_cause(
                component="supervisor",
                stable_error_code="vc1-parent-lease-deadline-missing",
                failed_step="",
                stable_dimensions={},
            ),
        )
        with self.assertRaisesRegex(root_cause.RootCauseError, "历史字面量"):
            root_cause.legacy_root_cause_id("campaign-run.action-failed")

    def test_codes_identity_is_enforced(self) -> None:
        table = root_cause.load_codes()
        self.assertEqual(len(table["codes_sha256"]), 64)
        root_cause.assert_codes_identity(
            expected_codes_sha256=table["codes_sha256"],
            expected_algorithm_version=root_cause.ALGORITHM_VERSION,
        )
        with self.assertRaisesRegex(root_cause.RootCauseError, "枚举表摘要"):
            root_cause.assert_codes_identity(
                expected_codes_sha256="0" * 64,
                expected_algorithm_version=root_cause.ALGORITHM_VERSION,
            )
        with self.assertRaisesRegex(root_cause.RootCauseError, "算法版本"):
            root_cause.assert_codes_identity(
                expected_codes_sha256=table["codes_sha256"],
                expected_algorithm_version="structured-root-cause/v0",
            )

    def test_modified_codes_file_changes_identity_and_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codes.json"
            payload = json.loads(root_cause.DEFAULT_CODES_PATH.read_text("utf-8"))
            payload["codes"]["new.code"] = {
                "component": "reconciler",
                "stable_dimensions": ["phase"],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            table = root_cause.load_codes(path)
            self.assertNotEqual(
                table["codes_sha256"], root_cause.load_codes()["codes_sha256"]
            )
            self.assertTrue(
                root_cause.is_structured(
                    root_cause.structured_root_cause(
                        component="reconciler",
                        stable_error_code="new.code",
                        failed_step="x",
                        stable_dimensions={"phase": "VC-1"},
                        codes=table,
                    )
                )
            )
            payload["codes"]["bad code"] = {"component": "x", "stable_dimensions": []}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(root_cause.RootCauseError, "错误码非法"):
                root_cause.load_codes(path)


if __name__ == "__main__":
    unittest.main()
