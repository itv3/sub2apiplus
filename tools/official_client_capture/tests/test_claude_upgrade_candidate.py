"""Claude VC-4 候选门禁计划（Claude 升级控制面设计 B3-04 第一步：plan-candidate-gates）。

全部离线、零请求。夹具沿用 B3-03 的画像链：VC-2 判 ``SPEC-BODY-001`` 为 change、``SPEC-TOOL-024`` 为 delete，VC-3 准备与
暂存批准之后 Store 到 ``profile_approved``；Campaign 总计划绑定一份 Claude 计时账本；候选 revision 由 Store 的
``candidate-revision-record`` 写显式 r1（编排器的 ``revision-open`` 接入属于 B3-05 编排器部分）。

* 需求预览：门禁需求由 VC-2 分区、VC-3 联合摘要与 VC-2 已批准迁移台账确定性推出，逐字段核对，与直接调用
  ``build_gate_requirements`` 逐字相同；预览零写入。
* 生成计划：映射按预览逐项绑定；计划落盘后与 ``build_gate_plan`` 复算一致、``validate_gate_plan`` 按需求校验通过；输出
  已存在即拒绝。
* 映射与路径：缺项、需求摘要不符、映射或输出在源码树外、源码树与 Campaign 互相包含、参数不成组，一律拒绝且零写入。
* 候选级前置门：没有显式 revision、候选不属于当前 revision、当前 revision 已作废、VC-3 未完成、没有绑定计时账本，
  一律拒绝。
* 候选模块不进 wire／evidence 闭包。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate as candidate
from tools.official_client_capture import claude_upgrade_classify
from tools.official_client_capture import claude_upgrade_profile
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.tests import test_claude_upgrade_classify as classify_tests
from tools.official_client_capture.tests import test_claude_upgrade_profile as profile_tests
from tools.official_client_control.contracts import invalidation_review_sha256

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
    """在 B3-03 夹具上走完 VC-3 暂存批准，总计划绑定 Claude 计时账本，Store 写显式候选 revision。"""

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
        return super().build_campaign(**kwargs)

    def staged(self) -> Path:
        """走到 VC-3 暂存批准完成（Store profile_approved、VC-3 checkpoint 已写）。"""

        campaign, _approved = self.prepared()
        code, preview, stderr = run_main(["stage-profile", "--campaign-dir", str(campaign)])
        self.assertEqual(code, 2, stderr)
        code, _staged, stderr = run_main(
            ["stage-profile", "--campaign-dir", str(campaign), "--approve-joint-sha256", preview["joint_sha256"], "--approved-by", "reviewer-a"]
        )
        self.assertEqual(code, 0, stderr)
        return campaign

    def session(self, campaign: Path) -> claude_upgrade_seal._StoreSession:
        return claude_upgrade_seal._StoreSession(claude_upgrade_store.campaign_store_root(campaign), CAMPAIGN_ID, self.root / "store-inputs")

    def open_revision(self, campaign: Path, candidate_id: str = CANDIDATE) -> dict[str, Any]:
        """Store 写显式 r1（revision-open --initial 的 Store 侧）。"""

        outcome = self.session(campaign).record_fact(
            "candidate-revision-record",
            "candidate_revision_recorded",
            {"revision": 1, "candidate_id": candidate_id, "supersedes": None, "invalidation_ref": None},
            issued_at=claude_upgrade._utc_now(),
            logical_action="claude-upgrade:revision-open:initial",
        )
        return outcome["fact_ref"]

    def invalidate(self, campaign: Path, candidate_id: str, revision_ref: dict[str, Any]) -> None:
        """Store 作废当前 revision（candidate-invalidate 两步批准，这里直接给出批准后的对象）。"""

        review = {
            "campaign_id": CAMPAIGN_ID, "candidate_id": candidate_id, "revision": 1, "revision_ref": revision_ref,
            "reason": "候选源码需要变化（测试）", "evidence_refs": [],
        }
        payload = {**review, "approved_by": "reviewer-a", "approved_at_utc": claude_upgrade._utc_now(), "review_sha256": invalidation_review_sha256(review)}
        path = self.root / "invalidation.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        claude_upgrade_seal.store_cli(
            ["candidate-invalidate", "--store", claude_upgrade_store.campaign_store_root(campaign), "--campaign", CAMPAIGN_ID,
             "--input", path, "--approve-sha256", payload["review_sha256"]]
        )

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
    """候选级前置门：revision、作废、VC-3 与计时账本。"""

    def test_candidate_revision_gates(self) -> None:
        campaign = self.staged()
        code, _result, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("revision-open --initial", stderr)
        revision_ref = self.open_revision(campaign, OTHER)
        code, _result, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("不属于当前 revision r1", stderr)
        self.invalidate(campaign, OTHER, revision_ref)
        code, _result, stderr = run_main(self.preview_argv(campaign, OTHER))
        self.assertEqual(code, 1)
        self.assertIn("已作废", stderr)

    def test_requires_completed_vc3(self) -> None:
        campaign, _approved = self.prepared()
        self.open_revision(campaign)
        code, _result, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("VC-3", stderr)

    def test_requires_bound_timing_ledger(self) -> None:
        self.bind_ledger = False
        campaign = self.staged()
        self.open_revision(campaign)
        code, _result, stderr = run_main(self.preview_argv(campaign))
        self.assertEqual(code, 1)
        self.assertIn("UpgradeTimingLedger", stderr)

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
