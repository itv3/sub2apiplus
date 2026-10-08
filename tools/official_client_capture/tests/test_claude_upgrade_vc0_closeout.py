"""Claude VC-0 收口（Claude 升级控制面设计 B2-11）：共享收口流程在 Claude 作用域里建 Formal 并派发首批。

* 端到端：真实预检 Campaign（``plan`` 建）＋四份输入（Claude 部署收据、Claude v2 发布认证、它绑定的 Job 演练、两客户端同一
  合同的 P0 门禁收据）→ ``claude_upgrade_vc0_closeout`` 一条命令：收口 attempt、收据副本组、active VC-0 checkpoint、从预检
  恢复参数建 Formal（五项控制收据、首批恰是一个 ``capture-official`` 动作、Store Campaign v2 与 bootstrap 四份控制收据副本）、
  项目总账登记、「VC-0 完成」「VC-1 开始」、首批派发与 Claude 合同名的收口收据；Formal 的执行合同与预检签 Job 演练时的合同
  逐字相同（同一份冻结、同一组坐标与工具身份）；
* 需要人工处理与拒绝：发布认证绑定的部署收据不是这一份、Job 演练不是这个预检签的，在写任何账本事件之前拒绝；两个 Codex
  历史修复子命令明确拒绝；现场判定用 Claude 的现场合同名；
* Formal 参数恢复：基线规则清单必须是仓库那份且与预检冻结副本逐字节相同；冻结输入全部取预检副本；
* Codex 不变：没有作用域时收口取 Codex 适配器，Codex 画像就是收口模块本身（合同名与两个画像函数是原值）；两客户端作用域
  嵌套即拒绝。

真实发布认证要在 ARM64 签发（B5a-09），这里用替身返回认证内容；Job 演练与 ARM64 环境收据的重放同样用替身（各自的模块测试
覆盖重放），其余（计时账本、P0 门禁收据、项目总账、Store、VC 制品、冻结目录复核）全部真实执行。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import certify_release
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_campaign as campaign_module
from tools.official_client_capture import claude_upgrade_job_rehearsal as claude_job_profile
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc0_closeout as claude_closeout
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as job_rehearsal
from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests.control_receipt_fixtures import create_p0_gate_receipt
from tools.official_client_capture.tests.test_claude_upgrade_campaign import (
    BASELINE,
    TARGET,
    UPGRADE_ID,
    CampaignWorldTestCase,
    _sha256,
    _write_json,
)

PREFLIGHT_ID = "claude-code-2_1_285-preflight-t1"
FORMAL_ID = "claude-code-2_1_285-formal-t1"
DEPLOY_CAMPAIGN = "c01600-supervisor-enable-fixture"


class ClaudeCloseoutTestCase(CampaignWorldTestCase):
    def setUp(self) -> None:
        super().setUp()
        extra = self.world.predecessor_argv() if self.evidence_decision == "reuse" else []
        self.preflight, self.preflight_manifest = self.world.create_preflight(PREFLIGHT_ID, *extra)
        control = self.world.data / "control"
        identity = claude_upgrade._tool_identity(include_git=False)
        self.identity = identity
        # Claude 受管部署收据（B5a-01 形状）：放在数据根 control 下（项目总账对账也按它核当前工具身份）。
        self.deploy_run = control / "deploy-run"
        self.deploy_run.mkdir(mode=0o700)
        deploy = ledger_fixture.write_claude_deploy_receipt(self.world.data, identity, stamp="20261007t000000z")
        payload = json.loads(deploy.read_text(encoding="utf-8"))
        payload.update(
            {
                "campaign_id": DEPLOY_CAMPAIGN,
                "architecture": "aarch64",
                "production_data_root": str(self.world.data),
                "supervisor_run_dir": str(self.deploy_run),
            }
        )
        self.deploy = _write_json(deploy, payload)
        # 完整 Job 演练（替身重放）：执行合同按预检总计划算，绑定这个预检 Campaign。
        self.job_root = control / "job-rehearsal-t1"
        self.job_root.mkdir(mode=0o700)
        self.job_path = _write_json(self.job_root / "receipt.json", {"synthetic": "claude-job-rehearsal"})
        contract = campaign_module.job_rehearsal_contract(self.preflight, self.preflight_manifest)
        self.contract = contract
        self.job_receipt = {
            "schema_version": claude_job_profile.RECEIPT_SCHEMA,
            "status": "passed",
            "execution_contract": contract,
            "execution_contract_sha256": job_rehearsal.execution_contract_sha256(contract),
            "job_count": contract["job_count"],
            "job_set_sha256": "1" * 64,
            "runtime_identity_sha256": "2" * 64,
            "storage_probe_sha256": "3" * 64,
            # B2-11 第二部分起 Claude Job 演练与 Codex 同样要求失败生命周期探针摘要（Formal 必需）。
            "failure_lifecycle_probe_sha256": "5" * 64,
            "preflight_campaign": {
                "path": str(self.preflight),
                "campaign_id": PREFLIGHT_ID,
                "manifest_sha256": _sha256(self.preflight / "campaign.json"),
                "campaign_mode": "preflight_only",
            },
        }
        # Claude v2 发布认证（替身核验）：五摘要等于部署收据，绑定部署收据与 Job 演练。
        self.release = _write_json(control / "policy-certification" / "claude-release-certification.json", {"synthetic": "release"})
        self.certification = {
            "receipt_sha256": "4" * 64,
            "policy_version": identity["policy_version"],
            "identity": {field: policy_certification.current_identity("claude")[field] for field in policy_certification.IDENTITY_FIELDS},
            "deployment_receipt": {"path": str(self.deploy), "sha256": _sha256(self.deploy)},
            "job_rehearsal": {"evidence_root": str(self.job_root), "receipt": "receipt.json", "sha256": _sha256(self.job_path),
                              "failure_lifecycle_probe_sha256": "5" * 64},
        }
        # P0 门禁收据：两客户端同一合同，主体是 Claude 的升级与这份发布认证（真实生成与重放）。
        self.p0 = create_p0_gate_receipt(
            control / "p0-t1",
            upgrade_id=UPGRADE_ID,
            baseline_version=BASELINE,
            target_version=TARGET,
            campaign_purpose="production_replacement",
            release_certification=self.release,
        )
        self.formal = self.world.campaign_dir(FORMAL_ID)
        self.supervisor_state = control / "vc1-supervisor-t1"
        self.run_calls: list[argparse.Namespace] = []

    def argv(self, audit: str = "audit-t1") -> list[str]:
        return [
            "--preflight-campaign-dir", str(self.preflight),
            "--formal-campaign-dir", str(self.formal),
            "--formal-campaign-id", FORMAL_ID,
            "--p0-gate-root", str(self.p0.parent),
            "--p0-gate-receipt", self.p0.name,
            "--managed-tool-deploy-receipt", str(self.deploy),
            "--release-certification", str(self.release),
            "--supervisor-state-dir", str(self.supervisor_state),
            "--audit-dir", str(self.world.data / "control" / audit),
        ]

    def _read_state(self, run_dir: Path) -> dict[str, Any]:
        if Path(run_dir).resolve() == self.deploy_run.resolve():
            return {"campaign_id": DEPLOY_CAMPAIGN, "phase": "bootstrap", "state": "stopped"}
        return self._original_read_state(run_dir)

    def _run_command(self, namespace: argparse.Namespace) -> tuple[int, dict[str, Any]]:
        self.run_calls.append(namespace)
        return 0, {"status": "stopped", "reason": "queue-complete", "run_dir": str(self.supervisor_state / "run-fixture")}

    @contextlib.contextmanager
    def patched(self, *, job_receipt: dict[str, Any] | None = None, certification: dict[str, Any] | None = None) -> Iterator[None]:
        self._original_read_state = codex_upgrade_supervisor._read_state
        tool_root = (self.world.data / "tools" / "official_client_capture").resolve(strict=False)
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.world.environment_patch())
            stack.enter_context(mock.patch.object(job_rehearsal, "replay", return_value=job_receipt or self.job_receipt))
            stack.enter_context(mock.patch.object(certify_release, "verify", return_value=certification or self.certification))
            stack.enter_context(mock.patch.object(claude_closeout.platform, "machine", return_value="aarch64"))
            stack.enter_context(mock.patch.object(claude_closeout, "_current_tool_root", return_value=tool_root))
            stack.enter_context(mock.patch.object(codex_upgrade_supervisor, "_read_state", side_effect=self._read_state))
            stack.enter_context(
                mock.patch.object(
                    codex_upgrade_supervisor,
                    "_audit_command",
                    return_value={"run_dir": str(self.deploy_run.resolve()), "state": "stopped", "audit_incomplete": False},
                )
            )
            stack.enter_context(mock.patch.object(codex_upgrade_supervisor, "_campaign_run_command", side_effect=self._run_command))
            yield

    def run_closeout(self, argv: list[str], **patches: Any) -> tuple[int, dict[str, Any] | None, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with self.patched(**patches), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = claude_closeout.main(argv)
        text = stdout.getvalue().strip()
        return code, (json.loads(text) if text else None), stderr.getvalue()

    def event_ids(self) -> list[str]:
        return [str(event["event_id"]) for event, _raw in timing._load_events(self.world.ledger)]


class ClaudeCloseoutEndToEndTests(ClaudeCloseoutTestCase):
    def test_closeout_creates_formal_and_dispatches_first_batch(self) -> None:
        code, receipt, stderr = self.run_closeout(self.argv())
        self.assertEqual(code, 0, stderr)
        assert receipt is not None
        self.assertEqual(receipt["schema_version"], claude_closeout.CLOSEOUT_RECEIPT_SCHEMA)
        self.assertEqual((receipt["create_campaign_call_count"], receipt["campaign_run_call_count"]), (1, 1))
        self.assertEqual(sorted(item["role"] for item in receipt["input_receipts"]), list(closeout.INPUT_ROLES))
        self.assertEqual(len(self.run_calls), 1)
        # Formal：五项控制收据，计时 checkpoint 是收口新写的 active VC-0 checkpoint；前序沿用预检（null）。
        manifest = claude_upgrade.load_campaign_manifest(self.formal)
        self.assertEqual((manifest["campaign_mode"], manifest["client"]), ("formal", "claude"))
        controls = manifest["control_receipts"]
        self.assertEqual(sorted(controls), sorted(campaign_module.FORMAL_CONTROL_RECEIPTS))
        self.assertTrue(controls["upgrade_timing"]["receipt"]["path"].startswith(f"receipts/vc0-closeout/{FORMAL_ID}/"))
        self.assertEqual(controls["job_rehearsal"]["preflight_campaign_id"], PREFLIGHT_ID)
        self.assertEqual(controls["release_certification"]["sha256"], _sha256(self.release))
        self.assertIsNone(manifest["predecessor"])
        with self.patched():
            campaign_module.verify_control_receipts(manifest, require_complete=True)
        # Formal 与预检同源：冻结输入逐字节相同，执行合同与预检签 Job 演练时的合同逐字相同。
        for name in ("target_scenarios", "evidence_labels", "active_profile", "production_inventory", "static_baseline_anchors"):
            self.assertEqual(manifest["inputs"][name]["sha256"], self.preflight_manifest["inputs"][name]["sha256"], name)
        self.assertEqual(campaign_module.job_rehearsal_contract(self.formal, manifest), self.contract)
        # 首批：恰一个 capture-official 动作，项为全部 official Job（共享监督器的 VC-1 普通恢复链要求）。
        batch = json.loads((self.formal / manifest["vc_control"]["first_formal_batch"]["path"]).read_text(encoding="utf-8"))
        official = sorted(job["id"] for job in manifest["jobs"] if job["phase"] == "official")
        self.assertEqual([action["action_id"] for action in batch["actions"]], ["capture-official"])
        self.assertEqual((batch["execute_item_ids"], batch["actions"][0]["item_ids"]), (official, official))
        vc0 = json.loads((self.formal / manifest["vc_control"]["vc0_checkpoint"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(vc0["stage_receipt"]["sha256"], _sha256(self.p0))
        # Store Campaign v2 与 bootstrap：四份控制收据副本（计时、环境、P0、发布认证）。
        store, store_campaign = claude_upgrade_store.open_campaign_store(self.formal, manifest)
        bootstrap = store.load_object(store_campaign["bootstrap_ref"])["payload"]
        self.assertEqual(
            [Path(item["path"]).name for item in bootstrap["fw_c_receipts"]],
            ["arm64-environment.json", "p0-gate.json", "release-certification.json", "timing-checkpoint.json"],
        )
        self.assertTrue(store.replay(external_root=self.world.data, require_external=True))
        # 账本：收口 attempt、收据通过、VC-0 完成与 VC-1 开始；项目总账登记了 Formal。
        events = self.event_ids()
        for event_id in (
            f"{FORMAL_ID}-closeout-a1-started",
            f"{FORMAL_ID}-p0-receipts-passed",
            f"{FORMAL_ID}-closeout-a1-completed",
            f"{FORMAL_ID}-vc0-completed",
            f"{FORMAL_ID}-vc1-started",
        ):
            self.assertIn(event_id, events)
        self.assertIn(FORMAL_ID, project_ledger.replay_head(self.world.project_root)["registered_campaigns"])
        self.assertEqual(len(campaign_module.require_campaign_client_roots(self.formal, manifest)), 4)

    def test_inputs_not_bound_together_are_rejected_before_any_event(self) -> None:
        before = self.event_ids()
        other_deploy = {**self.certification, "deployment_receipt": {"path": "/elsewhere.json", "sha256": "0" * 64}}
        code, _result, stderr = self.run_closeout(self.argv("audit-a"), certification=other_deploy)
        self.assertEqual(code, 1)
        self.assertIn("部署收据不是本次收口提供", stderr)
        foreign = {**self.job_receipt, "preflight_campaign": {**self.job_receipt["preflight_campaign"], "campaign_id": "other"}}
        code, _result, stderr = self.run_closeout(self.argv("audit-b"), job_receipt=foreign)
        self.assertEqual(code, 1)
        self.assertIn("未绑定当前 preflight", stderr)
        drifted_contract = {**self.job_receipt, "execution_contract": {**self.contract, "suite": "core"}}
        code, _result, stderr = self.run_closeout(self.argv("audit-c"), job_receipt=drifted_contract)
        self.assertEqual(code, 1)
        self.assertIn("Job rehearsal 收据重放失败", stderr)
        # B2-11 第二部分：没有失败生命周期探针摘要的 Claude Job 演练（补探针之前的收据）不能用来建 Formal。
        without_lifecycle = {key: value for key, value in self.job_receipt.items() if key != "failure_lifecycle_probe_sha256"}
        code, _result, stderr = self.run_closeout(self.argv("audit-d"), job_receipt=without_lifecycle)
        self.assertEqual(code, 1)
        self.assertIn("Job rehearsal 收据重放失败", stderr)
        self.assertEqual(self.event_ids(), before)
        self.assertFalse(self.formal.exists())
        failure = json.loads((self.world.data / "control" / "audit-a" / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["schema_version"], claude_closeout.CLOSEOUT_DIAGNOSTIC_SCHEMA)

    def test_inspect_and_rejected_repair_subcommands(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = claude_closeout.main(
                ["inspect", "--preflight-campaign-dir", str(self.preflight), "--formal-campaign-dir", str(self.formal),
                 "--formal-campaign-id", FORMAL_ID, "--supervisor-state-dir", str(self.supervisor_state)]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        site = json.loads(stdout.getvalue())
        self.assertEqual((site["schema_version"], site["site"]), (claude_closeout.SITE_SCHEMA, "fresh"))
        for name in claude_closeout.REJECTED_SUBCOMMANDS:
            with self.subTest(name=name), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(claude_closeout.main([name, "--formal-campaign-dir", str(self.formal)]), 1)
            self.assertIn("Claude 没有这类记录", error.getvalue())


class ReusePredecessorCloseoutTests(ClaudeCloseoutTestCase):
    """只读复用前序：账本是 reuse，预检冻结前序绑定，Formal 沿用它，首批是零请求 no-op（复用项为全部 official Job）。"""

    evidence_decision = "reuse"

    def test_formal_reuses_predecessor_with_noop_first_batch(self) -> None:
        binding = self.preflight_manifest["predecessor"]
        self.assertEqual((binding["mode"], binding["campaign_id"]), ("read_only_reuse", self.world.predecessor_campaign))
        code, receipt, stderr = self.run_closeout(self.argv())
        self.assertEqual(code, 0, stderr)
        manifest = claude_upgrade.load_campaign_manifest(self.formal)
        self.assertEqual(manifest["predecessor"], binding)
        batch = json.loads((self.formal / manifest["vc_control"]["first_formal_batch"]["path"]).read_text(encoding="utf-8"))
        official = sorted(job["id"] for job in manifest["jobs"] if job["phase"] == "official")
        self.assertEqual((batch["actions"], batch["execute_item_ids"], batch["reuse_item_ids"]), ([], [], official))
        self.assertEqual(receipt["schema_version"], claude_closeout.CLOSEOUT_RECEIPT_SCHEMA)


class FormalArgumentRecoveryTests(ClaudeCloseoutTestCase):
    def validated(self) -> closeout.ValidatedInputs:
        with self.patched(), claude_closeout.client_scope():
            return claude_closeout.validate_inputs(
                preflight_campaign_dir=self.preflight,
                p0_gate_root=self.p0.parent,
                p0_gate_receipt=Path(self.p0.name),
                managed_tool_deploy_receipt=self.deploy,
                release_certification=self.release,
            )

    def test_recovered_arguments_take_frozen_preflight_inputs(self) -> None:
        validated = self.validated()
        self.assertEqual(validated.job_rehearsal_receipt, self.job_path.resolve())
        arguments = claude_closeout.recover_formal_plan_arguments(
            validated, formal_campaign_dir=self.formal, formal_campaign_id=FORMAL_ID, timing_receipt=self.world.ledger / "x.json"
        )
        self.assertEqual((arguments.command, arguments.campaign_mode, arguments.campaign_id), ("plan", "formal", FORMAL_ID))
        self.assertEqual(Path(arguments.scenario_manifest).parent, (self.preflight / "inputs").resolve())
        self.assertEqual(Path(arguments.production_inventory).name, "production-inventory.json")
        self.assertEqual(Path(arguments.rule_manifest), claude_upgrade_plan.baseline_rule_manifest_path(BASELINE))
        self.assertEqual(arguments.source_commit, self.preflight_manifest["configuration"]["source_commit"])
        self.assertIsNone(arguments.predecessor_binding)

    def test_recovered_arguments_carry_candidate_variables(self) -> None:
        """B4-01 d 段（B5b-01 落实）：预检 configuration 里的候选阶段变量（网关侧三个容器名、候选验收账号与 ingress API Key）原样
        带进 Formal plan；空串与缺字段不带（与预检同一取值），类型不对即拒绝。"""

        import dataclasses

        validated = self.validated()
        configuration = dict(validated.preflight_manifest["configuration"])
        configuration.update(service_container="sub2apiplus", keeper_container="sub2apiplus-keeper", postgres_container="sub2apiplus-postgres",
                             claude_account_id="7", api_key_id="8")

        def recover(values: dict[str, Any]) -> Any:
            manifest = {**validated.preflight_manifest, "configuration": values}
            return claude_closeout.recover_formal_plan_arguments(
                dataclasses.replace(validated, preflight_manifest=manifest), formal_campaign_dir=self.formal,
                formal_campaign_id=FORMAL_ID, timing_receipt=self.world.ledger / "x.json",
            )

        arguments = recover(configuration)
        self.assertEqual(
            (arguments.service_container, arguments.keeper_container, arguments.postgres_container, arguments.claude_account_id,
             arguments.api_key_id),
            ("sub2apiplus", "sub2apiplus-keeper", "sub2apiplus-postgres", "7", "8"),
        )
        empty = recover({**configuration, "claude_account_id": "", "api_key_id": ""})
        self.assertEqual((empty.claude_account_id, empty.api_key_id), ("", ""), "预检里为空（official-only）就不带，取 plan 缺省的空串")
        missing = recover({key: value for key, value in configuration.items() if key not in {"claude_account_id", "api_key_id"}})
        self.assertEqual((missing.claude_account_id, missing.api_key_id), ("", ""), "B4-01 之前建的预检没有这两个字段")
        with self.assertRaisesRegex(closeout.VC0CloseoutError, "configuration.api_key_id 不是字符串"):
            recover({**configuration, "api_key_id": 8})

    def test_baseline_rules_drift_after_preflight_is_rejected(self) -> None:
        validated = self.validated()
        drifted = self.root / "drifted-baseline-rules.json"
        drifted.write_text(claude_upgrade_plan.baseline_rule_manifest_path(BASELINE).read_text(encoding="utf-8") + " ", encoding="utf-8")
        with mock.patch.object(claude_upgrade_plan, "baseline_rule_manifest_path", return_value=drifted):
            with self.assertRaisesRegex(closeout.VC0CloseoutError, "基线规则清单"):
                claude_closeout.recover_formal_plan_arguments(
                    validated, formal_campaign_dir=self.formal, formal_campaign_id=FORMAL_ID, timing_receipt=self.world.ledger / "x.json"
                )


class CodexCloseoutUnchangedTests(unittest.TestCase):
    def test_default_scope_is_codex_and_codex_profile_is_the_module(self) -> None:
        self.assertEqual(closeout._adapter().client, "codex")
        codex = upgrade_client_adapter.adapter_for("codex")
        self.assertIs(codex.closeout_profile, closeout)
        self.assertIs(codex.closeout_profile.validate_inputs, closeout.validate_inputs)
        self.assertEqual(
            (closeout.CLOSEOUT_RECEIPT_SCHEMA, closeout.CLOSEOUT_DIAGNOSTIC_SCHEMA, closeout.SITE_SCHEMA,
             closeout.LEDGER_RESUME_PREVIEW_SCHEMA, closeout.VC1_HANDOVER_SCHEMA),
            ("codex-upgrade-vc0-closeout-receipt/v1", "codex-upgrade-vc0-closeout-diagnostic/v1", "codex-upgrade-vc0-closeout-site/v1",
             "codex-upgrade-vc0-ledger-resume-preview/v1", "codex-upgrade-vc0-vc1-handover/v1"),
        )
        self.assertIs(upgrade_client_adapter.adapter_for("claude").closeout_profile, claude_closeout)

    def test_scopes_of_two_clients_cannot_nest(self) -> None:
        with claude_closeout.client_scope():
            self.assertEqual(closeout._adapter().client, "claude")
            with self.assertRaisesRegex(RuntimeError, "客户端作用域冲突"):
                with closeout.adapter_scope(upgrade_client_adapter.adapter_for("codex")):
                    pass
        self.assertEqual(closeout._adapter().client, "codex")


if __name__ == "__main__":
    unittest.main()
