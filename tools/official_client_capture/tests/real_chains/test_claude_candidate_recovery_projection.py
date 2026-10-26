"""真实恢复前缀的来源投影回归：一次构造世界，集中复核 b0／b1／ar1／ar2。

沿既有离线输入源执行真实候选 capture、seal、transient 裁定、ar1
部分失败、对账批准、ar2 承接、增量封存、比较、builder 与严格 accept，
不执行七场景交付长链。另用真实 P→S→T run／resume 检查跨 Campaign
来源坐标；该专项的最小阶段边界输入不代表正式资格签发。
候选阶段、恢复段、Store、原件摘要、权限和归档读者均保持生产实现。
外部官方序列、Docker／网关和 Mac 输入仍是明示的回归来源，本测试
不签发 ARM64 原生资格，也不代表真实客户端版本语义认证。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping
from unittest import mock

from tools.official_client_capture import claude_qualification_material_source as material
from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_assertion_profile as assertion_profile
from tools.official_client_capture import claude_upgrade_attempt_recovery as recovery
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_predecessor_sealed as predecessor_sealed
from tools.official_client_capture import claude_upgrade_recovery as attempt_recovery
from tools.official_client_capture import claude_upgrade_segment_seal as segment_seal
from tools.official_client_capture import codex_upgrade_evidence_manifest as evidence_manifest
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_client_receipt_fixture as clients
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator
from tools.official_client_capture.tests import claude_fault_chain_world as worlds
from tools.official_client_capture.tests import claude_transient_chain_driver as transient
from tools.official_client_capture.tests import claude_typed_vc6_fixture as typed_sources
from tools.official_client_capture.tests import claude_vc5_chain_fixture as sources
from tools.official_client_capture.tests import test_claude_upgrade_candidate_reuse as reuse_tests
from tools.official_client_control.delivery_execution import VerifiedSource
from tools.official_client_control.errors import ControlError
from tools.official_client_control.qualification_material_import import validate_qualification_material_import
from tools.official_client_control.store import ControlStore
from tools.official_client_control.sealed_gate_bindings import (
    VerifiedGateProjection,
    _TOKEN,
    project_delivery_raw_bindings,
)

DIAGNOSTIC_ENV = "CLAUDE_PROJECTION_DIAGNOSTIC_ROOT"


class ProjectionCollector:
    """只收集生产读者交回的精确父证明，不按路径或 schema 推定权限。"""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.originals: dict[str, dict[str, Any]] = {}
        self.parents: dict[str, material.VerifiedQualificationSubjectProjection] = {}
        self.gate: VerifiedGateProjection | None = None

    def retain(self, path: Path, expected_sha256: str | None = None) -> bytes:
        if not path.is_relative_to(self.data):
            raise ControlError("短回归原件越过世界数据根")
        raw, _metadata = material._plain_file(path)
        row = sources.binding(path, self.data)
        if expected_sha256 is not None and row["sha256"] != expected_sha256:
            raise ControlError("短回归原件摘要漂移")
        if self.originals.setdefault(row["path"], row) != row:
            raise ControlError("短回归同坐标原件漂移")
        return raw

    def project_parent(self, path: Path, base: Path, metadata=(), *, coordinate_roots=()) -> None:
        self.retain(path)
        document = json.loads(path.read_bytes())
        domains = []
        for coordinate in metadata:
            node = document
            for key in coordinate:
                node = node[int(key)] if isinstance(node, list) else node[key]
            domains.append((coordinate, material.canonical_sha256(node)))
        row = sources.binding(path, self.data)
        proof = material.VerifiedQualificationSubjectProjection(row,
            base.relative_to(self.data).as_posix(), tuple(domains),
            tuple((coordinate, root.relative_to(self.data).as_posix())
                  for coordinate, root in coordinate_roots), material._TOKEN)
        if self.parents.setdefault(row["path"], proof) != proof:
            raise ControlError("短回归同一父原件出现不同坐标证明")

    def absorb(self, originals, parents) -> None:
        for row in originals:
            if self.originals.setdefault(row["path"], row) != row:
                raise ControlError("完整 subject 原件与专项原件冲突")
        for proof in parents:
            path = proof.parent_binding["path"]
            if self.parents.setdefault(path, proof) != proof:
                raise ControlError("完整 subject 投影与专项投影冲突")

    def absorb_gate(self, proof: VerifiedGateProjection) -> None:
        """合并真实导入读者的完整第二根证明；不能仅复制 subject 父字段。"""
        if not isinstance(proof, VerifiedGateProjection) or proof.token is not _TOKEN:
            raise ControlError("专项门禁组合必须使用生产纯验证器返回的投影")
        self.absorb(proof.archive_bindings, proof.subject_projections)
        self.gate = proof

    def close(self, initial, *, include_archive: bool = False):
        channel = VerifiedSource("0" * 64, "1" * 64, {}, "2" * 64,
                                 "fixture-only", self.data)
        projection = self.gate or VerifiedGateProjection((), (), (), (), _TOKEN)
        projection = replace(projection,
            archive_bindings=tuple(self.originals.values()) if include_archive else (),
            subject_projections=tuple(self.parents.values()))
        return project_delivery_raw_bindings(channel, initial, gate_projection=projection)

    def projected(self, path: Path) -> list[dict[str, Any]]:
        row = sources.binding(path, self.data)
        return self.parents[row["path"]].project(row, path.read_bytes())


SECOND_JOB = "candidate-entrypoint-identity"
SECOND_PROBE = "v4-replay-agent-sdk"


class PartialRecoveryProjectionDriver(transient.ClaudeTransientChainDriver):
    """先冻结两个真实失败 Job，再在 ar1 只注入一个宿主前检失败。"""

    def setUp(self) -> None:
        # 原 helper 只保留最外层世界，嵌套 runtime 仍会被清理。诊断开关
        # 因而只脱离本专项 setUp 创建目录的删除器；路径原地保留，守卫、
        # 来源 registry 与临时 patch 仍按原清理顺序退出，测试期临时目录不保留。
        self.retained_setup_worlds: list[str] = []
        self.retain_setup_active = bool(os.environ.get(DIAGNOSTIC_ENV))
        if self.retain_setup_active:
            create = tempfile.TemporaryDirectory

            def temporary(*arguments, **options):
                directory = create(*arguments, **options)
                if self.retain_setup_active:
                    directory._finalizer.detach()
                    directory.cleanup = lambda: None
                    self.retained_setup_worlds.append(str(Path(directory.name).resolve()))
                return directory

            patch = mock.patch.object(tempfile, "TemporaryDirectory", side_effect=temporary)
            patch.start()
            self.addCleanup(patch.stop)
        try:
            super().setUp()
        finally:
            self.retain_setup_active = False
        transient._write(self.relay_control, {"bad_probes": [transient.FAILED_PROBE, SECOND_PROBE]})

    def candidate_build_root(self) -> Path:
        """候选源码仓放进宿主数据根：严格 subject 读者只接受数据根内的候选源码根（受管来源合同）。"""
        return self.data / "candidate-build"

    def install_relay_source(self) -> None:
        super().install_relay_source()
        relay = self.runtime.tool_copy / "claude_fw_e_relay.py"
        source = relay.read_text(encoding="utf-8")
        original = 'probe == json.load(open(os.environ["CLAUDE_TRANSIENT_RELAY_CONTROL"], encoding="utf-8")).get("bad_probe")'
        replacement = 'probe in json.load(open(os.environ["CLAUDE_TRANSIENT_RELAY_CONTROL"], encoding="utf-8")).get("bad_probes", [])'
        self.assertEqual(source.count(original), 1)
        relay.write_text(source.replace(original, replacement), encoding="utf-8")
        relay.chmod(0o700)

    def source_command(self, argv):
        result = super().source_command(argv)
        code, payload, _stderr = result
        if hasattr(self, "observations"):
            self.observations.append({"coverage": "真实入口：" + " ".join(str(value) for value in argv[:2]),
                "status": "observed", "exit_code": code,
                "result_status": payload.get("status") if isinstance(payload, dict) else None,
                "recovery_revision": payload.get("recovery_revision") if isinstance(payload, dict) else None})
        return result

    def freeze_two_job_control_profile(self, campaign: Path) -> None:
        """冻结覆盖两个 Job 的明示画像输入；评估生成器和失败定位仍为真实实现。

        原连续链输入只让 transport.http 承担全部规则，因而只能定位第一个
        Job。本专项在首次冻结前增加 entrypoint.agent_sdk 场景，同样套用
        版本头和脱敏判据；不改共享夹具，也不认证全规则的真实版本语义。
        """
        real = assertion.build_expectations

        def build(*, claude_version, ledger, scenarios_payload, templates,
                  release_profile=None, release_wire=None, rule_manifest=None):
            owners = sorted({str(row["owner_id"]) for row in ledger["assertions"]
                             if row["owner_kind"] == "required_rule"})
            atomic_ids = sorted(str(row["assertion_id"]) for row in ledger["assertions"]
                                if row["owner_kind"] == "required_rule")
            scenarios = copy.deepcopy(evaluator.SCENARIOS_PAYLOAD)
            scenarios["claude_version"] = claude_version
            transport = next(row for row in scenarios["evidence_scenarios"]
                             if row["scenario_id"] == sources.EVIDENCE_SCENARIO)
            for row in scenarios["evidence_scenarios"]:
                row["covers"] = owners if row is transport else []
            second = copy.deepcopy(transport)
            second.update(scenario_id="entrypoint.agent_sdk", probe_ids=[SECOND_PROBE],
                          description="第二个真实候选 Job 的离线控制面输入")
            scenarios["evidence_scenarios"].append(second)
            template = evaluator.TEMPLATES["templates"][sources.TEMPLATE_ATOMIC]
            inputs = {"schema_version": assertion.TEMPLATES_SCHEMA_VERSION,
                      "description": "两个真实候选 Job 的离线控制面画像",
                      "templates": {atomic_id: copy.deepcopy(template) for atomic_id in atomic_ids}}
            return real(claude_version=claude_version, ledger=ledger,
                        scenarios_payload=scenarios, templates=inputs,
                        release_profile=None, release_wire=copy.deepcopy(evaluator.RELEASE_WIRE),
                        rule_manifest=rule_manifest)

        with mock.patch.object(assertion, "build_expectations", side_effect=build):
            assertion_profile.freeze_approved_assertion_profile(
                campaign, upgrade.load_campaign_manifest(campaign))

    def preserve_builder_failure(self, world: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        """先保留本轮真实判据输出，再让前缀断言报告失败，避免清理后只剩退出码。"""
        baseline = world["evaluation_baseline"]
        target = upgrade._stage_write_target(world["campaign"], transient.CANDIDATE,
                                             baseline, "assertions")
        self.observations.append({"coverage": f"b{baseline} 真实 builder 失败诊断",
            "status": "observed", "campaign_run": payload.get("campaign_run"),
            "assertions_directory": str(target)})
        value = os.environ.get(DIAGNOSTIC_ENV)
        if value and target.is_dir():
            shutil.copytree(target, Path(value) / f"builder-b{baseline}", symlinks=True)

    def evaluation_context(self, campaign: Path) -> dict[str, Any]:
        """在首次比较前声明两场景的官方离线输入，不改变历史官方包或原请求字节。

        两场景本专项只检查同一版本头与脱敏合同，官方 print 字节可作为
        二者的明示合成输入；这不提供真实 Agent SDK 的行为或版本认证。
        修改发生在新输入第一次物化前，之后只读，候选阶段不使用替身结果。
        """
        with self.runtime.patched():
            layout = {"host_runs_root": str(upgrade.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT),
                      "container_run_roots": [str(root) for root in upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS]}
            world = worlds.real_candidate_world(self, campaign, candidate_id=transient.CANDIDATE,
                revision=1, expected_client_requests=2, capture_layout=layout)
            side = world["official"]
            self.bind_two_scene_official_input(side)
            world = worlds.prepare_delivery_sources(self, world)
            self.rebase_official_capture_bindings(world)
            return world

    def rebase_official_capture_bindings(self, world: Mapping[str, Any]) -> None:
        """把物化官方来源里的 capture_manifest／capture_index 两个绑定改成宿主数据根坐标。

        生产 ``official_evaluation_source`` 取这两个绑定自 Store 完整性清单，路径相对宿主数据根；
        严格 subject 读者（``qualification_gate_subject``）也按数据根坐标读它们。共享夹具
        ``materialize_official_source`` 为兼容交付归档读者，把这两个绑定写成相对 Campaign 的路径
        （inventory 在生产里本就相对 Campaign，不动）。本专项要走严格 subject 读者，所以在首次 compare 前、
        来源配置第一次被消费之前，只在本专项的来源文件里改这两个路径；文件字节（摘要、字节数）与共享夹具、
        其它测试都不受影响。
        """
        config = json.loads(world["config_path"].read_bytes())
        source_path = Path(config["official_source"])
        source = json.loads(source_path.read_bytes())
        prefix = world["campaign"].relative_to(self.data).as_posix()
        for name in ("capture_manifest", "capture_index"):
            binding = source[name]
            if binding["path"].startswith("seam/official/"):
                binding["path"] = f"{prefix}/{binding['path']}"
        sources._write_json(source_path, source)

    @staticmethod
    def bind_two_scene_official_input(side: evaluator.SideEvidence) -> Mapping[str, Any]:
        """输入还须绑定真实 companion 文件，不能仅拿显式 artifacts 重建清单。"""
        previous = json.loads(side.manifest_path.read_bytes())
        for artifact in side.capture_manifest["artifacts"]:
            if sources.EVIDENCE_SCENARIO in artifact["scenario_ids"]:
                artifact["scenario_ids"] = sorted({*artifact["scenario_ids"], "entrypoint.agent_sdk"})
        bindings = {}
        for path in side.evidence_root.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                bindings[path.relative_to(side.evidence_root).as_posix()] = {
                    "sha256": material.sha256_bytes(raw), "bytes": len(raw)}
        manifest = assertion.bind_capture_manifest(side.capture_manifest, bindings,
                                                   capture_id=previous["capture_id"])
        transient._write(side.manifest_path, manifest)
        return manifest

    def run_partial_recovery_prefix(self) -> dict[str, Any]:
        """每个变更均由真实入口落盘；失败源与成功源保持不同执行身份。"""
        campaign = self.drive_vc3_vc4_to_candidate_sealed()
        self.prepare_runtime(campaign)
        self.freeze_two_job_control_profile(campaign)
        with clients.source_scope():
            code, first, stderr = self.source_command(["capture-candidate", "run", "--campaign-dir", campaign,
                "--candidate-id", transient.CANDIDATE, "--acknowledge-live-requests"])
            self.assertIsNotNone(first, stderr)
            self.assertEqual((code, first["status"]), (2, "awaiting_receipts"), stderr)
            attempt_id = first["attempt_id"]
            with self.source_context():
                attempt_root, original_attempt = upgrade._load_capture_attempt(
                    campaign, "candidate", transient.CANDIDATE, attempt_id)
            client_args = self.write_clients(campaign, attempt_root, original_attempt)
            self.seal(campaign, attempt_id, clients=client_args)
            code, _accounted, stderr = self.source_command(["account-sealed-candidate", "--campaign-dir", campaign,
                "--candidate-id", transient.CANDIDATE])
            self.assertEqual(code, 0, stderr)
            world = self.evaluation_context(campaign)
            code, failed, stderr = self.dispatch_builder(world, 9, baseline=0)
            self.assertIsNotNone(failed, stderr)
            if code:
                self.preserve_builder_failure(world, failed)
            self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:assert-rules"), stderr)
            code, reconciled, stderr = self.source_command(["reconcile-supervisor-run", "--campaign-dir", campaign,
                "--run-dir", failed["campaign_run"]["run_dir"]])
            self.assertIsNotNone(reconciled, stderr)
            self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
            with self.source_context():
                preview = self.preview(world)
                self.assertEqual(set(preview["failure_scope"]["jobs"]), {transient.FAILED_JOB, SECOND_JOB})
                code, applied, stderr = self.recover(world, "apply", "--approve-sha256", preview["review_sha256"],
                    "--root-cause-class", "transient-environment")
            self.assertIsNotNone(applied, stderr)
            self.assertEqual(code, 0, stderr)
            original_stage = candidate_capture.legacy_stage_result_path(campaign, transient.CANDIDATE)
            recovery_path = campaign / "candidates" / transient.CANDIDATE / "revisions" / "b1" / "recovery.json"
            baseline_recovery = json.loads(recovery_path.read_bytes())
            self.assertEqual(set(baseline_recovery["execute_jobs"]), {transient.FAILED_JOB, SECOND_JOB})
            old_boundary = self.boundary([original_stage, attempt_root,
                campaign / "candidates" / transient.CANDIDATE / "seal",
                *[Path(value) for row in original_attempt["results"] for value in row["evidence_roots"]]])
            transient._write(self.relay_control, {"bad_probes": []})
            self.runtime.set_candidate_control({"precheck_fail": [SECOND_PROBE]})
            with mock.patch.object(upgrade, "JOB_RETRY_LIMIT", 0):
                code, failed_segment, stderr = self.source_command(["capture-candidate", "run", "--campaign-dir", campaign,
                    "--candidate-id", transient.CANDIDATE, "--attempt-recovery", "ar1", "--acknowledge-live-requests"])
            self.assertIsNotNone(failed_segment, stderr)
            self.assertEqual((code, failed_segment["status"]), (2, "failed"), stderr)
            failed_root = attempt_root / "recovery" / "ar1"
            failed_summary = json.loads((failed_root / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME).read_bytes())
            results = {row["id"]: row for row in failed_summary["results"]}
            self.assertEqual(results[transient.FAILED_JOB]["status"], "complete")
            self.assertNotEqual(results[SECOND_JOB]["status"], "complete")
            # 修复只改外部故障开关；失败段正文与原 b0 封存原件均不修改。
            self.runtime.set_candidate_control({})
            reconcile_argv = ["reconcile-attempt", "--campaign-dir", campaign, "--attempt-id", attempt_id,
                "--recovery-revision", "ar1"]
            code, decision, stderr = self.source_command(reconcile_argv)
            self.assertIsNotNone(decision, stderr)
            self.assertEqual((code, decision["status"]), (0, "recoverable"), stderr)
            preview = decision["recovery_preview"]
            self.assertEqual((preview["reuse_job_ids"], preview["execute_job_ids"]), ([transient.FAILED_JOB], [SECOND_JOB]))
            code, approved, stderr = self.source_command([*reconcile_argv,
                "--approve-recovery-sha256", preview["review_sha256"]])
            self.assertIsNotNone(approved, stderr)
            self.assertEqual(code, 0, stderr)
            code, current, stderr = self.source_command(["capture-candidate", "run", "--campaign-dir", campaign,
                "--candidate-id", transient.CANDIDATE, "--attempt-recovery", "ar2", "--rerun-failed",
                "--recovery-preview", approved["recovery_preview_path"], "--acknowledge-live-requests"])
            self.assertIsNotNone(current, stderr)
            self.assertEqual((code, current["status"]), (2, "awaiting_receipts"), stderr)
            current_root = attempt_root / "recovery" / "ar2"
            summary = json.loads((current_root / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME).read_bytes())
            reservation = json.loads((current_root / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME).read_bytes())
            self.assertEqual(reservation["reuse_job_ids"], [transient.FAILED_JOB])
            self.assertEqual(set(reservation["reuse_proofs"]), {transient.FAILED_JOB})
            self.seal(campaign, attempt_id, recovery="ar2")
            code, _accounted, stderr = self.source_command(["account-sealed-candidate", "--campaign-dir", campaign,
                "--candidate-id", transient.CANDIDATE, "--attempt-recovery", "ar2"])
            self.assertEqual(code, 0, stderr)
            code, passed, stderr = self.dispatch_builder(world, world["next_sequence"], baseline=1)
            self.assertIsNotNone(passed, stderr)
            if code:
                self.preserve_builder_failure(world, passed)
            self.assertEqual((code, passed["campaign_run"]["reason"]), (0, "queue-complete"), stderr)
            self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
            receipt = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)
            self.assertEqual(receipt["status"], "complete")
            worlds.refresh_world(self, world)
            world.update(original_attempt_root=attempt_root, original_attempt=original_attempt, attempt_id=attempt_id,
                recovery_revision="ar2", recovery_root=current_root, recovery_summary=summary,
                prior_recovery_root=failed_root, prior_recovery_summary=failed_summary,
                baseline_recovery=baseline_recovery, old_boundary=old_boundary, relay_control=self.relay_control)
            return world


class CandidateRecoveryProjectionTests(PartialRecoveryProjectionDriver):
    """一个合法恢复前缀，随后逐父检查；独立失败使用 subTest 集中收集。"""

    def check_case(self, name: str, operation: Callable[[], None]) -> None:
        with self.subTest(coverage=name):
            try:
                operation()
            except Exception as error:
                self.observations.append({"coverage": name, "status": "failed",
                    "type": type(error).__name__, "message": str(error)})
                raise
            else:
                self.observations.append({"coverage": name, "status": "passed"})

    def write_diagnostics(self) -> None:
        value = os.environ.get(DIAGNOSTIC_ENV)
        if not value:
            return
        root = Path(value)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "recovery-projection-observations.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"fixture_only": True, "world": str(self.data),
                "retained_setup_worlds": self.retained_setup_worlds,
                "observations": self.observations}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    def require_bindings(self, parent: Path, targets: list[Path]) -> None:
        rows = {row["path"] for row in self.collector.projected(parent)}
        self.assertTrue({path.relative_to(self.data).as_posix() for path in targets} <= rows,
                        f"{parent} 未指向真实产出坐标")

    def check_original_family(self, path: Path) -> None:
        closed = self.collector.close([sources.binding(path, self.data)])
        self.assertIn(path.relative_to(self.data).as_posix(), {row["path"] for row in closed})

    def invalid_stage(self, name: str, document: dict[str, Any]) -> Path:
        return transient._write(self.campaign / "projection-invalid-inputs" / name / "result.json", document)

    def assert_stage_rejected(self, path: Path) -> None:
        collector = ProjectionCollector(self.data)
        with self.assertRaises((ConfigurationError, ControlError)):
            material._retain_candidate_recovery_materials(self.campaign, self.manifest,
                sources.binding(path, self.campaign, with_bytes=False), self.data,
                candidate_id=transient.CANDIDATE, retain=collector.retain,
                project_parent=collector.project_parent)

    def check_wrong_producer_with_new_digest(self) -> None:
        altered = copy.deepcopy(self.stage)
        old = {row["id"]: row for row in self.world["original_attempt"]["results"]}
        for result in altered["results"]:
            if result["id"] == transient.FAILED_JOB:
                result["job_closure"] = copy.deepcopy(old[result["id"]]["job_closure"])
        effective = json.loads(self.effective_path.read_bytes())
        effective["results"] = altered["results"]
        effective.pop("effective_results_sha256")
        effective["effective_results_sha256"] = upgrade._fingerprint(effective)
        path = transient._write(self.campaign / "projection-invalid-inputs" / "wrong-producer" / "effective-results.json", effective)
        altered["effective_results"] = sources.binding(path, self.campaign, with_bytes=False)
        self.assert_stage_rejected(self.invalid_stage("wrong-producer", altered))

    def check_wrong_summary_with_new_binding(self) -> None:
        altered = copy.deepcopy(self.stage)
        altered["recovery"].update(sources.binding(self.attempt_root / "attempt.json",
                                                self.campaign, with_bytes=False))
        self.assert_stage_rejected(self.invalid_stage("wrong-summary", altered))

    def check_unknown_parent_not_authorized(self) -> None:
        original = self.attempt_root / "attempt.json"
        copy_path = self.data / "projection-unverified-copy.json"
        copy_path.write_bytes(original.read_bytes())
        copy_path.chmod(0o600)
        row = sources.binding(copy_path, self.data)
        original_proof = self.collector.parents[original.relative_to(self.data).as_posix()]
        self.assertIsNone(original_proof.project(row, copy_path.read_bytes()))
        with self.assertRaises(ControlError):
            self.collector.close([row])

    def check_unknown_stage_field_not_authorized(self) -> None:
        altered = copy.deepcopy(self.stage)
        closure = next(row["job_closure"] for row in altered["results"]
                       if row["id"] == transient.FAILED_JOB)
        altered["unknown_attachment"] = copy.deepcopy(closure)
        self.assert_stage_rejected(self.invalid_stage("unknown-field", altered))

    def check_round_two_cannot_change_first_round(self) -> None:
        """伪造轮次即使重绑摘要也必须拒绝，不能为它签发父原件权限。"""
        first = upgrade._legacy_stage_path(self.campaign, "capture-candidate", transient.CANDIDATE)[1]
        altered = copy.deepcopy(self.stage)
        altered["validation_round"] = 2
        altered["round_one_result"] = sources.binding(first, self.campaign, with_bytes=False)
        altered["store"]["validation_attempt_id"] = candidate_seal.validation_attempt_id(
            f"{self.attempt_root.name}-{self.segment_root.name}", 2)
        # 正规 v2 坐标也不能把 b1 的恢复结果伪称为 b0 首轮的门禁重试。
        path = candidate_seal._round_result_path(self.campaign, transient.CANDIDATE, 2,
                                                round_one_path=self.stage_path)
        self.assertFalse(path.exists())
        transient._write(path, altered)
        try:
            self.assert_stage_rejected(path)
        finally:
            path.unlink()
            path.parent.rmdir()
            path.parent.parent.rmdir()
        # 首轮摘要正确但轮次原件在任意目录，同样不能授予字段转根。
        altered["round_one_result"] = sources.binding(self.stage_path, self.campaign, with_bytes=False)
        self.assert_stage_rejected(self.invalid_stage("foreign-round-two", altered))

    def check_real_reuse_proof_bindings(self) -> None:
        # 证明由真实 ar1 对账、批准与 ar2 预约产生。封存以后消费冻结
        # 证明，不再重新要求旧失败段满足运行期 inode／mtime 边界。
        reservation = json.loads((self.segment_root / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME).read_bytes())
        proofs = reservation["reuse_proofs"]
        self.assertEqual(set(proofs), {transient.FAILED_JOB})
        proof = proofs[transient.FAILED_JOB]
        self.assertEqual(proof["recovered_from"], "ar1")
        for name in ("result", "checkpoint", "permission_closeout", "after_probe", "restoration_report"):
            binding = proof[name]
            path = self.campaign / binding["path"]
            self.assertTrue(path.is_relative_to(self.prior_segment_root))
            raw, _metadata = material._plain_file(path)
            self.assertEqual((material.sha256_bytes(raw), len(raw)),
                             (binding["sha256"], binding["bytes"]))
        result = recovery.reused_job_result(self.campaign, proof)
        self.assertEqual(result["source_receipt"], proof["result"])
        self.assertEqual(result["recovery_checkpoint"], proof["checkpoint"])
        inherited = next(row for row in self.summary["results"] if row["id"] == transient.FAILED_JOB)
        self.assertEqual(result, inherited)

    def check_after_metadata_rebind(self) -> None:
        """使旧运行期边界和当前封存边界分别漂移，重绑后仍消费冻结来源证明。"""
        inherited = next(row for row in self.summary["results"] if row["id"] == transient.FAILED_JOB)
        run = inherited["environment_runs"][0]
        probe = self.prior_segment_root / run["after_probe"]["path"]
        snapshot = probe.parent / json.loads(probe.read_bytes())["snapshots"][0]["path"]
        digest = material.sha256_bytes(snapshot.read_bytes())
        metadata = snapshot.stat()
        os.utime(snapshot, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
        # 旧段的运行期边界确实已经失效，不能拿旧 reader 的失败冒充
        # 当前合并段的资格。修复只经生产元数据重绑命令完成。
        with self.assertRaises(ConfigurationError):
            recovery.load_segment(self.campaign, transient.CANDIDATE, self.attempt_root.name, "ar1")
        # 旧段探针不一定落在当前合并清单内，不能把它的变化当成本次
        # rebind 必须成功的条件。另从已验真的当前段封存清单选取一份
        # 原件，只改元数据；该选择仅注入测试故障，不授予任何读取权限。
        manifest_path = segment_seal.segment_boundary(self.campaign, self.segment_root, self.summary)
        self.assertIsNotNone(manifest_path)
        manifest = json.loads(manifest_path.read_bytes())
        roots = {row["prefix"]: Path(row["path"]) for row in manifest["roots"]}
        current_files = []
        for entry in manifest["entries"]:
            prefix, relative = entry["path"].split("/", 1)
            root = roots[prefix]
            path = root / relative if root.is_dir() else root
            if path.is_relative_to(self.segment_root):
                current_files.append((path, entry))
        self.assertTrue(current_files, "合并封存清单必须包含本次段的真实原件")
        current, entry = min(current_files, key=lambda item: str(item[0]))
        current_digest = material.sha256_bytes(current.read_bytes())
        self.assertEqual(current_digest, entry["sha256"])
        metadata = current.stat()
        os.utime(current, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
        with self.assertRaises(candidate_seal.SealedEvidenceBoundaryError) as drift:
            segment_seal.segment_boundary(self.campaign, self.segment_root, self.summary)
        self.assertEqual(drift.exception.failure_class, "evidence-metadata-drift")
        code, rebound, stderr = self.source_command(["harden-evidence-permissions", "rebind-boundary",
            "--campaign-dir", self.campaign, "--attempt-id", self.attempt_root.name,
            "--candidate-id", transient.CANDIDATE, "--attempt-recovery", self.segment_root.name])
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(rebound, stderr)
        self.assertGreaterEqual(rebound["drifted_entry_count"], 1)
        self.assertEqual(material.sha256_bytes(snapshot.read_bytes()), digest)
        self.assertEqual(material.sha256_bytes(current.read_bytes()), current_digest)
        originals, parents = [], []
        # 此方法若再次重算旧段继承资格就失败；正式段和封存校验器
        # 仍执行生产实现，未用替身提供任何成功证明。
        with mock.patch.object(recovery, "segment_reuse_proofs",
                side_effect=AssertionError("封存后不应重算旧失败段运行期继承资格")):
            material.qualification_gate_subject(self.world["store"], self.qualification, self.data,
                self.accept_document, read_bindings=originals, read_projections=parents)
        collector = ProjectionCollector(self.data)
        collector.absorb(originals, parents)
        collector.absorb_gate(self.validate_gate_materials(prior=self.gate_projection))
        collector.close([self.qualification["sealed_result_binding"]], include_archive=True)
        self.assertTrue(any("evidence-manifest-rebind-" in path for path in collector.originals))
        self.assertIn((self.segment_root / segment_seal.SEGMENT_BOUNDARY_FILENAME).relative_to(self.data).as_posix(),
                      collector.originals)

    def prepare_gate_materials(self) -> None:
        """沿受管回归来源和独立原 reader 补齐候选 accept 的第二根归档证明。

        rollback 只为材料合同的另一角色建立独立合成四根，不执行交付动作。
        原候选 capture、批准、封存、builder 和 accept 的字节均不改写。
        """
        from tools.official_client_capture.tests.test_claude_real_delivery_integration import DeliveryCampaign
        from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world

        root = self.data / "control/projection-rollback"
        root.parent.mkdir(mode=0o700, exist_ok=True)
        root.mkdir(mode=0o700)
        configuration, _, _ = fixture_world(root / "runtime")
        rollback = DeliveryCampaign(root, schema="v2", store=self.world["store"],
            external_root=self.data, external_prefix="control/projection-rollback/external")
        # 独立回滚来源使用不同的合成产品标识，避免与最小门禁夹具的
        # 内容寻址对象同名；不覆盖旧对象，也不放宽不可变写入合同。
        rollback.persona = {**rollback.persona, "official_product": "projection-rollback-fixture"}
        rollback.materials = configuration
        rollback.campaign_id = "projection-rollback-" + material.canonical_sha256(self.qualification)[:32]
        rollback.bootstrap_and_campaign(platforms=["linux/arm64"], campaign_purpose="validation_only")
        rollback.discovery_and_evidence()
        rollback.profile_objects()
        rollback.select_profile("rollback")
        rollback.profile_approve("validation_only")
        rollback.strict_validate(tag="baseline")
        sealed = {"schema_version": "claude-upgrade-acceptance/v1",
            "candidate_id": "synthetic-candidate-baseline", "production_state": "not_activated",
            "store": {"acceptance_ref": rollback.references["acceptance-baseline"],
                "validation_completed_ref": rollback.references["validation-completed-baseline"]}}
        path = transient._write(root / "sealed-result.json", sealed)
        configuration["candidate"]["qualification"] = self.qualification
        configuration["rollback"]["qualification"] = {
            "store_relative": self.qualification["store_relative"], "external_root_relative": ".",
            "campaign_id": rollback.campaign_id, "candidate_id": sealed["candidate_id"],
            "candidate_ref": rollback.references["candidate-baseline"],
            "build_receipt_ref": rollback.references["candidate-build-receipt-baseline"],
            "validation_ref": rollback.references["validation-completed-baseline"],
            "acceptance_ref": rollback.references["acceptance-baseline"],
            "sealed_result_binding": sources.binding(path, self.data)}
        self.gate_configuration = configuration
        self.gate_import_binding = typed_sources.prepare_fixture_qualification_materials(
            self.world["store"], configuration, self.data, gate_root=self.world["gate_root"])
        self.gate_projection = self.validate_gate_materials()
        self.assertTrue(self.gate_projection.domains)
        self.assertTrue(self.gate_projection.material_files)
        self.assertTrue(self.gate_projection.reader_replays)
        self.collector.absorb_gate(self.gate_projection)

    def validate_gate_materials(self, *, prior=None) -> VerifiedGateProjection:
        """实际读者重验完整导入；既有 reader 证明只能按生产合同受控承接。"""
        channel = VerifiedSource("0" * 64, "1" * 64, {}, "2" * 64,
                                 "fixture-only", self.data, gate_projection=prior)
        store = ControlStore.open_read_only(self.world["store"].root)
        with typed_sources.fixture_mac_source_scope(self.gate_configuration, self.data):
            return validate_qualification_material_import(store, self.gate_import_binding, channel,
                {role: self.gate_configuration[role]["qualification"] for role in ("candidate", "rollback")},
                purpose="formed_replay", expected_source_scope="regression_fixture")

    def test_real_recovery_prefix_closes_all_exact_parent_families(self) -> None:
        self.observations = []
        self.addCleanup(self.write_diagnostics)
        # 只有一次成功的 b1 builder 和 accept；不运行暂停、延期或 VC-6。
        self.world = self.run_partial_recovery_prefix()
        # 作用域只登记明示的回归输入源；所有封存和验收读者仍是真实实现。
        with clients.source_scope():
            worlds.strict_accept(self, self.world)
        self.campaign = self.world["campaign"]
        self.manifest = self.world["manifest"]
        self.attempt_root = self.world["original_attempt_root"]
        self.segment_root = self.world["recovery_root"]
        self.prior_segment_root = self.world["prior_recovery_root"]
        self.summary = self.world["recovery_summary"]
        with self.source_context(), clients.source_scope(), sources.seams(self.world["config_path"]):
            comparison_path = upgrade._stage_path(self.campaign, "compare", transient.CANDIDATE)[1]
            comparison = compare.load_stage_result(self.campaign, comparison_path,
                transient.CANDIDATE, manifest=self.manifest)
            stage_binding = comparison["inputs"]["candidate_stage"]
            self.stage_path = compare._bound_campaign_file(self.campaign, stage_binding, "专项精确候选阶段")
            self.stage = candidate_seal._load_round_document(self.campaign, self.stage_path,
                transient.CANDIDATE, manifest=self.manifest, validation_round=1)
            segment_seal.validate_stage(self.campaign, self.stage, transient.CANDIDATE)
            self.effective_path = self.campaign / self.stage["effective_results"]["path"]
            store, store_campaign = self.world["store"], self.world["store_campaign"]
            handoff = delivery._vc5_handoff(store, store_campaign, transient.CANDIDATE)
            accept_path = upgrade._stage_path(self.campaign, "accept", transient.CANDIDATE)[1]
            qualification = {"store_relative": store.root.relative_to(self.data).as_posix(),
                "external_root_relative": ".", "campaign_id": self.campaign.name,
                "candidate_id": transient.CANDIDATE, "candidate_ref": handoff["candidate_ref"],
                "build_receipt_ref": handoff["candidate_build_receipt_ref"],
                "validation_ref": handoff["validation_completed_ref"], "acceptance_ref": handoff["acceptance_ref"],
                "sealed_result_binding": sources.binding(accept_path, self.data)}
            self.qualification = qualification
            self.accept_document = json.loads(accept_path.read_bytes())
            self.collector = ProjectionCollector(self.data)
            b0_path = upgrade._legacy_stage_path(self.campaign, "capture-candidate", transient.CANDIDATE)[1]
            b0_document = json.loads(b0_path.read_bytes())
            # 分别取证后再组完整 subject，一族失败也继续诊断其余族。
            # 每个投影仍由对应生产读者验真后给出，不从失败列表推权。
            self.check_case("原 b0 attempt 家族取证", lambda:
                material._retain_candidate_attempt_materials(self.campaign, b0_document, self.data,
                    retain=self.collector.retain, project_parent=self.collector.project_parent))
            self.check_case("原 b0 draft 家族取证", lambda:
                material._retain_candidate_evaluation_draft(self.campaign, self.manifest, b0_document, self.data,
                    retain=self.collector.retain, project_parent=self.collector.project_parent))
            self.check_case("b1 ar2 与历史 ar1 家族取证", lambda:
                material._retain_candidate_recovery_materials(self.campaign, self.manifest, stage_binding, self.data,
                    candidate_id=transient.CANDIDATE, retain=self.collector.retain,
                    project_parent=self.collector.project_parent))
            originals, parents = [], []
            self.check_case("完整 subject 来源读者", lambda:
                material.qualification_gate_subject(store, qualification, self.data,
                    self.accept_document, read_bindings=originals, read_projections=parents))
            self.check_case("完整 subject 与分族证明合并", lambda: self.collector.absorb(originals, parents))
            self.check_case("外部门禁原 reader 与完整第二根投影", self.prepare_gate_materials)
            family_paths = [self.attempt_root / "attempt.json", self.stage_path,
                self.effective_path, self.segment_root / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME,
                self.segment_root / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME]
            for root, document in ((self.attempt_root, self.world["original_attempt"]),
                    (self.prior_segment_root, self.world["prior_recovery_summary"]),
                    (self.segment_root, self.summary)):
                family_paths += [root / f"job-{row['id']}.json" for row in document["results"]]
                family_paths += [root / "checkpoints" / f"{number:08d}.json"
                    for number in range(1, document["job_checkpoint"]["record_count"] + 1)]
            family_paths += [self.prior_segment_root / name for name in
                (upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME, upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME)]
            for stage in (b0_document, self.stage):
                for name in ("seal_draft", "preview"):
                    family_paths.append(self.campaign / stage["seal"][name]["path"])
            for path in family_paths:
                self.check_case("闭包：" + path.relative_to(self.campaign).as_posix(),
                                lambda path=path: self.check_original_family(path))
            current_ids = {row["id"] for row in self.summary["results"]}
            expected_closures = [(self.prior_segment_root if row.get("recovered_from") == "ar1"
                else self.segment_root if row["id"] in current_ids else self.attempt_root)
                / row["job_closure"]["path"] for row in self.stage["results"]]
            for path in (self.stage_path, self.effective_path):
                self.check_case("逐 Job 产出：" + path.name,
                    lambda path=path: self.require_bindings(path, expected_closures))
            self.check_case("真实失败 ar1→ar2 的冻结继承证明与完整附件坐标", self.check_real_reuse_proof_bindings)
            self.check_case("完整 subject 原件归档", lambda: self.collector.close(
                [qualification["sealed_result_binding"]], include_archive=True))
            self.check_case("重算摘要也拒绝错误 Job 产出", self.check_wrong_producer_with_new_digest)
            self.check_case("重绑摘要也拒绝错误 summary", self.check_wrong_summary_with_new_binding)
            self.check_case("同 SHA 不同父坐标不授权", self.check_unknown_parent_not_authorized)
            self.check_case("未知阶段附件字段不授权", self.check_unknown_stage_field_not_authorized)
            self.check_case("重试轮次只承接精确首轮及规范坐标", self.check_round_two_cannot_change_first_round)
            self.check_case("合并封存重绑后仍可归档且不重算历史运行期资格", self.check_after_metadata_rebind)


class MultiCampaignProducerProjectionTests(reuse_tests.CandidateReuseFixture):
    """真实 P→S→T 承接，分别核直接来源与最初生产者，不扩大父字段权限。

    宿主、容器与入口身份沿原候选复用夹具；run、批准、结果追加、闭环
    复核及投影执行生产函数。阶段材料仅构造来源边界合同，不能代表全
    规则认证；正式完整 subject 另由上面的真实恢复前缀覆盖。
    """

    check_case = CandidateRecoveryProjectionTests.check_case
    require_bindings = CandidateRecoveryProjectionTests.require_bindings

    def setUp(self) -> None:
        super().setUp()
        self.observations = []
        self.source_coordinates = {}
        self.addCleanup(self.write_diagnostics)

    def write_diagnostics(self) -> None:
        value = os.environ.get(DIAGNOSTIC_ENV)
        if not value:
            return
        root = Path(value)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "multi-campaign-projection-observations.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"fixture_only": True, "world": str(self.data),
                       "source_coordinates": self.source_coordinates,
                       "observations": self.observations}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    def write_successor(self, campaign_id: str, source: Path) -> Path:
        campaign = self.write_candidate_campaign(campaign_id=campaign_id)
        manifest = json.loads((campaign / "campaign.json").read_bytes())
        manifest["predecessor"] = {"mode": "read_only_reuse",
            "campaign_id": source.name, "campaign_identity_sha256": reuse_tests.STORE_IDENTITY}
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        (campaign / "campaign.json").write_bytes(raw)
        (campaign / "campaign.sha256").write_text(material.sha256_bytes(raw) + "\n", encoding="utf-8")
        (campaign / "candidates" / reuse_tests.CANDIDATE).mkdir(parents=True)
        return campaign

    def inherit(self, campaign: Path, source: Path, attempt_id: str):
        preview = self.preview(campaign, source, attempt_id)
        self.assertEqual(preview["execute_job_ids"], [], "本专项两次承接都应零新增请求")
        code, result, stderr = self.resume_reuse(campaign, source, attempt_id,
            "--approve-candidate-reuse-sha256", preview["preview_sha256"])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
        return self.load_attempt(campaign, result["attempt_id"])

    def seal_source_boundary(self, campaign: Path, root: Path, attempt: dict[str, Any]):
        """用真实根、真实封存清单与已验证前序登记准备只读边界输入。"""
        with self.reuse_patched():
            manifest = evidence_manifest.build_evidence_manifest(
                candidate_seal._sealed_evidence_roots(campaign, root.name, reuse_tests.CANDIDATE),
                checkpoint_path=root / "projection-seal-checkpoint.json")
            manifest_path = root / candidate_seal.SEALED_EVIDENCE_MANIFEST_FILENAME
            upgrade._secure_write_json_once(manifest_path, manifest)
            evaluation_path = root / "projection-evaluation.json"
            upgrade._secure_write_json_once(evaluation_path, {})
            stage = {name: None for name in candidate_seal.STAGE_RESULT_FIELDS}
            stage.update({"schema_version": candidate_seal.STAGE_RESULT_SCHEMA, "client": "claude",
                "campaign_id": campaign.name, "stage": "capture-candidate", "candidate_id": reuse_tests.CANDIDATE,
                "status": "complete", "client_evidence": {"client_bindings": {}, "boundary_checks": {}},
                "attempt": candidate_seal._stage_binding(root / "attempt.json", campaign),
                candidate_seal.SEALED_EVIDENCE_MANIFEST_FIELD: candidate_seal._stage_binding(manifest_path, campaign),
                candidate_seal.ASSERTION_CONTEXT_FIELD: {
                    candidate_seal.EVALUATION_MANIFEST_FIELD: candidate_seal._stage_binding(evaluation_path, campaign)},
                predecessor_sealed.STAGE_FIELD: predecessor_sealed.stage_evidence(attempt["results"]),
                "sealed_at_utc": "2026-10-10T00:00:00Z"})
            # 本专项只把该真实边界交给原读者，不签发 Store 资格事实。
            candidate_seal.sealed_attempt_context(campaign, stage, candidate_id=reuse_tests.CANDIDATE)
            return stage

    def check_producer(self, result: dict[str, Any]) -> None:
        origin = result["reused_from"]
        self.assertEqual(origin["campaign"]["campaign_id"], self.direct_campaign.name)
        self.assertEqual(origin["attempt"]["path"],
            (self.direct_root / "attempt.json").relative_to(self.direct_campaign).as_posix())
        self.assertEqual(origin["job_receipt"]["path"],
            (self.direct_root / f"job-{result['id']}.json").relative_to(self.direct_campaign).as_posix())
        producer = attempt_recovery._producer(self.final_campaign, self.final_root, result)
        self.assertEqual(producer, self.first_producers[result["id"]])
        self.assertEqual(producer["campaign"]["campaign_id"], self.original_campaign.name)
        self.assertNotEqual(result["execution_sha256"], producer["execution_sha256"])
        layout = capture._capture_layout(self.final_campaign, upgrade.load_campaign_manifest(self.final_campaign))
        attempt_recovery._verify_reusable_evidence(self.final_campaign, result, producer, layout,
            phase="candidate", candidate_id=reuse_tests.CANDIDATE)

    def check_projected_origin(self, result: dict[str, Any]) -> None:
        # 原结果的探针相对最初 attempt；生产者闭环绑定相对最初 Campaign。
        targets = [self.original_root / run[name]["path"]
                   for run in result["environment_runs"] for name in ("before_probe", "after_probe")]
        producer = self.first_producers[result["id"]]
        closure = self.original_campaign / producer["job_closure"]["path"]
        for parent in (self.final_root / "attempt.json", self.final_root / f"job-{result['id']}.json"):
            self.require_bindings(parent, targets)
        self.require_bindings(closure, targets)

    def check_retained_originals(self, result: dict[str, Any]) -> None:
        producer = self.first_producers[result["id"]]
        closure = self.original_campaign / producer["job_closure"]["path"]
        cover = producer[predecessor_sealed.COVER_FIELD]
        consumed_originals = [self.original_campaign / "campaign.json", self.original_root / "attempt.json",
            self.original_campaign / cover["stage_result"]["path"],
            self.original_campaign / cover["sealed_evidence_manifest"]["path"], closure]
        expected = {path.relative_to(self.data).as_posix() for path in consumed_originals}
        missing = sorted(expected - set(self.collector.originals))
        self.observations.append({"coverage": "最初生产者精确原件留存：" + result["id"],
            "status": "failed" if missing else "passed", "missing": missing,
            "consumed_originals": sorted(expected),
            "direct_source": result["reused_from"], "original_producer": producer})
        self.assertEqual(missing, [], "原 reader 已实读但归档遗漏：" + repr(missing))

    def check_foreign_origin_refused(self, result: dict[str, Any]) -> None:
        forged = copy.deepcopy(result)
        forged["reused_from"]["producer"]["campaign"] = copy.deepcopy(forged["reused_from"]["campaign"])
        collector = ProjectionCollector(self.data)
        # 改来源声明并重新计算父摘要也不能取得另一 Campaign 的原件权限。
        with self.assertRaises((ConfigurationError, ControlError)):
            material._candidate_attempt_result_producers(self.final_campaign,
                {**self.final_attempt, "results": [forged]}, self.final_root, self.data,
                retain=collector.retain, project_parent=collector.project_parent)
        self.assertFalse(collector.parents)

    def test_twice_cross_campaign_keeps_exact_original_producer(self) -> None:
        self.original_campaign, original_id = reuse_tests.SealedReuseTests.sealed_predecessor(self)
        self.original_root, original_attempt = self.load_attempt(self.original_campaign, original_id)
        before = reuse_tests.SealedReuseTests.source_boundary(self, self.original_campaign, original_id)
        calls = len(self.candidate_calls())
        self.direct_campaign = self.write_successor(reuse_tests.SUCCESSOR, self.original_campaign)
        self.direct_root, direct = self.inherit(self.direct_campaign, self.original_campaign, original_id)
        self.first_producers = {result["id"]: copy.deepcopy(result["reused_from"]["producer"])
                                for result in direct["results"]}
        self.final_campaign = self.write_successor("claude-code-2_1_285-t3", self.direct_campaign)
        self.final_root, self.final_attempt = self.inherit(self.final_campaign, self.direct_campaign, self.direct_root.name)
        self.source_coordinates = {"original_campaign": str(self.original_campaign),
            "original_attempt": str(self.original_root), "direct_campaign": str(self.direct_campaign),
            "direct_attempt": str(self.direct_root), "final_campaign": str(self.final_campaign),
            "final_attempt": str(self.final_root)}
        self.assertEqual(len(self.candidate_calls()), calls)
        with self.reuse_patched():
            for result in self.final_attempt["results"]:
                self.check_case("跨 Campaign 原生产者：" + result["id"], lambda result=result: self.check_producer(result))
            stage = self.seal_source_boundary(self.final_campaign, self.final_root, self.final_attempt)
            self.collector = ProjectionCollector(self.data)
            material._retain_candidate_attempt_materials(self.final_campaign, stage, self.data,
                retain=self.collector.retain, project_parent=self.collector.project_parent)
            for result in self.final_attempt["results"]:
                self.check_case("跨 Campaign 精确原件留存：" + result["id"],
                                lambda result=result: self.check_retained_originals(result))
                self.check_case("跨 Campaign 探针与闭环坐标：" + result["id"],
                                lambda result=result: self.check_projected_origin(result))
                self.check_case("拒绝混用直接来源根：" + result["id"],
                                lambda result=result: self.check_foreign_origin_refused(result))
            self.check_case("跨 Campaign 当前父族全部原件归档", lambda: self.collector.close(
                [sources.binding(self.final_root / "attempt.json", self.data)], include_archive=True))
        self.assertEqual(reuse_tests.SealedReuseTests.source_boundary(self, self.original_campaign, original_id), before)
