"""Claude VC-4 候选构建与冻结、候选 revision（Claude 升级控制面设计 B3-04、B3-05 编排器部分）。

全部离线、零请求。夹具沿用 B3-03 的画像链：VC-2 判 ``SPEC-BODY-001`` 为 change、``SPEC-TOOL-024`` 为 delete，VC-3 准备与
暂存批准之后 Store 到 ``profile_approved``；Campaign 总计划绑定一份 Claude 计时账本并在项目总账注册；候选 revision 由真实的
``revision-open`` 写编排器与 Store 两份登记，作废走真实的 ``invalidate-candidate`` preview／apply。

* 需求预览：门禁需求由 VC-2 分区、VC-3 联合摘要与 VC-2 已批准迁移台账确定性推出，逐字段核对，与直接调用
  ``build_gate_requirements`` 逐字相同；预览零写入。
* 生成计划：映射按预览逐项绑定；计划落盘后与 ``build_gate_plan`` 复算一致、``validate_gate_plan`` 按需求校验通过；输出
  已存在即拒绝。
* 映射与路径：缺项、需求摘要不符、映射或输出在源码树外、源码树与 Campaign 互相包含、参数不成组，一律拒绝且零写入。
* 候选级前置门：没有显式 revision、候选不属于当前 revision、当前 revision 已作废、VC-3 未完成、没有绑定计时账本，
  一律拒绝。
* 候选模块不进 wire／evidence 闭包。
* record-candidate-build：Store 到 candidate_sealed、revision seal、VC-4 checkpoint；硬杀后重跑收敛；零写入预览；VC-4 阶段
  幂等重派合同；r2（作废＋取代）全链：改名候选全同被 seal 预检拒绝且零写入，源码变化的新候选写出 r2 的 seal、取代标记与
  checkpoint。
* revision 成员：两份登记一致、共享流程与 Store 之间被强杀后重跑收敛、Store 作废缺失时 --supersedes 预检拒绝且零写入、
  隐含 r1 一律拒绝、失败父 run 定位逐字照搬 Codex。
"""

from __future__ import annotations

import collections
import inspect
import json
import os
import signal
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_batch
from tools.official_client_capture import claude_upgrade_candidate as candidate
from tools.official_client_capture import claude_upgrade_classify
from tools.official_client_capture import claude_upgrade_profile
from tools.official_client_capture import claude_upgrade_revision
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.tests import claude_candidate_build_fixture as build_fixture
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests import test_claude_upgrade_classify as classify_tests
from tools.official_client_capture.tests import test_claude_upgrade_profile as profile_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
from tools.official_client_control.store import ControlStore

REPOSITORY_ROOT = profile_tests.REPOSITORY_ROOT
CAMPAIGN_ID = profile_tests.CAMPAIGN_ID
CANDIDATE = "claude-cand-r1"
OTHER = "claude-cand-other"
PUBLIC_GATES = sorted(gate_id for gate_id, _semantic in vc_artifacts.PUBLIC_GATES)


def run_main(argv: list[str]) -> tuple[int, dict | None, str]:
    return classify_tests.run_main(argv)


def tree_digests(root: Path) -> dict[str, str]:
    """目录下全部普通文件的相对路径 → 摘要（判零写入用）。"""

    return {path.relative_to(root).as_posix(): claude_upgrade.file_sha256(path) for path in sorted(root.rglob("*")) if path.is_file()}


class _CandidateCase(profile_tests._ProfileCase):
    """在 B3-03 夹具上走完 VC-3 暂存批准，总计划绑定 Claude 计时账本并在项目总账注册；候选 revision 走真实的 revision-open。"""

    bind_ledger = True

    def build_campaign(self, **kwargs: Any) -> Path:
        if self.bind_ledger and "control_receipts" not in kwargs:
            ledger = self.root / "timing-ledger"
            timing.create_ledger(
                ledger,
                upgrade_id=CAMPAIGN_ID,
                baseline_version=classify_tests.BASELINE,
                target_version=classify_tests.TARGET,
                campaign_purpose="validation_only",
                evidence_decision="recapture",
                client="claude",
            )
            kwargs["control_receipts"] = {
                "upgrade_timing": {"ledger_dir": str(ledger.resolve()), "ledger_plan_sha256": claude_upgrade.file_sha256(ledger / "ledger.json")}
            }
        # VC-1 取只读复用前序证据的形态（本 Campaign 没有官方 attempt）：作废的二次判定会重放全部 attempt 的原子预约收据，
        # 采集形态的夹具 attempt 没有预约收据（与 B3-01 批次链同一做法）。
        kwargs.setdefault("official_reuse", True)
        campaign = super().build_campaign(**kwargs)
        # revision-open 与 invalidate-candidate 先过项目总账准入（Claude 的 Formal Campaign 一律在总账内执行）；总账要求控制
        # 目录私有，推送对账要求当前工具身份有通过的受管部署收据。
        for directory in (campaign / "control", campaign / "control" / "vc"):
            if directory.is_dir():
                directory.chmod(0o700)
        self.project_root = ledger_fixture.register_claude_campaign(campaign)
        ledger_fixture.write_claude_deploy_receipt(self.data, self.tool_identity, stamp="20261007t000000z")
        return campaign

    def staged(self) -> Path:
        """走到 VC-3 暂存批准完成（Store profile_approved、VC-3 checkpoint 已写）。"""

        campaign, _approved = self.prepared()
        code, preview, stderr = run_main(["stage-profile", "--campaign-dir", str(campaign)])
        self.assertEqual(code, 2, stderr)
        code, _staged, stderr = run_main(
            ["stage-profile", "--campaign-dir", str(campaign), "--approve-joint-sha256", preview["joint_sha256"], "--approved-by", "reviewer-a"]
        )
        self.assertEqual(code, 0, stderr)
        if self.bind_ledger:
            # 计时账本按批次入口的口径登记 VC-0～VC-3 完成（夹具不经批次派发）：进入 VC-4 时没有进行中的阶段。
            ledger = self.root / "timing-ledger"
            for phase in ("VC-0", "VC-1", "VC-2", "VC-3"):
                if phase != "VC-0":
                    timing.append_event(ledger, event_id=f"fixture-{phase.lower()}-started", phase=phase, event_type="stage_started", next_action=f"{phase} 批次", receipts=[])
                timing.append_event(ledger, event_id=f"fixture-{phase.lower()}-completed", phase=phase, event_type="stage_completed", next_action=f"{phase} 完成", receipts=[])
        return campaign

    def session(self, campaign: Path) -> claude_upgrade_seal._StoreSession:
        return claude_upgrade_seal._StoreSession(claude_upgrade_store.campaign_store_root(campaign), CAMPAIGN_ID, self.root / "store-inputs")

    def revision_open_argv(self, campaign: Path, candidate_id: str = CANDIDATE, supersedes: str | None = None) -> list[str]:
        mode = ["--initial"] if supersedes is None else ["--supersedes", supersedes]
        return ["revision-open", "--campaign-dir", str(campaign), "--candidate-id", candidate_id, *mode]

    def open_revision(self, campaign: Path, candidate_id: str = CANDIDATE, supersedes: str | None = None) -> dict[str, Any]:
        """真实的 revision-open（--initial 或 --supersedes）：编排器侧与 Store 侧两份登记；返回命令结果。"""

        code, result, stderr = run_main(self.revision_open_argv(campaign, candidate_id, supersedes))
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (0, "complete"), stderr)
        return result

    def invalidate_argv(self, campaign: Path, action: str, candidate_id: str, *extra: str) -> list[str]:
        return ["invalidate-candidate", action, "--campaign-dir", str(campaign), "--candidate-id", candidate_id, "--reviewer", "reviewer-a", *extra]

    def invalidate(self, campaign: Path, candidate_id: str, *, candidate_source: Path | None = None) -> dict[str, Any]:
        """真实的 invalidate-candidate preview → apply（同一批准摘要）；返回 apply 结果。"""

        extra = ["--candidate-source", str(candidate_source)] if candidate_source is not None else []
        code, preview, stderr = run_main(self.invalidate_argv(campaign, "preview", candidate_id, *extra))
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["status"]), (0, "preview"), stderr)
        self.assertEqual(preview["store_invalidation"]["written_on"], "apply")
        code, applied, stderr = run_main(self.invalidate_argv(campaign, "apply", candidate_id, *extra, "--approve-sha256", preview["review_sha256"]))
        self.assertIsNotNone(applied, stderr)
        self.assertEqual((code, applied["status"]), (0, "applied"), stderr)
        return applied

    def preview_argv(self, campaign: Path, candidate_id: str = CANDIDATE) -> list[str]:
        return ["plan-candidate-gates", "--campaign-dir", str(campaign), "--candidate-id", candidate_id]

    def source_tree(self) -> Path:
        source = self.root / "candidate-source"
        (source / "backend").mkdir(parents=True)
        (source / "docs").mkdir()
        return source

    @staticmethod
    def mapping_for(preview: dict[str, Any]) -> dict[str, Any]:
        """按需求预览逐项绑定的映射：每项一个唯一测试与字面命令。"""

        gates = [
            {
                "gate_id": row["gate_id"],
                "test_id": f"test-{row['gate_id']}",
                "working_directory": "backend" if row["kind"] == "affected_rule" else ".",
                "command": ["go", "test", "./internal/officialegress", "-run", f"TestGate{index:02d}"],
                "requirement_sha256": preview["requirement_sha256s"][row["gate_id"]],
            }
            for index, row in enumerate(preview["requirements"]["requirements"])
        ]
        return {"schema_version": vc_artifacts.GATE_MAPPING_SCHEMA, "requirements_sha256": preview["requirements_sha256"], "gates": gates}

    def write_mapping(self, source: Path, mapping: dict[str, Any], name: str = "gate-mapping.json") -> Path:
        path = source / "docs" / name
        path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def plan_argv(self, campaign: Path, source: Path, mapping: Path, output: Path, candidate_id: str = CANDIDATE) -> list[str]:
        return [
            *self.preview_argv(campaign, candidate_id),
            "--candidate-source", str(source), "--mapping", str(mapping), "--output", str(output),
        ]

    def ready(self) -> tuple[Path, dict[str, Any]]:
        """暂存批准、显式 r1 之后的需求预览。"""

        campaign = self.staged()
        self.open_revision(campaign)
        code, preview, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual((code, preview["status"]), (2, "awaiting_gate_mapping"), stderr)
        return campaign, preview


class GateRequirementTests(_CandidateCase):
    """门禁需求的确定性推出与需求预览。"""

    def test_requirements_are_derived_from_vc2_and_vc3(self) -> None:
        campaign = self.staged()
        self.open_revision(campaign)
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        classification = claude_upgrade_classify.load_stage_result(campaign, manifest)
        staged = claude_upgrade_profile.load_stage_result(campaign, manifest)
        store_root = claude_upgrade_store.campaign_store_root(campaign)
        before = (tree_digests(campaign), tree_digests(store_root))
        code, preview, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual((code, preview["mode"], preview["candidate_revision"]), (2, "gate_requirements", 1), stderr)
        self.assertEqual((tree_digests(campaign), tree_digests(store_root)), before)

        requirements = preview["requirements"]
        migration = classification["approved"]["rule_migration"]
        self.assertEqual(requirements["schema_version"], vc_artifacts.GATE_REQUIREMENTS_SCHEMA)
        self.assertEqual((requirements["campaign_id"], requirements["target_version"]), (CAMPAIGN_ID, classify_tests.TARGET))
        self.assertEqual(requirements["joint_manifest_sha256"], staged["joint_sha256"])
        self.assertEqual(requirements["migration_manifest"], {"path": migration["path"], "sha256": migration["sha256"]})
        self.assertEqual(migration["path"], "classification/approved/rule-migration.json")
        self.assertEqual(requirements["affected_rule_ids"], sorted([profile_tests.CHANGE, profile_tests.DELETE]))
        self.assertEqual(requirements["inherited_rule_ids"], sorted(classification["inherited_rules"]))
        self.assertEqual(len(requirements["affected_rule_ids"]) + len(requirements["inherited_rule_ids"]), 40)
        gate_ids = [row["gate_id"] for row in requirements["requirements"]]
        self.assertEqual(gate_ids, sorted(["affected-spec-body-001", "affected-spec-tool-024", *PUBLIC_GATES]))
        self.assertEqual(preview["requirement_sha256s"], {row["gate_id"]: vc_artifacts.digest(row) for row in requirements["requirements"]})
        # 与直接调用 build_gate_requirements 逐字相同；重跑得到同一份需求。
        direct = vc_artifacts.build_gate_requirements(
            campaign_id=CAMPAIGN_ID,
            target_version=classify_tests.TARGET,
            joint_manifest_sha256=staged["joint_sha256"],
            affected_rule_ids=classification["affected_rules"],
            inherited_rule_ids=classification["inherited_rules"],
            migration_manifest={"path": migration["path"], "sha256": migration["sha256"]},
        )
        self.assertEqual(requirements, direct)
        self.assertEqual(run_main(self.preview_argv(campaign))[1]["requirements"], requirements)


class GatePlanTests(_CandidateCase):
    """生成门禁执行计划：复算一致、只写一次、映射与路径的拒绝。"""

    def test_gate_plan_recomputes_and_is_written_once(self) -> None:
        campaign, preview = self.ready()
        source = self.source_tree()
        mapping = self.mapping_for(preview)
        mapping_path = self.write_mapping(source, mapping)
        output = source / "docs" / "gate-plan.json"
        code, result, stderr = run_main(self.plan_argv(campaign, source, mapping_path, output))
        self.assertEqual((code, result["status"], result["gate_count"]), (0, "complete", 6), stderr)
        stored = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], vc_artifacts.GATE_PLAN_SCHEMA)
        # 复算：同一需求与映射原样交给 build_gate_plan 得到逐字相同的计划；validate_gate_plan 按需求校验通过。
        recomputed = vc_artifacts.build_gate_plan(
            preview["requirements"], mapping, mapping_sha256=claude_upgrade.file_sha256(mapping_path)
        )
        self.assertEqual(stored, recomputed)
        self.assertEqual(vc_artifacts.validate_gate_plan(stored, preview["requirements"]), stored)
        self.assertEqual(
            (result["plan_sha256"], result["requirements_sha256"], result["mapping_sha256"]),
            (stored["plan_sha256"], preview["requirements_sha256"], claude_upgrade.file_sha256(mapping_path)),
        )
        # 输出已存在即拒绝覆盖（与 Codex 同口径），文件不变。
        before = claude_upgrade.file_sha256(output)
        code, _result, stderr = run_main(self.plan_argv(campaign, source, mapping_path, output))
        self.assertEqual(code, 1)
        self.assertIn("已存在", stderr)
        self.assertEqual(claude_upgrade.file_sha256(output), before)

    def test_mapping_and_paths_are_rejected_without_writes(self) -> None:
        campaign, preview = self.ready()
        source = self.source_tree()
        good = self.mapping_for(preview)
        output = source / "docs" / "gate-plan.json"

        missing = dict(good, gates=good["gates"][1:])
        stale = dict(good, requirements_sha256="0" * 64)
        cases = {
            "缺项": (self.write_mapping(source, missing, "missing.json"), output, "未精确覆盖"),
            "需求摘要不符": (self.write_mapping(source, stale, "stale.json"), output, "未绑定本轮 VC-3 需求"),
        }
        outside = self.root / "outside-mapping.json"
        outside.write_text(json.dumps(good), encoding="utf-8")
        cases["映射在源码树外"] = (outside, output, "不在候选源码树内")
        good_path = self.write_mapping(source, good)
        cases["输出在源码树外"] = (good_path, self.root / "gate-plan.json", "不在候选源码树内")
        for label, (mapping_path, target, message) in cases.items():
            with self.subTest(label=label):
                code, _result, stderr = run_main(self.plan_argv(campaign, source, mapping_path, target))
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
                self.assertFalse(target.exists())
        # 参数不成组；源码树包含 Campaign。
        code, _result, stderr = run_main([*self.preview_argv(campaign), "--candidate-source", str(source)])
        self.assertEqual(code, 1)
        self.assertIn("同时给出", stderr)
        code, _result, stderr = run_main(self.plan_argv(campaign, self.root, good_path, self.root / "plan.json"))
        self.assertEqual(code, 1)
        self.assertIn("彼此独立", stderr)


class CandidateGateTests(_CandidateCase):
    """候选级前置门：两份 revision 登记、作废与取代、VC-3 与计时账本。"""

    def test_candidate_revision_gates(self) -> None:
        campaign = self.staged()
        code, _result, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("revision-open --initial", stderr)
        opened = self.open_revision(campaign, OTHER)
        self.assertEqual((opened["revision"], opened["store"]["payload"]["revision"], opened["store"]["reused"]), (1, 1, False))
        code, _result, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("不属于当前 revision r1", stderr)
        # 作废 OTHER（没有构建收据：身份快照取 --candidate-source）：账本 revision_required，Store 写作废对象。
        applied = self.invalidate(campaign, OTHER, candidate_source=self.source_tree())
        self.assertEqual(applied["store"]["reused"], False)
        self.assertEqual(timing.inspect_ledger(self.root / "timing-ledger")["status"], "revision_required")
        store = ControlStore(claude_upgrade_store.campaign_store_root(campaign))
        invalidation = store.load_object(applied["store"]["candidate_invalidation_ref"])["payload"]
        self.assertEqual((invalidation["candidate_id"], invalidation["revision"], invalidation["approved_by"]), (OTHER, 1, "reviewer-a"))
        self.assertEqual(invalidation["revision_ref"], opened["store"]["candidate_revision_fact_ref"])
        code, _result, stderr = run_main(self.preview_argv(campaign, OTHER))
        self.assertEqual(code, 1)
        self.assertIn("已作废", stderr)
        # 以同一批准摘要重跑 apply：共享流程幂等，Store 作废对象复用。
        code, again, stderr = run_main(
            self.invalidate_argv(
                campaign, "apply", OTHER, "--candidate-source", str(self.root / "candidate-source"),
                "--approve-sha256", self.review_of(campaign, OTHER),
            )
        )
        self.assertEqual((code, again["status"], again["idempotent"], again["store"]["reused"]), (0, "applied", True, True), stderr)
        # 取代：r2 以新候选登记，Store 事实引用作废对象；新候选的需求预览可用。
        superseding = self.open_revision(campaign, CANDIDATE, supersedes=OTHER)
        payload = superseding["store"]["payload"]
        self.assertEqual((superseding["revision"], payload["revision"], payload["invalidation_ref"]), (2, 2, applied["store"]["candidate_invalidation_ref"]))
        self.assertEqual(payload["supersedes"], opened["store"]["candidate_revision_fact_ref"])
        code, preview, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual((code, preview["candidate_revision"]), (2, 2), stderr)
        # revision-open 重跑幂等（两份登记都不增加）。
        again = self.open_revision(campaign, CANDIDATE, supersedes=OTHER)
        self.assertEqual((again["idempotent"], again["store"]["reused"]), (True, True))
        self.assertEqual(seal_tests.facts_by_kind(claude_upgrade_store.campaign_store_root(campaign), CAMPAIGN_ID)["candidate_revision_recorded"], 2)

    def review_of(self, campaign: Path, candidate_id: str) -> str:
        """已落盘作废记录的诊断审核摘要（apply 重跑用的批准摘要）。"""

        record = json.loads((campaign / "candidates" / candidate_id / "invalidation.json").read_text(encoding="utf-8"))
        return record["diagnosis"]["review_sha256"]

    def test_requires_completed_vc3(self) -> None:
        campaign, _approved = self.prepared()
        code, _result, stderr = run_main(self.revision_open_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("VC-3", stderr)
        code, _result, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("VC-3", stderr)

    def test_requires_bound_timing_ledger(self) -> None:
        self.bind_ledger = False
        campaign = self.staged()
        code, _result, stderr = run_main(self.revision_open_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("UpgradeTimingLedger", stderr)
        self.assertFalse(claude_upgrade._candidate_revisions_root(campaign).exists())

    def test_requires_active_timing_ledger(self) -> None:
        campaign = self.staged()
        self.open_revision(campaign)
        with mock.patch.object(candidate.codex_upgrade_timing_ledger, "inspect_ledger", return_value={"status": "stage_review_required"}):
            code, _result, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("UpgradeTimingLedger 为 active（当前 stage_review_required）", stderr)

    def test_candidate_module_stays_outside_wire_and_evidence_closures(self) -> None:
        policy = claude_identity.load_policy()
        digests = claude_identity.managed_tree_digests(REPOSITORY_ROOT, policy)
        index = claude_identity._module_index(policy, digests)
        module = "tools.official_client_capture.claude_upgrade_candidate"
        for layer, roots in (("wire_producer", "wire_roots"), ("evidence_semantics", "evidence_roots")):
            closure = claude_identity.closure(
                policy, REPOSITORY_ROOT, digests, claude_identity._orchestrator_roots(policy, index, policy["orchestrator"][roots]), layer=layer
            )
            with self.subTest(layer=layer):
                self.assertNotIn(module, {item["module"] for item in closure["symbols"]})
                self.assertNotIn(module, {item["module"] for item in closure["modules"]})
        self.assertEqual(claude_identity.classify_path(policy, "official_client_capture/claude_upgrade_candidate.py"), "control")

    def test_service_surface_path_member_delegates(self) -> None:
        # 服务面 _path_in_candidate_source（共享的阶段重派证明用它核对树内映射）转调本模块：树内文件通过，符号链接与树外拒绝。
        source = self.source_tree()
        inside = source / "docs" / "gate-mapping.json"
        inside.write_text("{}", encoding="utf-8")
        self.assertEqual(claude_upgrade._path_in_candidate_source(inside, source, "门禁映射"), inside.resolve())
        link = source / "docs" / "link.json"
        link.symlink_to(inside)
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        for path in (link, outside):
            with self.subTest(path=path.name), self.assertRaises(claude_upgrade.ConfigurationError):
                claude_upgrade._path_in_candidate_source(path, source, "门禁映射")
        self.assertEqual(candidate.PLAN_GATES_ACTION, "plan-candidate-gates")


class RecordCandidateBuildTests(_CandidateCase):
    """record-candidate-build：同源构建复算、候选构建收据、Store CandidateBuildReceipt 与 candidate_frozen（到 candidate_sealed）。

    构建结果用合成夹具（``claude_candidate_build_fixture``）：本地 git 候选仓库、Codex 严格构建合同的四棵树与二进制、
    可从 Git 两端复算的 source transition、``codex_upgrade_vc_receipt.finalize`` 签发的实现测试收据，docker／go 命令走替身。
    """

    def fixture(self) -> tuple[Path, build_fixture.CandidateBuildFixture]:
        campaign, preview = self.ready()
        fixture = build_fixture.CandidateBuildFixture(self.root / "build", campaign, CANDIDATE, run_main=run_main)
        fixture.build_source(preview=preview)
        fixture.build_artifacts()
        fixture.build_transition()
        fixture.build_implementation_receipt(preview["requirements"], CAMPAIGN_ID)
        return campaign, fixture

    def record(self, fixture: build_fixture.CandidateBuildFixture, **overrides: str) -> tuple[int, dict | None, str]:
        with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()):
            return run_main(fixture.argv(**overrides))

    def test_record_build_reaches_candidate_sealed_and_converges(self) -> None:
        campaign, fixture = self.fixture()
        store_root = claude_upgrade_store.campaign_store_root(campaign)
        # 在 candidate-build-finalize 之后被硬杀：Store 收据已在，冻结事实与编排器收据都未写；重跑收敛。
        with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()):
            status = seal_tests.run_killed(fixture.argv(), kill_after="candidate-build-finalize")
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        self.assertEqual(len(ControlStore(store_root).list_receipt_refs("candidate_build")), 1)
        self.assertNotIn("candidate_frozen", seal_tests.facts_by_kind(store_root, CAMPAIGN_ID))
        receipt_path = campaign / "candidates" / CANDIDATE / candidate.CANDIDATE_BUILD_RECEIPT_FILENAME
        self.assertFalse(receipt_path.exists())
        # 在 candidate-freeze 之后被硬杀：Store 已到 candidate_sealed，编排器收据未写；重跑收敛。
        with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()):
            status = seal_tests.run_killed(fixture.argv(), kill_after="candidate-freeze")
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "candidate_sealed")
        self.assertFalse(receipt_path.exists())
        code, result, stderr = self.record(fixture)
        self.assertEqual((code, result["status"]), (0, "complete"), stderr)
        self.assertTrue(result["store"]["candidate_build_receipt_reused"])
        self.assertTrue(result["store"]["candidate_frozen_reused"])
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "candidate_sealed")
        self.assertEqual(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)["candidate_frozen"], 1)
        self.assertEqual(len(ControlStore(store_root).list_receipt_refs("candidate_build")), 1)

        receipt = vc_artifacts.validate_official_client_candidate_build_receipt(json.loads(receipt_path.read_text(encoding="utf-8")))
        self.assertEqual((receipt["client"], receipt["candidate_revision"], receipt["candidate_id"]), ("claude", 1, CANDIDATE))
        self.assertEqual(receipt["source"]["git_commit"], fixture.commit)
        self.assertEqual(receipt["gate_plan"]["plan_sha256"], fixture.gate_plan["plan_sha256"])
        store = ControlStore(store_root)
        store_receipt = store.load_receipt(receipt["store"]["candidate_build_receipt_ref"])
        self.assertEqual(store_receipt["source_identity"]["source_tree_sha256"], receipt["source"]["tree_sha256"])
        self.assertEqual(store_receipt["profile_identity"]["model_capability_catalog_ref"], receipt["store"]["model_capability_catalog_ref"])
        self.assertEqual(store_receipt["validation_scope_sha256"], receipt["validation_scope_sha256"])
        self.assertEqual(store_receipt["image"]["image_digest"], "sha256:" + "7" * 64)
        self.assertEqual(
            [item["binding"]["path"] for item in store_receipt["artifact_inventory"]["entries"]],
            [f"evidence/campaigns/{CAMPAIGN_ID}/candidates/{CANDIDATE}/build-evidence/artifacts/{name}"
             for name in ("container-healthcheck.sh", "docker-entrypoint.sh", "sub2api")],
        )
        for entry in store_receipt["artifact_inventory"]["entries"]:
            store._verify_external_binding(self.data, entry["binding"])
            self.assertEqual((self.data / entry["binding"]["path"]).stat().st_mode & 0o777, 0o755)
        frozen = next(fact for fact in store.list_facts(CAMPAIGN_ID) if fact["fact_kind"] == "candidate_frozen")
        self.assertEqual(frozen["payload"]["candidate_build_receipt_ref"], receipt["store"]["candidate_build_receipt_ref"])
        self.assertEqual(store.fact_ref(frozen), receipt["store"]["candidate_frozen_ref"])
        staged = claude_upgrade_profile.load_stage_result(campaign, claude_upgrade.load_campaign_manifest(campaign))
        self.assertEqual(receipt["profile"]["release_sha256"], staged["staged_release"]["release_sha256"])
        # revision seal（显式 r1 也写）：绑定构建收据去掉签发时刻与自摘要的摘要、VC-3 阶段收据；r1 没有被取代候选。
        seal = vc_artifacts.validate_candidate_revision_seal(json.loads((claude_upgrade._candidate_revision_dir(campaign, 1) / "seal.json").read_text(encoding="utf-8")))
        self.assertEqual((seal["revision"], seal["candidate_id"], seal["superseded"], seal["candidate_commit"]), (1, CANDIDATE, None, fixture.commit))
        self.assertEqual(
            seal["build_receipt_sha256"],
            vc_artifacts.digest({key: value for key, value in receipt.items() if key not in {"built_at_utc", "receipt_digest"}}),
        )
        self.assertEqual(result["revision_seal"]["seal_sha256"], seal["seal_sha256"])
        # VC-4 checkpoint：r1 在原路径，阶段收据是构建收据、直接前序是 Campaign 级 VC-3，继承规则记为复用项。
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        plan = claude_upgrade_vc.vc_campaign_plan(campaign, manifest)
        checkpoint_path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(campaign, plan, "VC-4", revision=1)
        self.assertEqual(checkpoint_path, campaign / "control" / "vc" / "vc-4-checkpoint.json")
        self.assertEqual(checkpoint["stage_receipt"], {"path": receipt_path.relative_to(campaign).as_posix(), "sha256": claude_upgrade.file_sha256(receipt_path)})
        self.assertEqual((checkpoint["completed_at_utc"], checkpoint["execute_item_ids"]), (receipt["built_at_utc"], []))
        self.assertEqual(checkpoint["reuse_item_ids"], sorted(staged["inherited_rules"]))
        self.assertEqual(checkpoint["metrics"], {"live_request_count": 0, "scanned_bytes": 0})
        self.assertEqual(result["vc4_checkpoint"]["checkpoint_sha256"], checkpoint["checkpoint_sha256"])
        # 再跑逐字复用：收据只有签发时刻与自摘要可变，Store 不再增加。
        before = {path: path.read_bytes() for path in (receipt_path, checkpoint_path, claude_upgrade._candidate_revision_dir(campaign, 1) / "seal.json")}
        code, again, stderr = self.record(fixture)
        self.assertEqual((code, again["receipt_digest"]), (0, receipt["receipt_digest"]), stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(
            seal_tests.facts_by_kind(store_root, CAMPAIGN_ID),
            collections.Counter(
                {
                    "discovery_recorded": 1, "evidence_recorded": 1, "rule_classification_recorded": 1, "evidence_approved": 1,
                    "profile_approved": 1, "candidate_revision_recorded": 1, "candidate_frozen": 1,
                }
            ),
        )

    def test_record_build_rejects_wrong_identity_and_inputs(self) -> None:
        campaign, fixture = self.fixture()
        cases = {
            "用途不符": ({"--candidate-purpose": "production_replacement"}, "冻结用途"),
            "架构不符": ({"--target-architecture": "linux/amd64"}, "Campaign 平台"),
            "版本不符": ({"--deployed-version": "9.9.9"}, "目标版本"),
            "镜像引用可变": ({"--runtime-image": "registry.local/sub2api:latest"}, "repository@sha256"),
            "门禁计划在树外": ({"--gate-plan": str(self.root / "gate-plan.json")}, "不在候选源码树内"),
        }
        (self.root / "gate-plan.json").write_text("{}", encoding="utf-8")
        for label, (overrides, message) in cases.items():
            with self.subTest(label=label):
                code, _result, stderr = self.record(fixture, **overrides)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
        # 别的候选签发的实现测试收据不能借用。
        other_root = self.root / "other-implementation"
        other = build_fixture.CandidateBuildFixture(self.root / "build", campaign, OTHER, run_main=run_main)
        other.implementation_root = other_root
        other.commit, other.source = fixture.commit, fixture.source
        other.build_implementation_receipt(run_main(self.preview_argv(campaign))[1]["requirements"], CAMPAIGN_ID)
        code, _result, stderr = self.record(
            fixture, **{"--implementation-test-root": str(other_root), "--implementation-test-receipt": str(other_root / "receipt.json")}
        )
        self.assertEqual(code, 1)
        self.assertIn("实现测试收据未绑定", stderr)
        store_root = claude_upgrade_store.campaign_store_root(campaign)
        self.assertNotIn("candidate_frozen", seal_tests.facts_by_kind(store_root, CAMPAIGN_ID))
        self.assertEqual(ControlStore(store_root).list_receipt_refs("candidate_build"), [])

    def test_candidate_catalog_must_be_the_staged_release(self) -> None:
        campaign, fixture = self.fixture()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        staged = claude_upgrade_profile.load_stage_result(campaign, manifest)
        result = candidate.verify_candidate_catalog(campaign, manifest, staged, fixture.source.resolve(), fixture.stage_dir)
        self.assertEqual(result["entry"]["release_sha256"], staged["staged_release"]["release_sha256"])
        catalog_path = fixture.source / candidate.RELEASE_CATALOG_RELATIVE
        original = catalog_path.read_bytes()
        wire_path = fixture.source / "backend" / "internal" / "officialegress" / result["entry"]["wire"]["path"]
        wire_original = wire_path.read_bytes()

        def change_active(document: dict[str, Any]) -> None:
            document["selectors"]["production_active"]["changeset"] = "tampered"

        def drop_candidate_selector(document: dict[str, Any]) -> None:
            document["selectors"]["validation_candidate"] = document["selectors"]["production_active"]

        for label, mutate in (("改了 Active 选择器", change_active), ("候选选择器不指向暂存 Release", drop_candidate_selector)):
            with self.subTest(label=label):
                document = json.loads(original)
                mutate(document)
                catalog_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "release-catalog.json"):
                    candidate.verify_candidate_catalog(campaign, manifest, staged, fixture.source.resolve(), fixture.stage_dir)
        catalog_path.write_bytes(original)
        wire_path.write_bytes(wire_original + b" ")
        with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "字节不一致"):
            candidate.verify_candidate_catalog(campaign, manifest, staged, fixture.source.resolve(), fixture.stage_dir)
        wire_path.write_bytes(wire_original)
        (fixture.stage_dir / "extra.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "两个普通文件"):
            candidate.verify_candidate_catalog(campaign, manifest, staged, fixture.source.resolve(), fixture.stage_dir)

    def campaign_digests(self, campaign: Path) -> dict[str, str]:
        """Campaign 写域（候选目录与 VC 控制制品）与 Store 的全部文件摘要（判零写入用）。"""

        roots = (campaign / "candidates", campaign / "control" / "vc", claude_upgrade_store.campaign_store_root(campaign))
        return {f"{root.name}/{key}": value for root in roots if root.is_dir() for key, value in tree_digests(root).items()}

    def test_preview_is_zero_write_and_matches_the_real_run(self) -> None:
        campaign, fixture = self.fixture()
        before = self.campaign_digests(campaign)
        code, preview, stderr = self.record_preview(fixture)
        self.assertEqual((code, preview["status"], preview["mode"]), (2, "preview", "record_build_preview"), stderr)
        self.assertEqual(self.campaign_digests(campaign), before)
        self.assertIn("control/vc/vc-4-checkpoint.json", preview["writes"])
        self.assertIn("control/vc/revisions/r1/seal.json", preview["writes"])
        self.assertEqual(preview["revision_seal"], {"changed_layers": [], "superseded": None})
        # 父监督器上下文里拒绝预览（不是批次动作）。
        with mock.patch.dict(os.environ, {candidate.codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}):
            code, _result, stderr = self.record_preview(fixture)
        self.assertEqual(code, 1)
        self.assertIn("不能由 campaign-run 动作派发", stderr)
        code, result, stderr = self.record(fixture)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["store"]["candidate_build_receipt_ref"], preview["store"]["candidate_build_receipt_ref"])
        store = ControlStore(claude_upgrade_store.campaign_store_root(campaign))
        frozen = next(fact for fact in store.list_facts(CAMPAIGN_ID) if fact["fact_kind"] == "candidate_frozen")
        self.assertEqual(frozen["payload"]["identity_sha256"], preview["store"]["candidate_identity_sha256"])

    def record_preview(self, fixture: build_fixture.CandidateBuildFixture) -> tuple[int, dict | None, str]:
        with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()):
            return run_main([*fixture.argv(), "--preview"])

    def replay_facts(self, campaign: Path, *actions: dict[str, Any], revision: int = 1, candidate_id: str = CANDIDATE) -> dict[str, Any]:
        manifest = {"phase": "VC-4", "candidate_id": candidate_id, "candidate_revision": revision, "actions": list(actions)}
        return claude_upgrade._campaign_stage_replay_facts(campaign, manifest)

    def test_vc4_stage_replay_contract(self) -> None:
        campaign, fixture = self.fixture()
        record = claude_upgrade_batch.managed_action("vc4-record", "VC-4:record-candidate-build", fixture.argv(), item_ids=["vc4-record"])
        # 构建开始前：可逐字重派，构建输入按摘要绑定，还没有输出。
        facts = self.replay_facts(campaign, record)
        self.assertTrue(facts["allowed"], facts)
        (action,) = facts["actions"]
        self.assertEqual(set(action["inputs"]), {
            "--candidate-binary", "--build-parameters", "--source-transition", "--implementation-test-receipt", "--gate-plan",
            "--candidate-source", "--build-tree", "--docker-context", "--frontend-dist-source", "--catalog-stage-dir",
            "--implementation-test-root",
        })
        self.assertEqual(action["outputs"], {})
        # candidate-freeze 之后被硬杀：Store 已冻结、编排器收据未写——仍可逐字重派，Store 侧事实进 outputs。
        with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()):
            status = seal_tests.run_killed(fixture.argv(), kill_after="candidate-freeze")
        self.assertTrue(os.WIFSIGNALED(status), status)
        facts = self.replay_facts(campaign, record)
        self.assertTrue(facts["allowed"], facts)
        outputs = facts["actions"][0]["outputs"]
        self.assertIn("control-store/candidate_frozen", outputs)
        self.assertIn("control-store/candidate_build", outputs)
        self.assertIn(f"candidates/{CANDIDATE}/build-evidence/image-inspection.json", outputs)
        self.assertNotIn("checkpoint", outputs)
        # 换一个构建标识：Store 已冻结的是另一次构建，逐字重派必然被拒。
        other_build = claude_upgrade_batch.managed_action("vc4-record", "VC-4:record-candidate-build", fixture.argv(**{"--build-id": "claude-build-0002"}), item_ids=["vc4-record"])
        refused = self.replay_facts(campaign, other_build)
        self.assertFalse(refused["allowed"])
        self.assertIn("另一次构建", refused["reasons"][0])
        # 重跑收敛之后：checkpoint 重放并绑定。
        code, _result, stderr = self.record(fixture)
        self.assertEqual(code, 0, stderr)
        facts = self.replay_facts(campaign, record)
        self.assertTrue(facts["allowed"], facts)
        self.assertEqual(facts["actions"][0]["outputs"]["checkpoint"], claude_upgrade.file_sha256(campaign / "control" / "vc" / "vc-4-checkpoint.json"))
        self.assertIn("control/vc/revisions/r1/seal.json", facts["actions"][0]["outputs"])
        # plan-candidate-gates：输出已存在即拒绝；形态不闭合（预览旗标、watchdog 参数）没有合同。
        plan = claude_upgrade_batch.managed_action(
            "vc4-plan", "VC-4:plan-candidate-gates",
            ["plan-candidate-gates", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--candidate-source", fixture.source,
             "--mapping", fixture.mapping_path, "--output", fixture.plan_path],
            item_ids=["vc4-plan"],
        )
        refused = self.replay_facts(campaign, plan)
        self.assertFalse(refused["allowed"])
        self.assertIn("已存在", refused["reasons"][0])
        for label, argv in (
            ("预览", [*fixture.argv(), "--preview", "x"]),
            ("watchdog", [*fixture.argv(), "--heartbeat-seconds", "30"]),
            ("需求预览", ["plan-candidate-gates", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE]),
        ):
            with self.subTest(label=label):
                result = self.replay_facts(campaign, claude_upgrade_batch.managed_action("x", "VC-4:x", argv, item_ids=["x"]))
                self.assertFalse(result["allowed"])
                self.assertIn("没有精确闭合", result["reasons"][0])
        # 不属于当前 revision 的候选。
        result = self.replay_facts(campaign, record, candidate_id=OTHER)
        self.assertFalse(result["allowed"])

    def test_superseding_revision_requires_a_new_candidate(self) -> None:
        campaign, first = self.fixture()
        code, _result, stderr = self.record(first)
        self.assertEqual(code, 0, stderr)
        # 作废 r1 的候选（有构建收据：身份快照取构建收据），以 OTHER 开 r2。
        self.invalidate(campaign, CANDIDATE)
        self.open_revision(campaign, OTHER, supersedes=CANDIDATE)
        store_root = claude_upgrade_store.campaign_store_root(campaign)
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "profile_approved")
        code, preview, stderr = run_main(self.preview_argv(campaign, OTHER))
        self.assertEqual((code, preview["candidate_revision"]), (2, 2), stderr)
        # 改名候选：同一源码提交、同一构建实物，只换候选名——seal 预检拒绝，Store 与编排器零写入。
        renamed = build_fixture.CandidateBuildFixture(self.root / "renamed", campaign, OTHER, run_main=run_main)
        renamed.share_build_of(first)
        renamed.build_implementation_receipt(preview["requirements"], CAMPAIGN_ID)
        before = self.campaign_digests(campaign)
        code, _result, stderr = self.record(renamed)
        self.assertEqual(code, 1)
        self.assertIn("不是新候选", stderr)
        self.assertEqual(self.campaign_digests(campaign), before)
        # 源码变化的新候选：r2 的 seal 绑定被取代候选身份，写取代标记与 r2 的 VC-4 checkpoint。
        second = build_fixture.CandidateBuildFixture(self.root / "second", campaign, OTHER, run_main=run_main)
        second.build_source(preview=preview, marker="r2")
        second.build_artifacts()
        second.build_transition()
        second.build_implementation_receipt(preview["requirements"], CAMPAIGN_ID)
        code, result, stderr = self.record(second)
        self.assertEqual((code, result["candidate_revision"]), (0, 2), stderr)
        revision_dir = claude_upgrade._candidate_revision_dir(campaign, 2)
        seal = vc_artifacts.validate_candidate_revision_seal(json.loads((revision_dir / "seal.json").read_text(encoding="utf-8")))
        self.assertEqual((seal["superseded"]["candidate_id"], seal["superseded"]["git_commit"]), (CANDIDATE, first.commit))
        self.assertIn("source", seal["changed_layers"])
        marker = json.loads((campaign / "candidates" / CANDIDATE / "superseded-by.json").read_text(encoding="utf-8"))
        self.assertEqual(marker, {"schema_version": "claude-upgrade-candidate-superseded/v1", "candidate_id": CANDIDATE, "superseded_by": {"revision": 2, "candidate_id": OTHER}})
        self.assertTrue((revision_dir / "vc-4-checkpoint.json").is_file())
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        claude_upgrade_vc.replay_vc_checkpoint(campaign, claude_upgrade_vc.vc_campaign_plan(campaign, manifest), "VC-4", revision=2)
        self.assertEqual(seal_tests.store_status(store_root, CAMPAIGN_ID), "candidate_sealed")
        self.assertEqual(seal_tests.facts_by_kind(store_root, CAMPAIGN_ID)["candidate_frozen"], 2)
        # 被取代的候选只读：再为它记录构建即拒绝。
        code, _result, stderr = self.record(first)
        self.assertEqual(code, 1)
        self.assertIn("不属于当前 revision r2", stderr)

    def test_copied_codex_helpers_are_verbatim(self) -> None:
        """record-candidate-build 照搬的 Codex 通用函数与常量逐字一致（Codex 改动时提醒同步或登记分叉）。"""

        from tools.official_client_capture import codex_upgrade

        for name in (
            "_fingerprint", "_git_blob_sha256", "_validate_freeze_deletion_proof", "_validate_candidate_source_transition",
            "_require_clean_candidate_source", "_external_file_binding", "_external_json_object",
            "_require_outside_candidate_source", "_control_receipt_relative", "_read_json",
        ):
            with self.subTest(name=name):
                self.assertEqual(inspect.getsource(getattr(candidate, name)), inspect.getsource(getattr(codex_upgrade, name)))
        for name in (
            "FREEZE_SUCCESSOR_RESULT_PASSED", "FREEZE_SUCCESSOR_RESULT_PASSED_WITH_DELETIONS", "FREEZE_DELETION_PROOF_ALGORITHM",
            "FREEZE_REFERENCE_SCAN_ALGORITHM",
        ):
            self.assertEqual(getattr(candidate, name), getattr(codex_upgrade, name))
        self.assertEqual(candidate.SHA256_RE.pattern, codex_upgrade.SHA256_RE.pattern)


class RevisionMemberTests(_CandidateCase):
    """候选 revision 的编排器成员与入口：两份登记一致、强杀后重跑收敛、Store 作废缺失时预检拒绝、照搬与拒绝的成员。"""

    def test_both_registrations_must_agree(self) -> None:
        campaign = self.staged()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        self.assertEqual(claude_upgrade._current_candidate_revision_record(campaign, manifest), (None, None))
        with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "revision-open --initial"):
            claude_upgrade._require_candidate_revision(campaign, manifest, action="测试")
        # 共享流程写完（revision 目录＋账本 stage_revision）、Store 事实写入之前被强杀：两份登记不一致，提示重跑。
        with mock.patch.object(claude_upgrade_seal._StoreSession, "record_fact", side_effect=RuntimeError("模拟强杀")):
            code, _result, stderr = run_main(self.revision_open_argv(campaign))
        self.assertEqual(code, 1, stderr)
        self.assertTrue((claude_upgrade._candidate_revision_dir(campaign, 1) / "COMMIT").is_file())
        with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "重跑 revision-open"):
            claude_upgrade._current_candidate_revision_record(campaign, manifest)
        # 重跑同一命令收敛：共享流程走幂等分支，Store 事实补写。
        result = self.open_revision(campaign)
        self.assertEqual((result["idempotent"], result["store"]["reused"]), (True, False))
        revision, record = claude_upgrade._require_candidate_revision(campaign, manifest, action="测试")
        self.assertEqual((revision, record["candidate_id"]), (1, CANDIDATE))
        self.assertEqual(claude_upgrade._current_candidate_revision(campaign, manifest), 1)
        self.assertEqual(claude_upgrade._read_candidate_revision_record(campaign, 1)[0], record)
        # COMMIT 与账本事件不一致。
        event = claude_upgrade._ledger_last_stage_revision(self.root / "timing-ledger")
        with mock.patch.object(claude_upgrade, "_ledger_last_stage_revision", return_value={**event, "revision_commit_sha256": "0" * 64}):
            with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "COMMIT 与账本"):
                claude_upgrade._current_candidate_revision_record(campaign, manifest)
        # Store 已登记而编排器没有 stage_revision：两份登记不一致。
        with mock.patch.object(claude_upgrade, "_ledger_last_stage_revision", return_value=None):
            with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "两份登记不一致"):
                claude_upgrade._current_candidate_revision_record(campaign, manifest)
        # --initial 换候选重放即拒绝（Store 预检，零写入）。
        code, _result, stderr = run_main(self.revision_open_argv(campaign, OTHER))
        self.assertEqual(code, 1)
        self.assertIn("只能幂等重放同一候选的 r1", stderr)

    def test_supersedes_requires_store_invalidation(self) -> None:
        campaign = self.staged()
        self.open_revision(campaign, OTHER)
        source = self.source_tree()
        # apply 的共享流程完成、Store 作废对象写入之前被强杀：账本已 revision_required，Store 当前 revision 未作废。
        code, preview, stderr = run_main(self.invalidate_argv(campaign, "preview", OTHER, "--candidate-source", str(source)))
        self.assertEqual(code, 0, stderr)
        with mock.patch.object(claude_upgrade_revision, "_record_store_invalidation", side_effect=RuntimeError("模拟强杀")):
            code, _result, stderr = run_main(
                self.invalidate_argv(campaign, "apply", OTHER, "--candidate-source", str(source), "--approve-sha256", preview["review_sha256"])
            )
        self.assertEqual(code, 1, stderr)
        self.assertEqual(timing.inspect_ledger(self.root / "timing-ledger")["status"], "revision_required")
        # --supersedes 预检拒绝：编排器侧零写入（没有 r2 目录，账本不动）。
        head = timing.inspect_ledger(self.root / "timing-ledger")["head_sequence"]
        code, _result, stderr = run_main(self.revision_open_argv(campaign, CANDIDATE, supersedes=OTHER))
        self.assertEqual(code, 1)
        self.assertIn("先以同一批准摘要重跑 invalidate-candidate apply", stderr)
        self.assertFalse(claude_upgrade._candidate_revision_dir(campaign, 2).exists())
        self.assertEqual(timing.inspect_ledger(self.root / "timing-ledger")["head_sequence"], head)
        # 以同一批准摘要重跑 apply：共享流程幂等，Store 作废补写；之后取代成功。
        code, applied, stderr = run_main(
            self.invalidate_argv(campaign, "apply", OTHER, "--candidate-source", str(source), "--approve-sha256", preview["review_sha256"])
        )
        self.assertEqual((code, applied["idempotent"], applied["store"]["reused"]), (0, True, False), stderr)
        result = self.open_revision(campaign, CANDIDATE, supersedes=OTHER)
        self.assertEqual(result["store"]["payload"]["invalidation_ref"], applied["store"]["candidate_invalidation_ref"])
        # 取代之后：被取代的候选不能再取代，也不能 --initial。
        code, _result, stderr = run_main(self.revision_open_argv(campaign, "claude-cand-third", supersedes=OTHER))
        self.assertEqual(code, 1)
        self.assertIn("不是 --supersedes", stderr)

    def test_failed_run_member_is_verbatim_and_implicit_r1_is_refused(self) -> None:
        from tools.official_client_capture import codex_upgrade

        self.assertEqual(
            inspect.getsource(claude_upgrade_revision._candidate_failed_run_for_review),
            inspect.getsource(codex_upgrade._candidate_failed_run_for_review),
        )
        self.assertEqual(claude_upgrade_revision._VC_SEQUENCE_FILE_RE.pattern, codex_upgrade._VC_SEQUENCE_FILE_RE.pattern)
        self.assertEqual(tuple(claude_upgrade_revision.CANDIDATE_VC_PHASES), tuple(codex_upgrade.CANDIDATE_VC_PHASES))
        campaign = self.staged()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "没有隐含 r1"):
            claude_upgrade._implicit_r1_candidate_id(campaign, manifest)
        # 没有候选级 COMMIT：失败父 run 定位不到（事件 id 形态非法与命中 0 个都拒绝）。
        with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "形态非法"):
            claude_upgrade._candidate_failed_run_for_review(campaign, manifest, CANDIDATE, {"event_id": "x", "revision": 1})

    def test_identity_snapshot_reads_claude_build_receipt(self) -> None:
        campaign, preview = self.ready()
        fixture = build_fixture.CandidateBuildFixture(self.root / "build", campaign, CANDIDATE, run_main=run_main)
        fixture.build_source(preview=preview)
        fixture.build_artifacts()
        fixture.build_transition()
        fixture.build_implementation_receipt(preview["requirements"], CAMPAIGN_ID)
        with mock.patch.object(candidate, "BUILD_COMMAND_RUNNER", fixture.runner()):
            code, result, stderr = run_main(fixture.argv())
        self.assertEqual(code, 0, stderr)
        snapshot = claude_upgrade._candidate_identity_snapshot(campaign, CANDIDATE, None)
        self.assertEqual(snapshot["snapshot_sources"], ["build_receipt"])
        self.assertEqual((snapshot["git_commit"], snapshot["image_id"]), (fixture.commit, build_fixture.IMAGE_ID))
        self.assertEqual(snapshot["source_tree_sha256"], result["source_tree_sha256"])
        self.assertIsNotNone(snapshot["build_parameters_input_sha256"])
        # 没有构建收据的候选：取 --candidate-source。
        other = claude_upgrade._candidate_identity_snapshot(campaign, OTHER, fixture.source)
        self.assertEqual((other["snapshot_sources"], other["git_commit"]), (["candidate_source"], fixture.commit))
        with self.assertRaisesRegex(claude_upgrade.ConfigurationError, "--candidate-source"):
            claude_upgrade._candidate_identity_snapshot(campaign, OTHER, None)
